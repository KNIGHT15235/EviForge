"""Session-facing Trace-to-Skill evolution service."""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from mewcode.runtime.models import TraceRecord
from mewcode.runtime.store import RuntimeStore
from mewcode.runtime.fsm import TaskState

from .models import ExperienceCandidate, ScopeKind, SkillScope
from .registry import EvolutionRegistry


_TOKEN = re.compile(r"[\w.-]+", re.UNICODE)


def project_fingerprint(workspace: str | Path) -> str:
    root = Path(workspace).expanduser().resolve(strict=False)
    git = root / ".git"
    # A project identity must survive ordinary commits; binding it to HEAD would
    # make every newly committed task unable to retrieve yesterday's skill.
    # Hash only local identity metadata and never expose the path/config itself.
    seed = f"workspace:{root}"
    try:
        config = (git / "config").read_text(encoding="utf-8")
        remote = re.search(r"^\s*url\s*=\s*(.+?)\s*$", config, re.MULTILINE)
        if remote:
            seed = f"remote:{remote.group(1).strip()}"
    except OSError:
        pass
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def task_signature(text: str, *, max_terms: int = 24) -> str:
    terms = [term.casefold() for term in _TOKEN.findall(text)]
    return " ".join(terms[:max_terms]) or "unspecified task"


@dataclass(frozen=True, slots=True)
class EvolutionIngestResult:
    candidate_id: str
    disposition: str
    eligible_for_promotion: bool
    blocked_reasons: tuple[str, ...]


