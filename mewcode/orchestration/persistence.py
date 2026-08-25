"""Durable, host-owned DAG run records and conservative resume planning.

The workspace is model-visible and therefore is not an authoritative place for
resume metadata.  Records in this module live below EviForge's host control
root, are written atomically, and contain only typed graph/report data (never
provider credentials or prompts outside the reviewed graph itself).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from mewcode.runtime import ControlPlanePaths, resolve_control_root, workspace_id_for

from .artifact_store import ArtifactStore, ArtifactStoreError
from .events import DAGBudgetSnapshot, DAGProgressEvent, DAGProgressEventType
from .graph import TaskGraph
from .models import (
    AcceptanceStatus,
    ArtifactRef,
    ChangeEnvelope,
    NodeReport,
    NodeStatus,
    RunStatus,
    ScheduleBudget,
    ScheduleReport,
    SchedulerMetrics,
)


_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_IGNORED_WORKSPACE_PARTS = frozenset(
    {".git", ".pytest_cache", "__pycache__", ".venv"}
)


class DAGPersistenceError(RuntimeError):
    """A durable DAG record is missing, corrupt, or violates its boundary."""


class DAGRunState(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"
    BUDGET_EXCEEDED = "budget_exceeded"
    INTERRUPTED = "interrupted"


class DAGResumeAction(StrEnum):
    SKIP_VERIFIED = "skip_verified"
    RETRY_SAFE = "retry_safe"
    BLOCKED = "blocked"


class PersistedDAGNode(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    node_id: str
    observed_status: str = NodeStatus.PENDING.value
    report: NodeReport | None = None
    note: str | None = None

    @field_validator("observed_status")
    @classmethod
    def _known_status(cls, value: str) -> str:
        allowed = {item.value for item in NodeStatus} | {"uncertain"}
        if value not in allowed:
            raise ValueError("unknown persisted DAG node status")
        return value


class DAGRunRecord(BaseModel):
    """Atomic source of truth for one logical DAG run across attempts."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "1.0"
    run_id: str
    state: DAGRunState = DAGRunState.RUNNING
    graph_hash: str
    graph: TaskGraph
    workspace_root: str
    workspace_id: str
    workspace_fingerprint: str
    capability_hash: str
    capability_profile: dict[str, Any]
    budget: ScheduleBudget
    max_concurrency: int = Field(ge=1)
    attempt: int = Field(default=1, ge=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    nodes: dict[str, PersistedDAGNode]
    accepted_changes: tuple[ChangeEnvelope, ...] = ()
    metrics: SchedulerMetrics | None = None
    last_budget: DAGBudgetSnapshot | None = None
    interruption: str | None = None

    @field_validator("run_id")
    @classmethod
    def _safe_run_id(cls, value: str) -> str:
        if not _RUN_ID_RE.fullmatch(value):
            raise ValueError("run_id must be a safe stable identifier")
        return value

    @model_validator(mode="after")
    def _record_integrity(self) -> "DAGRunRecord":
        graph_nodes = set(self.graph.by_id)
        if set(self.nodes) != graph_nodes:
            raise ValueError("persisted node set does not match graph")
        if any(item.node_id != node_id for node_id, item in self.nodes.items()):
            raise ValueError("persisted node key/id mismatch")
        if graph_digest(self.graph) != self.graph_hash:
            raise ValueError("persisted graph hash does not match graph payload")
        if capability_digest(self.capability_profile) != self.capability_hash:
            raise ValueError("persisted capability hash does not match profile")
        return self

    def machine_readable(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class DAGNodeStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    node_id: str
    persisted_status: str
    effective_status: str
    resume_action: DAGResumeAction
    evidence_valid: bool | None = None
    reason: str | None = None
    report: NodeReport | None = None


class DAGRunStatusReport(BaseModel):
    """Stable machine-readable status and resume decision report."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "1.0"
    run_id: str
    state: DAGRunState
    graph_hash: str
    workspace_root: str
    workspace_id: str
    workspace_fingerprint: str
    current_workspace_fingerprint: str
    capability_hash: str
    attempt: int
    updated_at: datetime
    nodes: dict[str, DAGNodeStatus]
    resumable: bool
    blockers: tuple[str, ...] = ()
    total_tokens_used: int = Field(default=0, ge=0)

    def machine_readable(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class DAGRecoveryItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    action_id: str
    action_state: str
    effect_kind: str
    recommendation: str
    reason: str


class DAGRecoveryBlockedReport(BaseModel):
    """Machine protocol emitted before provider initialization is attempted."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "1.0"
    kind: str = "dag_recovery_preflight"
    status: str = "recovery_blocked"
    workspace_id: str
    items: tuple[DAGRecoveryItem, ...]
    automatic_replay: bool = False

    def machine_readable(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class DAGResumePlan(BaseModel):
    """Internal/publicly inspectable plan produced before any executor starts."""

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    record: DAGRunRecord
    status: DAGRunStatusReport
    initial_reports: dict[str, NodeReport]
    initial_artifacts: dict[str, tuple[ArtifactRef, ...]]
    initial_accepted_changes: tuple[ChangeEnvelope, ...]
    lease_generations: dict[str, int]


def graph_digest(graph: TaskGraph) -> str:
    payload = json.dumps(
        graph.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _provider_boundary(provider: Any) -> dict[str, Any]:
    parsed = urlsplit(str(getattr(provider, "base_url", "")))
    endpoint = {
        "scheme": parsed.scheme.casefold(),
        "host": (parsed.hostname or "").casefold(),
        "port": parsed.port,
    }
    return {
        "name": str(getattr(provider, "name", "")),
        "protocol": str(getattr(provider, "protocol", "")),
        "model": str(getattr(provider, "model", "")),
        "endpoint": endpoint,
    }


def capability_profile_for(
    graph: TaskGraph, permission_mode: Any, provider: Any
) -> dict[str, Any]:
    mode = getattr(permission_mode, "value", permission_mode)
    return {
        "permission_mode": str(mode),
        "provider": _provider_boundary(provider),
        "nodes": {
            node.node_id: {
                "role": node.role.value,
                "read_only": node.role.read_only,
                "allowed_write_set": list(
                    () if node.role.read_only else node.predicted_write_set
                ),
                # The current DAG composition disables command/network tools
                # for read-only roles and does not grant an unreviewed shell
                # manifest to writable roles.
                "command_policy": "deny" if node.role.read_only else "registry_limited",
                "network_policy": "deny" if node.role.read_only else "registry_limited",
            }
            for node in graph.nodes
        },
    }


def capability_digest(profile: dict[str, Any]) -> str:
    payload = json.dumps(
        profile, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    if path.is_symlink():
        digest.update(b"symlink\x00")
        digest.update(os.readlink(path).encode("utf-8", errors="surrogateescape"))
        return digest.hexdigest()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        digest.update(b"<unreadable>")
    return digest.hexdigest()


def workspace_fingerprint(
    workspace: str | os.PathLike[str],
    *,
    exclude_roots: tuple[str | os.PathLike[str], ...] = (),
) -> str:
    """Hash workspace file identities without writing inside the workspace."""

    root = Path(workspace).expanduser().resolve(strict=False)
    excluded: tuple[Path, ...] = tuple(
        path
        for raw in exclude_roots
        for path in (Path(raw).expanduser().resolve(strict=False),)
        if path != root and _is_within(path, root)
    )
    digest = hashlib.sha256()
    if not root.exists():
        digest.update(b"<missing-workspace>")
        return digest.hexdigest()
    for current, directories, names in os.walk(root, followlinks=False):
        directories[:] = sorted(
            name
            for name in directories
            if name not in _IGNORED_WORKSPACE_PARTS
            and not any(
                _is_within((Path(current) / name).resolve(strict=False), ignored)
                for ignored in excluded
            )
        )
        base = Path(current)
        for name in sorted(names):
            path = base / name
            if any(
                _is_within(path.resolve(strict=False), ignored)
                for ignored in excluded
            ):
                continue
            relative = path.relative_to(root).as_posix()
            if any(part in _IGNORED_WORKSPACE_PARTS for part in Path(relative).parts):
                continue
            digest.update(relative.encode("utf-8", errors="surrogateescape"))
            digest.update(b"\x00")
            digest.update(_hash_file(path).encode("ascii"))
            digest.update(b"\n")
    return digest.hexdigest()


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def new_dag_run_id() -> str:
    import uuid

    return f"dag_{uuid.uuid4().hex}"


def dag_recovery_preflight(
    *,
    workspace: str | os.PathLike[str] = ".",
    control_root: str | os.PathLike[str] | None = None,
) -> DAGRecoveryBlockedReport | None:
    """Surface unresolved durable actions without replaying any action.

    ``scan_recovery`` may classify an interrupted action from its already
    committed journal/postcondition, but it never calls the original tool or
    repeats an external side effect.  A missing runtime database is a clean
    preflight and is not created merely to answer this query.
    """

    from mewcode.recovery import RecoveryStore

    workspace_root = Path(workspace).expanduser().resolve(strict=False)
    workspace_id = workspace_id_for(workspace_root)
    paths = ControlPlanePaths.build(
        control_root=control_root, workspace_id=workspace_id
    )
    if not paths.database.exists():
        return None
    recovery = RecoveryStore(database_path=paths.database)
    try:
        report = recovery.scan_recovery()
    finally:
        recovery.close()
    if not report.items:
        return None
    return DAGRecoveryBlockedReport(
        workspace_id=workspace_id,
        items=tuple(
            DAGRecoveryItem(
                action_id=item.action.action_id,
                action_state=item.action.state.value,
                effect_kind=item.action.effect_kind.value,
                recommendation=item.recommendation,
                reason=item.reason,
            )
            for item in report.items
        ),
    )


class DAGRunStore:
    """Atomic JSON store rooted in the host control plane."""

    def __init__(
        self,
        workspace: str | os.PathLike[str],
        *,
        control_root: str | os.PathLike[str] | None = None,
    ) -> None:
        self.workspace = Path(workspace).expanduser().resolve(strict=False)
        self.control_root = resolve_control_root(control_root)
        self.workspace_id = workspace_id_for(self.workspace)
        self.run_root = (
            self.control_root / "workspaces" / self.workspace_id / "dag" / "runs"
        )
        self.artifact_root = (
            self.control_root / "workspaces" / self.workspace_id / "artifacts" / "dag"
        )

    def path_for(self, run_id: str) -> Path:
        if not _RUN_ID_RE.fullmatch(run_id):
            raise DAGPersistenceError("invalid DAG run id")
        return self.run_root / f"{run_id}.json"

    def create(
        self,
        *,
        run_id: str,
        graph: TaskGraph,
        capability_profile: dict[str, Any],
        budget: ScheduleBudget,
        max_concurrency: int,
    ) -> DAGRunRecord:
        path = self.path_for(run_id)
        if path.exists():
            raise DAGPersistenceError(f"DAG run already exists: {run_id}")
        record = DAGRunRecord(
            run_id=run_id,
            graph_hash=graph_digest(graph),
            graph=graph,
            workspace_root=str(self.workspace),
            workspace_id=self.workspace_id,
            workspace_fingerprint=workspace_fingerprint(
                self.workspace, exclude_roots=(self.control_root,)
            ),
            capability_hash=capability_digest(capability_profile),
            capability_profile=capability_profile,
            budget=budget,
            max_concurrency=max_concurrency,
            nodes={
                node.node_id: PersistedDAGNode(node_id=node.node_id)
                for node in graph.nodes
            },
        )
        self.save(record)
        return record

    def load(self, run_id: str) -> DAGRunRecord:
        path = self.path_for(run_id)
        try:
            return DAGRunRecord.model_validate_json(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise DAGPersistenceError(f"DAG run not found: {run_id}") from exc
        except Exception as exc:
            raise DAGPersistenceError(f"invalid DAG run record {run_id}: {exc}") from exc

    def save(self, record: DAGRunRecord) -> None:
        path = self.path_for(record.run_id)
        self.run_root.mkdir(parents=True, exist_ok=True)
        descriptor, raw_temporary = tempfile.mkstemp(
            prefix=f".{record.run_id}.", suffix=".tmp", dir=self.run_root
        )
        temporary = Path(raw_temporary)
        try:
            payload = json.dumps(
                record.machine_readable(),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.chmod(temporary, 0o600)
            except OSError:
                pass
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    @contextmanager
    def resume_guard(self, run_id: str) -> Iterator[None]:
        """Fence concurrent resume attempts with a host-owned atomic lease.

        A guard left behind by process death is intentionally not guessed
        stale.  An operator must inspect/reconcile it; deleting it
        automatically could launch a second writer while the first survives.
        """

        record_path = self.path_for(run_id)
        if not record_path.exists():
            raise DAGPersistenceError(f"DAG run not found: {run_id}")
        self.run_root.mkdir(parents=True, exist_ok=True)
        guard = self.run_root / f"{run_id}.resume.lock"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        try:
            descriptor = os.open(guard, flags, 0o600)
        except FileExistsError as exc:
            raise DAGPersistenceError(
                "DAG resume blocked: concurrent_or_unreconciled_resume_attempt"
            ) from exc
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(
                    {
                        "schema_version": "1.0",
                        "run_id": run_id,
                        "pid": os.getpid(),
                        "acquired_at": datetime.now(timezone.utc).isoformat(),
                    },
                    stream,
                    sort_keys=True,
                )
                stream.flush()
                os.fsync(stream.fileno())
            yield
        finally:
            guard.unlink(missing_ok=True)

    def record_progress(self, event: DAGProgressEvent) -> None:
        record = self.load(event.run_id)
        nodes = dict(record.nodes)
        if event.node_id is not None and event.node_id in nodes:
            previous = nodes[event.node_id]
            if event.type is DAGProgressEventType.NODE_STARTED:
                observed = NodeStatus.RUNNING.value
                note = event.message
            elif event.type in {
                DAGProgressEventType.NODE_COMPLETED,
                DAGProgressEventType.NODE_FAILED,
            }:
                # A progress event deliberately is not completion evidence. If
                # the process dies before finalize(), this remains uncertain
                # and is never replayed automatically.
                observed = "uncertain"
                note = "terminal event observed but final evidence was not committed"
            else:
                observed = previous.observed_status
                note = previous.note
            nodes[event.node_id] = PersistedDAGNode(
                node_id=event.node_id,
                observed_status=observed,
                report=previous.report,
                note=note,
            )
        self.save(
            record.model_copy(
                update={
                    "nodes": nodes,
                    "last_budget": event.budget,
                    "updated_at": datetime.now(timezone.utc),
                }
            )
        )

    def finalize(self, report: ScheduleReport) -> DAGRunRecord:
        record = self.load(report.run_id)
        state = DAGRunState(report.status.value)
        nodes = {
            node_id: PersistedDAGNode(
                node_id=node_id,
                observed_status=node_report.status.value,
                report=node_report,
                note=node_report.failure_reason,
            )
            for node_id, node_report in report.nodes.items()
        }
        updated = record.model_copy(
            update={
                "state": state,
                "workspace_fingerprint": workspace_fingerprint(
                    self.workspace, exclude_roots=(self.control_root,)
                ),
                "nodes": nodes,
                "accepted_changes": report.accepted_changes,
                "metrics": report.metrics,
                "updated_at": datetime.now(timezone.utc),
                "interruption": None,
            }
        )
        self.save(updated)
        return updated

    def mark_interrupted(self, run_id: str, reason: str) -> None:
        try:
            record = self.load(run_id)
        except DAGPersistenceError:
            return
        nodes = {
            node_id: (
                node.model_copy(
                    update={
                        "observed_status": "uncertain",
                        "note": "process ended while node execution was in flight",
                    }
                )
                if node.observed_status == NodeStatus.RUNNING.value
                else node
            )
            for node_id, node in record.nodes.items()
        }
        self.save(
            record.model_copy(
                update={
                    "state": DAGRunState.INTERRUPTED,
                    "nodes": nodes,
                    "interruption": reason[:500],
                    "updated_at": datetime.now(timezone.utc),
                }
            )
        )

    def begin_resume(self, plan: DAGResumePlan) -> DAGRunRecord:
        record = plan.record
        nodes: dict[str, PersistedDAGNode] = {}
        for node_id, decision in plan.status.nodes.items():
            if decision.resume_action is DAGResumeAction.SKIP_VERIFIED:
                nodes[node_id] = record.nodes[node_id]
            else:
                nodes[node_id] = PersistedDAGNode(
                    node_id=node_id,
                    observed_status=NodeStatus.PENDING.value,
                    note="scheduled by conservative resume plan",
                )
        updated = record.model_copy(
            update={
                "state": DAGRunState.RUNNING,
                "attempt": record.attempt + 1,
                "nodes": nodes,
                "updated_at": datetime.now(timezone.utc),
                "interruption": None,
            }
        )
        self.save(updated)
        return updated


def _receipt_evidence_valid(graph: TaskGraph, report: NodeReport) -> tuple[bool, str | None]:
    node = graph.by_id[report.node_id]
    receipts = {item.criterion_id: item for item in report.acceptance_receipts}
    for criterion in node.acceptance_criteria:
        if not criterion.blocking:
            continue
        receipt = receipts.get(criterion.criterion_id)
        if receipt is None or receipt.status is not AcceptanceStatus.PASS:
            return False, f"blocking acceptance receipt missing/not PASS: {criterion.criterion_id}"
        expected = hashlib.sha256(
            json.dumps(
                list(criterion.verifier_argv),
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        if receipt.verifier_argv_sha256 != expected:
            return False, f"acceptance command changed: {criterion.criterion_id}"
        if receipt.verifier_cwd != criterion.verifier_cwd:
            return False, f"acceptance cwd changed: {criterion.criterion_id}"
    return True, None


def _change_evidence_valid(
    workspace: Path,
    artifacts: ArtifactStore,
    change: ChangeEnvelope,
) -> tuple[bool, str | None]:
    if not change.patch_ref:
        return False, "accepted workspace change has no host manifest"
    metadata = dict(change.metadata)
    reference = ArtifactRef(
        name=f"{change.node_id}.change-manifest",
        uri=change.patch_ref,
        digest=metadata.get("change_manifest_sha256") or None,
        media_type="application/json",
    )
    try:
        payload = json.loads(artifacts.read_bytes(reference))
    except (ArtifactStoreError, json.JSONDecodeError) as exc:
        return False, f"workspace change manifest is unavailable/corrupt: {exc}"
    if payload.get("node_id") != change.node_id:
        return False, "workspace change manifest node mismatch"
    declared = set(change.write_set)
    entries = payload.get("changes")
    if not isinstance(entries, list) or {
        item.get("path") for item in entries if isinstance(item, dict)
    } != declared:
        return False, "workspace change manifest paths do not match accepted change"
    for item in entries:
        relative = item.get("path")
        if not isinstance(relative, str):
            return False, "workspace change manifest contains an invalid path"
        target = (workspace / relative).resolve(strict=False)
        try:
            target.relative_to(workspace)
        except ValueError:
            return False, "workspace change manifest escapes workspace"
        expected = item.get("after_sha256")
        if expected is None:
            if target.exists() or target.is_symlink():
                return False, f"deleted output exists again: {relative}"
        elif not (target.exists() or target.is_symlink()):
            return False, f"accepted output is missing: {relative}"
        elif _hash_file(target) != expected:
            return False, f"accepted output drifted: {relative}"
    return True, None


def _success_evidence(
    record: DAGRunRecord,
    node_id: str,
    *,
    workspace: Path,
    artifacts: ArtifactStore,
) -> tuple[bool, str | None]:
    persisted = record.nodes[node_id]
    report = persisted.report
    if report is None or report.status is not NodeStatus.SUCCEEDED:
        return False, "successful node lacks a committed terminal report"
    if not report.artifact_refs:
        return False, "successful node has no host artifact evidence"
    try:
        for artifact in report.artifact_refs:
            artifacts.read_bytes(artifact)
    except ArtifactStoreError as exc:
        return False, f"artifact evidence is unavailable/corrupt: {exc}"
    valid, reason = _receipt_evidence_valid(record.graph, report)
    if not valid:
        return valid, reason
    change = next(
        (item for item in record.accepted_changes if item.node_id == node_id), None
    )
    if change is not None:
        return _change_evidence_valid(workspace, artifacts, change)
    return True, None


def dag_run_status(
    run_id: str,
    *,
    workspace: str | os.PathLike[str] = ".",
    control_root: str | os.PathLike[str] | None = None,
    _ignore_resume_guard: bool = False,
) -> DAGRunStatusReport:
    """Return a conservative assessment without invoking a DAG node/tool."""

    store = DAGRunStore(workspace, control_root=control_root)
    record = store.load(run_id)
    current_workspace = Path(workspace).expanduser().resolve(strict=False)
    current_fingerprint = workspace_fingerprint(
        current_workspace, exclude_roots=(store.control_root,)
    )
    blockers: list[str] = []
    if (
        str(current_workspace) != record.workspace_root
        or store.workspace_id != record.workspace_id
    ):
        blockers.append("workspace_boundary_mismatch")
    if current_fingerprint != record.workspace_fingerprint:
        blockers.append("workspace_fingerprint_mismatch")
    if (
        not _ignore_resume_guard
        and (store.run_root / f"{run_id}.resume.lock").exists()
    ):
        blockers.append("concurrent_or_unreconciled_resume_attempt")
    artifacts = ArtifactStore(store.artifact_root)
    decisions: dict[str, DAGNodeStatus] = {}
    for node in record.graph.nodes:
        persisted = record.nodes[node.node_id]
        report = persisted.report
        observed = persisted.observed_status
        if observed == NodeStatus.SUCCEEDED.value:
            valid, reason = _success_evidence(
                record,
                node.node_id,
                workspace=current_workspace,
                artifacts=artifacts,
            )
            if valid:
                action = DAGResumeAction.SKIP_VERIFIED
                effective = NodeStatus.SUCCEEDED.value
            elif node.role.read_only:
                action = DAGResumeAction.RETRY_SAFE
                effective = "evidence_invalid"
            else:
                action = DAGResumeAction.BLOCKED
                effective = "evidence_invalid"
                blockers.append(f"{node.node_id}:write_success_evidence_invalid")
            decisions[node.node_id] = DAGNodeStatus(
                node_id=node.node_id,
                persisted_status=observed,
                effective_status=effective,
                resume_action=action,
                evidence_valid=valid,
                reason=reason,
                report=report,
            )
            continue
        if observed in {NodeStatus.RUNNING.value, "uncertain"}:
            action = DAGResumeAction.BLOCKED
            effective = "uncertain"
            reason = persisted.note or "node may have uncommitted external side effects"
            blockers.append(f"{node.node_id}:uncertain_side_effect")
        elif observed == NodeStatus.PENDING.value:
            action = DAGResumeAction.RETRY_SAFE
            effective = observed
            reason = persisted.note
        elif report is not None and report.generation == 0:
            # Scheduler-only terminal states (dependency/budget rejection) did
            # not dispatch an executor and therefore have no node side effect.
            action = DAGResumeAction.RETRY_SAFE
            effective = observed
            reason = report.failure_reason
        elif report is not None and report.status in {
            NodeStatus.POLICY_VIOLATION,
            NodeStatus.STALE_CHANGE_REJECTED,
        }:
            action = DAGResumeAction.BLOCKED
            effective = observed
            reason = "policy/change evidence indicates a possible side effect"
            blockers.append(f"{node.node_id}:side_effect_policy_failure")
        elif node.role.read_only:
            action = DAGResumeAction.RETRY_SAFE
            effective = observed
            reason = report.failure_reason if report is not None else persisted.note
        else:
            action = DAGResumeAction.BLOCKED
            effective = observed
            reason = (
                "writable node was dispatched; external/partial side effects require "
                "human reconciliation before a new run"
            )
            blockers.append(f"{node.node_id}:writable_failure_requires_review")
        decisions[node.node_id] = DAGNodeStatus(
            node_id=node.node_id,
            persisted_status=observed,
            effective_status=effective,
            resume_action=action,
            evidence_valid=None,
            reason=reason,
            report=report,
        )
    total_tokens = sum(
        item.report.tokens_used for item in record.nodes.values() if item.report is not None
    )
    unique_blockers = tuple(dict.fromkeys(blockers))
    return DAGRunStatusReport(
        run_id=record.run_id,
        state=record.state,
        graph_hash=record.graph_hash,
        workspace_root=record.workspace_root,
        workspace_id=record.workspace_id,
        workspace_fingerprint=record.workspace_fingerprint,
        current_workspace_fingerprint=current_fingerprint,
        capability_hash=record.capability_hash,
        attempt=record.attempt,
        updated_at=record.updated_at,
        nodes=decisions,
        resumable=not unique_blockers,
        blockers=unique_blockers,
        total_tokens_used=total_tokens,
    )


def build_resume_plan(
    run_id: str,
    graph: TaskGraph,
    *,
    workspace: str | os.PathLike[str],
    permission_mode: Any,
    provider: Any,
    control_root: str | os.PathLike[str] | None = None,
) -> DAGResumePlan:
    store = DAGRunStore(workspace, control_root=control_root)
    record = store.load(run_id)
    blockers: list[str] = []
    if graph_digest(graph) != record.graph_hash:
        blockers.append("graph_hash_mismatch")
    profile = capability_profile_for(graph, permission_mode, provider)
    if capability_digest(profile) != record.capability_hash:
        blockers.append("capability_boundary_mismatch")
    status = dag_run_status(
        run_id,
        workspace=workspace,
        control_root=control_root,
        _ignore_resume_guard=True,
    )
    blockers.extend(status.blockers)
    if blockers:
        raise DAGPersistenceError(
            "DAG resume blocked: " + ", ".join(dict.fromkeys(blockers))
        )
    initial_reports: dict[str, NodeReport] = {}
    initial_artifacts: dict[str, tuple[ArtifactRef, ...]] = {}
    for node_id, decision in status.nodes.items():
        if decision.resume_action is DAGResumeAction.SKIP_VERIFIED:
            if decision.report is None:  # pragma: no cover - status guarantees this
                raise DAGPersistenceError(f"verified node lacks report: {node_id}")
            initial_reports[node_id] = decision.report
            initial_artifacts[node_id] = decision.report.artifact_refs
    return DAGResumePlan(
        record=record,
        status=status,
        initial_reports=initial_reports,
        initial_artifacts=initial_artifacts,
        initial_accepted_changes=tuple(
            change
            for change in record.accepted_changes
            if change.node_id in initial_reports
        ),
        lease_generations={
            node_id: persisted.report.generation
            for node_id, persisted in record.nodes.items()
            if persisted.report is not None
        },
    )


__all__ = [
    "DAGNodeStatus",
    "DAGPersistenceError",
    "DAGRecoveryBlockedReport",
    "DAGRecoveryItem",
    "DAGResumeAction",
    "DAGResumePlan",
    "DAGRunRecord",
    "DAGRunState",
    "DAGRunStatusReport",
    "DAGRunStore",
    "PersistedDAGNode",
    "build_resume_plan",
    "capability_digest",
    "capability_profile_for",
    "dag_recovery_preflight",
    "dag_run_status",
    "graph_digest",
    "new_dag_run_id",
    "workspace_fingerprint",
]