class TraceEvolutionService:
    """Turn completed task evidence into a quarantined candidate.

    The service is intentionally deterministic and conservative.  It does not
    let a model copy arbitrary conversation text into an executable Skill;
    callers provide a decision/procedure, all source trace and evidence refs
    are bound here, and :class:`CandidateSanitizer` performs the final poison
    gate in the authoritative registry.
    """

    def __init__(
        self,
        registry: EvolutionRegistry,
        runtime: RuntimeStore,
        *,
        workspace: str | Path,
    ) -> None:
        self.registry = registry
        self.runtime = runtime
        self.workspace = Path(workspace).expanduser().resolve(strict=False)

    def successful_evidence_refs(self, task_id: str) -> tuple[str, ...]:
        run = self.runtime.get_task(task_id)
        if run is None or run.state not in {
            TaskState.COMPLETED,
            TaskState.EVOLUTION_PENDING,
        }:
            return ()
        events = self.runtime.list_events(task_id=task_id)
        references: list[str] = []
        pass_observed = False
        for record in events:
            event = record.event
            if event.event_type == "task_state_changed" and event.status == "COMPLETED":
                details = event.payload.get("details", {})
                if details.get("gate_verdict") != "PASS":
                    continue
                decision = details.get("decision_id")
                bundle = details.get("bundle_ref")
                if decision:
                    pass_observed = True
                    references.append(f"gate://{decision}")
                if bundle:
                    references.append(str(bundle))
        if not pass_observed:
            return ()
        for record in events:
            references.extend(str(ref) for ref in record.event.artifact_refs)
        return tuple(dict.fromkeys(references))

    def structured_failures(self, task_id: str) -> tuple[TraceRecord, ...]:
        """Return failed tool stages without exposing raw tool arguments/output."""

        allowed = {
            "FAILED",
            "TOOL_ERROR",
            "INTERNAL_ERROR",
            "VALIDATION_ERROR",
            "PERMISSION_DENIED",
        }
        return tuple(
            record
            for record in self.runtime.list_events(task_id=task_id)
            if record.event.event_type == "tool_execution_stage"
            and str(record.event.status or "").upper() in allowed
        )

    def derive_failure_facts(self, task_id: str) -> tuple[str, str, tuple[str, ...]]:
        """Derive bounded facts from typed event metadata, never event prose."""

        failures = self.structured_failures(task_id)
        if not failures:
            return (
                "verified task required no failed tool retry",
                "verified-first-pass",
                (),
            )
        latest = failures[-1].event
        codes = latest.payload.get("reason_codes", ())
        safe_codes = tuple(
            str(code)[:128]
            for code in codes
            if isinstance(code, str) and code.strip()
        )[:8]
        tool = (latest.tool_name or "tool")[:64]
        signature = f"{tool}: {latest.error_class or latest.status or 'failed'}"
        family = (latest.error_class or (safe_codes[0] if safe_codes else "tool-failure"))
        family = re.sub(r"[^A-Za-z0-9_.:-]+", "-", str(family)).strip("-")[:128]
        if len(family) < 2:
            family = "tool-failure"
        return signature[:512], family, safe_codes

    def ingest(
        self,
        *,
        task_id: str,
        objective: str,
        failure_signature: str,
        root_cause_family: str,
        decision: str,
        procedure: Iterable[str],
        failed_attempts: Iterable[str] = (),
        source_commit: str,
        source_code_hash: str,
        language: str = "python",
        confidence_prior: float = 0.5,
        expires_in_days: int = 90,
    ) -> EvolutionIngestResult:
        run = self.runtime.get_task(task_id)
        if run is None:
            raise KeyError(f"unknown TaskRun: {task_id}")
        evidence = self.successful_evidence_refs(task_id)
        if not evidence:
            raise ValueError("a candidate requires a completed Evidence Gate receipt")
        if expires_in_days < 1:
            raise ValueError("expires_in_days must be positive")
        created = datetime.now(timezone.utc)
        project = project_fingerprint(self.workspace)
        candidate = ExperienceCandidate(
            candidate_id=f"candidate-{uuid.uuid4().hex}",
            task_signature=task_signature(objective),
            failure_signature=failure_signature,
            root_cause_family=root_cause_family,
            decision=decision,
            procedure=tuple(procedure),
            failed_attempts=tuple(failed_attempts),
            evidence_refs=evidence,
            source_trace_ids=(run.trace_id,),
            source_commit=source_commit,
            source_code_hash=source_code_hash,
            project_fingerprint=project,
            scope=SkillScope(
                kind=ScopeKind.PROJECT,
                project_fingerprints=(project,),
                languages=(language,),
            ),
            confidence_prior=confidence_prior,
            expires_at=created + timedelta(days=expires_in_days),
            created_at=created,
        )
        record = self.registry.register_candidate(candidate, actor="trace-evolution-service")
        return EvolutionIngestResult(
            candidate_id=candidate.candidate_id,
            disposition=record.disposition.value,
            eligible_for_promotion=record.eligible_for_promotion,
            blocked_reasons=record.blocked_reasons,
        )

    def ingest_structured(
        self,
        *,
        task_id: str,
        objective: str,
        decision_code: str,
        procedure_codes: Sequence[str],
        source_commit: str,
        source_code_hash: str,
        language: str = "python",
        metadata: Mapping[str, str] | None = None,
    ) -> EvolutionIngestResult:
        """Build a conservative candidate from allowlisted structured fields.

        ``decision_code`` and ``procedure_codes`` are expected to be trusted
        application-owned identifiers/descriptions. They are length-bounded and
        sanitized by the registry. Conversation text and tool output are never
        inspected or copied by this method.
        """

        if not decision_code.strip() or not procedure_codes:
            raise ValueError("structured decision and procedure codes are required")
        failure, family, failed_codes = self.derive_failure_facts(task_id)
        constraints = metadata or {}
        safe_constraints = tuple(
            f"{re.sub(r'[^A-Za-z0-9_.:-]+', '-', str(key))[:64]}="
            f"{re.sub(r'[^A-Za-z0-9_.:-]+', '-', str(value))[:128]}"
            for key, value in sorted(constraints.items())
            if str(key).strip() and str(value).strip()
        )[:16]
        # Feed the strict core, then add constraints by constructing the same
        # immutable candidate shape through the ordinary ingestion path.  The
        # current public ingest deliberately owns scope/evidence binding.
        return self.ingest(
            task_id=task_id,
            objective=objective,
            failure_signature=failure,
            root_cause_family=family,
            decision=decision_code[:8_000],
            procedure=tuple(str(item)[:2_000] for item in procedure_codes[:64]),
            failed_attempts=tuple((*failed_codes, *safe_constraints))[:64],
            source_commit=source_commit,
            source_code_hash=source_code_hash,
            language=language,
        )
