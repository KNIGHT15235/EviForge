"""SQLite-backed authority for quarantine, promotion, retrieval, and rollback."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from mewcode.evolution.models import (
    AuditEvent,
    CandidateRecord,
    EvolutionState,
    ExperienceCandidate,
    FeedbackOutcome,
    PromotionPolicy,
    RegistrationDisposition,
    RetrievalContext,
    RetrievedSkill,
    RiskLevel,
    SkillManifest,
    SkillFeedback,
    ValidationRecord,
    ValidationSummary,
    canonical_json,
    normalize_signature,
    utc_now,
)
from mewcode.evolution.projection import render_skill_markdown
from mewcode.evolution.sanitizer import CandidateSanitizer
from mewcode.evolution.state import InvalidEvolutionTransition, require_transition


class EvolutionRegistryError(RuntimeError):
    pass


class CandidateNotPromotableError(EvolutionRegistryError):
    pass


class PromotionGateError(EvolutionRegistryError):
    pass


class DuplicateRecordError(EvolutionRegistryError):
    pass


class SkillNotFoundError(EvolutionRegistryError):
    pass


_SCHEMA = """
CREATE TABLE IF NOT EXISTS evolution_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS candidates (
    candidate_id TEXT PRIMARY KEY,
    signature_key TEXT NOT NULL,
    material_hash TEXT NOT NULL,
    disposition TEXT NOT NULL,
    eligible INTEGER NOT NULL CHECK (eligible IN (0, 1)),
    duplicate_of TEXT,
    conflicts_json TEXT NOT NULL,
    blocked_reasons_json TEXT NOT NULL,
    candidate_json TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_candidates_signature
    ON candidates(signature_key, material_hash);

CREATE TABLE IF NOT EXISTS skill_versions (
    skill_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version > 0),
    candidate_ids_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    signature_key TEXT NOT NULL,
    current_revision INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(skill_id, version)
);

CREATE TABLE IF NOT EXISTS skill_revisions (
    revision INTEGER PRIMARY KEY AUTOINCREMENT,
    skill_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    rollout_state TEXT NOT NULL,
    manifest_hash TEXT NOT NULL UNIQUE,
    manifest_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(skill_id, version) REFERENCES skill_versions(skill_id, version)
        DEFERRABLE INITIALLY DEFERRED
);
CREATE INDEX IF NOT EXISTS idx_skill_revisions_version
    ON skill_revisions(skill_id, version, revision);

CREATE TABLE IF NOT EXISTS validation_records (
    skill_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    validation_id TEXT NOT NULL,
    validation_group TEXT NOT NULL,
    repository_fingerprint TEXT NOT NULL,
    run_id TEXT NOT NULL,
    record_json TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    PRIMARY KEY(skill_id, version, validation_id),
    UNIQUE(skill_id, version, run_id),
    FOREIGN KEY(skill_id, version) REFERENCES skill_versions(skill_id, version)
);
CREATE INDEX IF NOT EXISTS idx_validations_group
    ON validation_records(skill_id, version, validation_group);

CREATE TABLE IF NOT EXISTS active_pointers (
    skill_id TEXT PRIMARY KEY,
    version INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(skill_id, version) REFERENCES skill_versions(skill_id, version)
);

CREATE TABLE IF NOT EXISTS evolution_audit (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL,
    candidate_id TEXT,
    skill_id TEXT,
    version INTEGER,
    from_state TEXT,
    to_state TEXT,
    actor TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    occurred_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS skill_feedback (
    feedback_id TEXT PRIMARY KEY,
    skill_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    task_id TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('hit', 'help', 'harm')),
    feedback_json TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    FOREIGN KEY(skill_id, version) REFERENCES skill_versions(skill_id, version)
);
CREATE INDEX IF NOT EXISTS idx_skill_feedback_version
    ON skill_feedback(skill_id, version, occurred_at);

CREATE TRIGGER IF NOT EXISTS candidate_no_update
BEFORE UPDATE ON candidates BEGIN
    SELECT RAISE(ABORT, 'candidate records are immutable');
END;
CREATE TRIGGER IF NOT EXISTS candidate_no_delete
BEFORE DELETE ON candidates BEGIN
    SELECT RAISE(ABORT, 'candidate records are immutable');
END;
CREATE TRIGGER IF NOT EXISTS skill_revision_no_update
BEFORE UPDATE ON skill_revisions BEGIN
    SELECT RAISE(ABORT, 'skill revisions are append-only');
END;
CREATE TRIGGER IF NOT EXISTS skill_revision_no_delete
BEFORE DELETE ON skill_revisions BEGIN
    SELECT RAISE(ABORT, 'skill revisions are append-only');
END;
CREATE TRIGGER IF NOT EXISTS validation_no_update
BEFORE UPDATE ON validation_records BEGIN
    SELECT RAISE(ABORT, 'validation records are immutable');
END;
CREATE TRIGGER IF NOT EXISTS validation_no_delete
BEFORE DELETE ON validation_records BEGIN
    SELECT RAISE(ABORT, 'validation records are immutable');
END;
CREATE TRIGGER IF NOT EXISTS audit_no_update
BEFORE UPDATE ON evolution_audit BEGIN
    SELECT RAISE(ABORT, 'audit events are append-only');
END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete
BEFORE DELETE ON evolution_audit BEGIN
    SELECT RAISE(ABORT, 'audit events are append-only');
END;
CREATE TRIGGER IF NOT EXISTS feedback_no_update
BEFORE UPDATE ON skill_feedback BEGIN
    SELECT RAISE(ABORT, 'skill feedback is append-only');
END;
CREATE TRIGGER IF NOT EXISTS feedback_no_delete
BEFORE DELETE ON skill_feedback BEGIN
    SELECT RAISE(ABORT, 'skill feedback is append-only');
END;
"""


def _iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.isoformat(timespec="microseconds")


def _default_database(workspace_id: str) -> Path:
    if not workspace_id or any(
        character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
        for character in workspace_id
    ):
        raise ValueError("workspace_id may contain only letters, digits, '.', '_' and '-'")
    local = os.environ.get("LOCALAPPDATA")
    if local:
        root = Path(local)
    else:
        root = Path.home() / ".local" / "share"
    return (
        root
        / "EviForge"
        / "workspaces"
        / workspace_id
        / "skills"
        / "registry.db"
    )


def _estimate_tokens(text: str) -> int:
    # Conservative local estimate: Unicode/code often exceeds the 4-char rule.
    return max(1, (len(text.encode("utf-8")) + 2) // 3)


def _estimate_manifest_tokens(manifest: SkillManifest) -> int:
    """Reach the small fixed point caused by mirroring manifest_hash in Markdown."""

    estimate = max(1, manifest.estimated_tokens)
    for _ in range(8):
        candidate = manifest if estimate == manifest.estimated_tokens else manifest.evolve(
            estimated_tokens=estimate
        )
        calculated = _estimate_tokens(render_skill_markdown(candidate))
        if calculated == estimate:
            return estimate
        estimate = calculated
    return estimate


def _maximum_group_repository_matching(
    groups: Mapping[str, set[str]],
) -> int:
    """Count validation groups that can be assigned distinct repositories."""

    assigned: dict[str, str] = {}

    def assign(group: str, visited: set[str]) -> bool:
        for repository in sorted(groups[group]):
            if repository in visited:
                continue
            visited.add(repository)
            owner = assigned.get(repository)
            if owner is None or assign(owner, visited):
                assigned[repository] = group
                return True
        return False

    matches = 0
    for group in sorted(groups, key=lambda item: (len(groups[item]), item)):
        if assign(group, set()):
            matches += 1
    return matches


class EvolutionRegistry:
    """Manage evolved skills without trusting repository-controlled files."""

    def __init__(
        self,
        database: str | os.PathLike[str] | None = None,
        *,
        workspace_id: str = "default",
        sanitizer: CandidateSanitizer | None = None,
        synchronous: str = "FULL",
        busy_timeout_ms: int = 5_000,
    ) -> None:
        synchronous = synchronous.upper()
        if synchronous not in {"OFF", "NORMAL", "FULL", "EXTRA"}:
            raise ValueError("invalid SQLite synchronous mode")
        self.database = (
            Path(database) if database is not None else _default_database(workspace_id)
        )
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self._sanitizer = sanitizer or CandidateSanitizer()
        self._lock = threading.RLock()
        self._closed = False
        self._connection = sqlite3.connect(
            self.database,
            isolation_level=None,
            check_same_thread=False,
            timeout=max(0.001, busy_timeout_ms / 1000),
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._connection.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
        mode = self._connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        if str(mode).lower() != "wal":
            self._connection.close()
            raise EvolutionRegistryError(f"SQLite WAL unavailable: {mode}")
        self._connection.execute(f"PRAGMA synchronous={synchronous}")
        self._connection.executescript(_SCHEMA)
        with self._transaction() as connection:
            connection.executemany(
                "INSERT OR REPLACE INTO evolution_meta(key, value) VALUES (?, ?)",
                (("schema_version", "1"), ("journal_mode", "WAL")),
            )

    def __enter__(self) -> EvolutionRegistry:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._connection.close()
                self._closed = True

    def _require_open(self) -> None:
        if self._closed:
            raise EvolutionRegistryError("registry is closed")

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._require_open()
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield self._connection
            except BaseException:
                self._connection.rollback()
                raise
            else:
                self._connection.commit()

    def _audit(
        self,
        connection: sqlite3.Connection,
        *,
        event_type: str,
        actor: str,
        candidate_id: str | None = None,
        skill_id: str | None = None,
        version: int | None = None,
        from_state: EvolutionState | None = None,
        to_state: EvolutionState | None = None,
        payload: dict[str, Any] | None = None,
        occurred_at: datetime | None = None,
    ) -> None:
        when = occurred_at or utc_now()
        connection.execute(
            """
            INSERT INTO evolution_audit(
                event_id, event_type, candidate_id, skill_id, version,
                from_state, to_state, actor, payload_json, occurred_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                uuid.uuid4().hex,
                event_type,
                candidate_id,
                skill_id,
                version,
                from_state.value if from_state else None,
                to_state.value if to_state else None,
                actor,
                canonical_json(payload or {}),
                _iso(when),
            ),
        )

    @staticmethod
    def _candidate_record(row: sqlite3.Row) -> CandidateRecord:
        candidate = ExperienceCandidate.model_validate_json(row["candidate_json"])
        return CandidateRecord(
            candidate=candidate,
            disposition=RegistrationDisposition(row["disposition"]),
            eligible_for_promotion=bool(row["eligible"]),
            duplicate_of=row["duplicate_of"],
            conflicts_with=tuple(json.loads(row["conflicts_json"])),
            blocked_reasons=tuple(json.loads(row["blocked_reasons_json"])),
            recorded_at=datetime.fromisoformat(row["recorded_at"]),
        )

    def get_candidate(self, candidate_id: str) -> CandidateRecord:
        with self._lock:
            self._require_open()
            row = self._connection.execute(
                "SELECT * FROM candidates WHERE candidate_id=?", (candidate_id,)
            ).fetchone()
        if row is None:
            raise KeyError(candidate_id)
        return self._candidate_record(row)

    def register_candidate(
        self,
        candidate: ExperienceCandidate,
        *,
        actor: str = "extractor",
    ) -> CandidateRecord:
        """Persist once, always in quarantine, after poison/duplicate checks."""

        report = self._sanitizer.inspect(candidate)
        when = utc_now()
        with self._transaction() as connection:
            existing_id = connection.execute(
                "SELECT * FROM candidates WHERE candidate_id=?",
                (candidate.candidate_id,),
            ).fetchone()
            if existing_id is not None:
                current = self._candidate_record(existing_id)
                if current.candidate == candidate:
                    return current
                raise DuplicateRecordError(
                    f"candidate_id already has different content: {candidate.candidate_id}"
                )

            same_signature = connection.execute(
                """
                SELECT candidate_id, material_hash, disposition
                FROM candidates WHERE signature_key=? AND eligible=1
                ORDER BY recorded_at, candidate_id
                """,
                (candidate.signature_key,),
            ).fetchall()
            duplicate = next(
                (
                    row["candidate_id"]
                    for row in same_signature
                    if row["material_hash"] == candidate.material_hash
                ),
                None,
            )
            conflicts = tuple(
                f"candidate:{row['candidate_id']}"
                for row in same_signature
                if row["material_hash"] != candidate.material_hash
            )
            skill_conflicts = connection.execute(
                """
                SELECT skill_id, version FROM skill_versions
                WHERE signature_key=? AND content_hash<>?
                ORDER BY skill_id, version
                """,
                (candidate.signature_key, candidate.material_hash),
            ).fetchall()
            conflicts += tuple(
                f"skill:{row['skill_id']}@{row['version']}" for row in skill_conflicts
            )

            if not report.safe:
                disposition = RegistrationDisposition.BLOCKED
                eligible = False
            elif duplicate is not None:
                disposition = RegistrationDisposition.DUPLICATE
                eligible = False
            elif conflicts:
                disposition = RegistrationDisposition.CONFLICT
                eligible = False
            else:
                disposition = RegistrationDisposition.ADDED
                eligible = not candidate.is_expired(at=when)

            blocked_reasons = report.reasons
            if candidate.is_expired(at=when):
                blocked_reasons = (*blocked_reasons, "expired_at_registration")
                eligible = False

            connection.execute(
                """
                INSERT INTO candidates(
                    candidate_id, signature_key, material_hash, disposition,
                    eligible, duplicate_of, conflicts_json, blocked_reasons_json,
                    candidate_json, recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    candidate.candidate_id,
                    candidate.signature_key,
                    candidate.material_hash,
                    disposition.value,
                    int(eligible),
                    duplicate,
                    canonical_json(conflicts),
                    canonical_json(blocked_reasons),
                    canonical_json(candidate),
                    _iso(when),
                ),
            )
            self._audit(
                connection,
                event_type="experience_candidate_created",
                actor=actor,
                candidate_id=candidate.candidate_id,
                to_state=EvolutionState.QUARANTINE,
                payload={
                    "disposition": disposition.value,
                    "eligible": eligible,
                    "duplicate_of": duplicate,
                    "conflicts": conflicts,
                    "blocked_reasons": blocked_reasons,
                },
                occurred_at=when,
            )
        return self.get_candidate(candidate.candidate_id)

    def _next_version(self, connection: sqlite3.Connection, skill_id: str) -> int:
        row = connection.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 FROM skill_versions WHERE skill_id=?",
            (skill_id,),
        ).fetchone()
        return int(row[0])

    def create_skill(
        self,
        candidate_id: str,
        *,
        skill_id: str,
        name: str,
        description: str,
        promotion_policy: PromotionPolicy,
        risk_level: RiskLevel = RiskLevel.LOW,
        estimated_tokens: int | None = None,
        actor: str = "evolution-engine",
    ) -> SkillManifest:
        record = self.get_candidate(candidate_id)
        candidate = record.candidate
        if not record.eligible_for_promotion:
            raise CandidateNotPromotableError(
                f"candidate {candidate_id} is {record.disposition.value}: "
                f"{', '.join(record.blocked_reasons or record.conflicts_with)}"
            )
        if candidate.is_expired():
            raise CandidateNotPromotableError(f"candidate {candidate_id} is expired")

        when = utc_now()
        with self._transaction() as connection:
            version = self._next_version(connection, skill_id)
            supersedes = version - 1 if version > 1 else None
            provisional = SkillManifest.build(
                skill_id=skill_id,
                version=version,
                name=name,
                description=description,
                candidate_ids=(candidate_id,),
                task_signatures=(candidate.task_signature,),
                failure_signatures=(candidate.failure_signature,),
                decision=candidate.decision,
                procedure=candidate.procedure,
                failed_attempts=candidate.failed_attempts,
                evidence_refs=candidate.evidence_refs,
                source_trace_ids=candidate.source_trace_ids,
                source_commits=(candidate.source_commit,),
                source_code_hashes=(candidate.source_code_hash,),
                scope=candidate.scope,
                risk_level=risk_level,
                confidence_prior=candidate.confidence_prior,
                confidence_lower_bound=0.0,
                expires_at=candidate.expires_at,
                rollout_state=EvolutionState.QUARANTINE,
                promotion_policy=promotion_policy,
                validation_ids=(),
                supersedes_version=supersedes,
                estimated_tokens=estimated_tokens or 1,
                created_at=when,
                updated_at=when,
            )
            computed_estimate = _estimate_manifest_tokens(provisional)
            estimate = max(estimated_tokens or 0, computed_estimate)
            manifest = provisional.evolve(estimated_tokens=estimate)
            settled = _estimate_manifest_tokens(manifest)
            estimate = max(estimate, settled)
            if estimate != manifest.estimated_tokens:
                manifest = manifest.evolve(estimated_tokens=estimate)

            # A candidate signature identifies the problem; material hash is the
            # solution.  Keep both in the immutable version row for conflict scans.
            cursor = connection.execute(
                """
                INSERT INTO skill_revisions(
                    skill_id, version, rollout_state, manifest_hash,
                    manifest_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    skill_id,
                    version,
                    manifest.rollout_state.value,
                    manifest.manifest_hash,
                    canonical_json(manifest),
                    _iso(when),
                ),
            )
            revision = int(cursor.lastrowid)
            connection.execute(
                """
                INSERT INTO skill_versions(
                    skill_id, version, candidate_ids_json, content_hash,
                    signature_key, current_revision, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    skill_id,
                    version,
                    canonical_json((candidate_id,)),
                    candidate.material_hash,
                    candidate.signature_key,
                    revision,
                    _iso(when),
                ),
            )
            self._audit(
                connection,
                event_type="skill_version_created",
                actor=actor,
                candidate_id=candidate_id,
                skill_id=skill_id,
                version=version,
                to_state=EvolutionState.QUARANTINE,
                payload={
                    "manifest_hash": manifest.manifest_hash,
                    "policy_hash": promotion_policy.policy_hash,
                },
                occurred_at=when,
            )
        return manifest

    def get_manifest(self, skill_id: str, version: int | None = None) -> SkillManifest:
        with self._lock:
            self._require_open()
            if version is None:
                row = self._connection.execute(
                    """
                    SELECT r.manifest_json FROM skill_versions v
                    JOIN skill_revisions r ON r.revision=v.current_revision
                    WHERE v.skill_id=? ORDER BY v.version DESC LIMIT 1
                    """,
                    (skill_id,),
                ).fetchone()
            else:
                row = self._connection.execute(
                    """
                    SELECT r.manifest_json FROM skill_versions v
                    JOIN skill_revisions r ON r.revision=v.current_revision
                    WHERE v.skill_id=? AND v.version=?
                    """,
                    (skill_id, version),
                ).fetchone()
        if row is None:
            suffix = "latest" if version is None else str(version)
            raise SkillNotFoundError(f"skill not found: {skill_id}@{suffix}")
        return SkillManifest.model_validate_json(row["manifest_json"])

    def list_versions(self, skill_id: str) -> tuple[SkillManifest, ...]:
        with self._lock:
            self._require_open()
            rows = self._connection.execute(
                """
                SELECT r.manifest_json FROM skill_versions v
                JOIN skill_revisions r ON r.revision=v.current_revision
                WHERE v.skill_id=? ORDER BY v.version
                """,
                (skill_id,),
            ).fetchall()
        return tuple(SkillManifest.model_validate_json(row[0]) for row in rows)

    def revision_history(
        self, skill_id: str, version: int
    ) -> tuple[SkillManifest, ...]:
        """Return every append-only lifecycle revision for forensic review."""

        with self._lock:
            self._require_open()
            rows = self._connection.execute(
                """
                SELECT manifest_json FROM skill_revisions
                WHERE skill_id=? AND version=? ORDER BY revision
                """,
                (skill_id, version),
            ).fetchall()
        if not rows:
            raise SkillNotFoundError(f"skill not found: {skill_id}@{version}")
        return tuple(SkillManifest.model_validate_json(row[0]) for row in rows)

    def add_validation(
        self,
        skill_id: str,
        version: int,
        record: ValidationRecord,
        *,
        actor: str = "verification-runner",
    ) -> ValidationSummary:
        with self._transaction() as connection:
            manifest = self._manifest_from_connection(connection, skill_id, version)
            if record.validation_group not in manifest.promotion_policy.validation_groups:
                raise PromotionGateError(
                    f"validation group was not pre-registered: {record.validation_group}"
                )
            if manifest.rollout_state in {
                EvolutionState.DEPRECATED,
                EvolutionState.ROLLED_BACK,
            }:
                raise PromotionGateError(
                    f"cannot validate a {manifest.rollout_state.value} skill"
                )
            try:
                connection.execute(
                    """
                    INSERT INTO validation_records(
                        skill_id, version, validation_id, validation_group,
                        repository_fingerprint, run_id, record_json, observed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        skill_id,
                        version,
                        record.validation_id,
                        record.validation_group,
                        normalize_signature(record.repository_fingerprint),
                        record.run_id,
                        canonical_json(record),
                        _iso(record.observed_at),
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise DuplicateRecordError(
                    f"duplicate validation id or run: {record.validation_id}/{record.run_id}"
                ) from error
            self._audit(
                connection,
                event_type="skill_validation_recorded",
                actor=actor,
                skill_id=skill_id,
                version=version,
                payload={
                    "validation_id": record.validation_id,
                    "validation_group": record.validation_group,
                    "repository_fingerprint": record.repository_fingerprint,
                    "passed": record.passed,
                    "harm_count": record.harm_count,
                },
                occurred_at=record.observed_at,
            )
            refreshed = manifest.evolve(
                validation_ids=(*manifest.validation_ids, record.validation_id),
                confidence_lower_bound=min(
                    (
                        existing.confidence_lower_bound
                        for existing in self._validation_records(
                            skill_id, version, connection=connection
                        )
                    ),
                    default=0.0,
                ),
                updated_at=utc_now(),
            )
            self._append_revision(connection, refreshed)
        return self.validation_summary(skill_id, version)

    def _validation_records(
        self,
        skill_id: str,
        version: int,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> tuple[ValidationRecord, ...]:
        target = connection or self._connection
        rows = target.execute(
            """
            SELECT record_json FROM validation_records
            WHERE skill_id=? AND version=? ORDER BY observed_at, validation_id
            """,
            (skill_id, version),
        ).fetchall()
        return tuple(ValidationRecord.model_validate_json(row[0]) for row in rows)

    @staticmethod
    def _summarize(
        policy: PromotionPolicy,
        records: Sequence[ValidationRecord],
    ) -> ValidationSummary:
        grouped: dict[str, list[ValidationRecord]] = defaultdict(list)
        for record in records:
            if record.validation_group in policy.validation_groups:
                grouped[record.validation_group].append(record)

        passing: list[str] = []
        passing_repositories: set[str] = set()
        group_repositories: dict[str, set[str]] = {}
        total_harm = sum(record.harm_count for record in records)
        effect_bounds: list[float] = []
        confidence_bounds = [record.confidence_lower_bound for record in records]
        reasons: list[str] = []

        for group in policy.validation_groups:
            observations = grouped.get(group, [])
            if not observations:
                continue
            group_pass_rate = sum(record.passed for record in observations) / len(
                observations
            )
            group_harm = sum(record.harm_count for record in observations)
            group_effect = min(record.effect_lower_bound for record in observations)
            effect_bounds.append(group_effect)
            if (
                group_pass_rate >= policy.minimum_group_pass_rate
                and group_harm <= policy.max_total_harm
                and group_effect >= policy.minimum_effect_lower_bound
            ):
                passing.append(group)
                repositories = {
                    normalize_signature(record.repository_fingerprint)
                    for record in observations
                }
                group_repositories[group] = repositories
                passing_repositories.update(repositories)

        observed = tuple(group for group in policy.validation_groups if group in grouped)
        passing_tuple = tuple(passing)
        pass_rate = len(passing) / len(observed) if observed else 0.0
        distinct_repositories = len(passing_repositories)
        independent_group_count = _maximum_group_repository_matching(
            group_repositories
        )
        global_harm_ok = total_harm <= policy.max_total_harm
        meets_canary = (
            len(passing) >= policy.canary_minimum_groups and global_harm_ok
        )
        meets_active = (
            independent_group_count >= policy.active_minimum_independent_groups
            and global_harm_ok
        )
        if not global_harm_ok:
            reasons.append("harm threshold exceeded")
        if len(passing) < policy.active_minimum_independent_groups:
            reasons.append("insufficient independent validation groups")
        if independent_group_count < policy.active_minimum_independent_groups:
            reasons.append("validation groups are not backed by independent repositories")
        if effect_bounds and min(effect_bounds) < policy.minimum_effect_lower_bound:
            reasons.append("effect lower bound below threshold")
        return ValidationSummary(
            registered_groups=policy.validation_groups,
            observed_groups=observed,
            counted_groups=observed,
            passing_groups=passing_tuple,
            independent_group_count=independent_group_count,
            distinct_repositories=distinct_repositories,
            pass_rate=pass_rate,
            total_harm=total_harm,
            minimum_effect_lower_bound=min(effect_bounds) if effect_bounds else None,
            confidence_lower_bound=min(confidence_bounds) if confidence_bounds else 0.0,
            meets_canary=meets_canary,
            meets_active=meets_active,
            reasons=tuple(reasons),
        )

    def validation_summary(self, skill_id: str, version: int) -> ValidationSummary:
        with self._lock:
            self._require_open()
            manifest = self.get_manifest(skill_id, version)
            records = self._validation_records(skill_id, version)
        return self._summarize(manifest.promotion_policy, records)

    def list_validations(
        self, skill_id: str, version: int
    ) -> tuple[ValidationRecord, ...]:
        """Return immutable replay/validation evidence for operator review.

        Validation rows are append-only.  Exposing them through a public,
        read-only API keeps UI/CLI code away from the registry's private SQL
        helpers while preserving the exact run, repository and Evidence refs
        that governed promotion.
        """

        with self._lock:
            self._require_open()
            # Resolve the manifest first so an unknown version fails with the
            # same stable domain error used by the other public read APIs.
            self.get_manifest(skill_id, version)
            return self._validation_records(skill_id, version)

    def _append_revision(
        self,
        connection: sqlite3.Connection,
        manifest: SkillManifest,
    ) -> int:
        cursor = connection.execute(
            """
            INSERT INTO skill_revisions(
                skill_id, version, rollout_state, manifest_hash,
                manifest_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                manifest.skill_id,
                manifest.version,
                manifest.rollout_state.value,
                manifest.manifest_hash,
                canonical_json(manifest),
                _iso(manifest.updated_at),
            ),
        )
        revision = int(cursor.lastrowid)
        connection.execute(
            """
            UPDATE skill_versions SET current_revision=?
            WHERE skill_id=? AND version=?
            """,
            (revision, manifest.skill_id, manifest.version),
        )
        return revision

    def promote(
        self,
        skill_id: str,
        version: int,
        target: EvolutionState,
        *,
        actor: str = "promotion-engine",
        manual_approval: bool = False,
    ) -> SkillManifest:
        if target not in {EvolutionState.CANARY, EvolutionState.ACTIVE}:
            raise ValueError("promote target must be canary or active")
        manifest = self.get_manifest(skill_id, version)
        require_transition(manifest.rollout_state, target)
        if manifest.is_expired():
            raise PromotionGateError("expired skills cannot be promoted")
        summary = self.validation_summary(skill_id, version)
        if target is EvolutionState.CANARY and not summary.meets_canary:
            raise PromotionGateError("canary evidence threshold not met")
        if target is EvolutionState.ACTIVE and not summary.meets_active:
            raise PromotionGateError("active evidence threshold not met")
        if manifest.risk_level is RiskLevel.HIGH and not manual_approval:
            raise PromotionGateError("high-risk skill promotion requires manual approval")

        when = utc_now()
        updated = manifest.evolve(
            rollout_state=target,
            confidence_lower_bound=summary.confidence_lower_bound,
            updated_at=when,
        )
        with self._transaction() as connection:
            if target is EvolutionState.ACTIVE:
                active = connection.execute(
                    "SELECT version FROM active_pointers WHERE skill_id=?",
                    (skill_id,),
                ).fetchone()
                if active is not None and int(active["version"]) != version:
                    old = self._manifest_from_connection(
                        connection, skill_id, int(active["version"])
                    )
                    if old.rollout_state is EvolutionState.ACTIVE:
                        deprecated = old.evolve(
                            rollout_state=EvolutionState.DEPRECATED,
                            updated_at=when,
                        )
                        self._append_revision(connection, deprecated)
                        self._audit(
                            connection,
                            event_type="skill_superseded",
                            actor=actor,
                            skill_id=skill_id,
                            version=old.version,
                            from_state=old.rollout_state,
                            to_state=EvolutionState.DEPRECATED,
                            payload={"superseded_by": version},
                            occurred_at=when,
                        )
                connection.execute(
                    """
                    INSERT INTO active_pointers(skill_id, version, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(skill_id) DO UPDATE SET
                        version=excluded.version, updated_at=excluded.updated_at
                    """,
                    (skill_id, version, _iso(when)),
                )
            self._append_revision(connection, updated)
            self._audit(
                connection,
                event_type="skill_published",
                actor=actor,
                skill_id=skill_id,
                version=version,
                from_state=manifest.rollout_state,
                to_state=target,
                payload={
                    "policy_hash": manifest.promotion_policy.policy_hash,
                    "validation_summary": summary.model_dump(mode="json"),
                    "manual_approval": manual_approval,
                },
                occurred_at=when,
            )
        return updated

    def _manifest_from_connection(
        self,
        connection: sqlite3.Connection,
        skill_id: str,
        version: int,
    ) -> SkillManifest:
        row = connection.execute(
            """
            SELECT r.manifest_json FROM skill_versions v
            JOIN skill_revisions r ON r.revision=v.current_revision
            WHERE v.skill_id=? AND v.version=?
            """,
            (skill_id, version),
        ).fetchone()
        if row is None:
            raise SkillNotFoundError(f"skill not found: {skill_id}@{version}")
        return SkillManifest.model_validate_json(row[0])

    def deprecate(
        self,
        skill_id: str,
        version: int,
        *,
        actor: str = "operator",
        reason: str = "manual deprecation",
    ) -> SkillManifest:
        if not reason.strip():
            raise ValueError("deprecation reason must not be blank")
        manifest = self.get_manifest(skill_id, version)
        require_transition(manifest.rollout_state, EvolutionState.DEPRECATED)
        when = utc_now()
        updated = manifest.evolve(
            rollout_state=EvolutionState.DEPRECATED, updated_at=when
        )
        with self._transaction() as connection:
            self._append_revision(connection, updated)
            connection.execute(
                "DELETE FROM active_pointers WHERE skill_id=? AND version=?",
                (skill_id, version),
            )
            self._audit(
                connection,
                event_type="skill_deprecated",
                actor=actor,
                skill_id=skill_id,
                version=version,
                from_state=manifest.rollout_state,
                to_state=EvolutionState.DEPRECATED,
                payload={"reason": reason},
                occurred_at=when,
            )
        return updated

    def rollback(
        self,
        skill_id: str,
        version: int,
        *,
        restore_version: int | None = None,
        actor: str = "operator",
        reason: str,
    ) -> tuple[SkillManifest, SkillManifest | None]:
        if not reason.strip():
            raise ValueError("rollback reason must not be blank")
        with self._transaction() as connection:
            bad = self._manifest_from_connection(connection, skill_id, version)
            require_transition(bad.rollout_state, EvolutionState.ROLLED_BACK)
            restored = (
                self._manifest_from_connection(connection, skill_id, restore_version)
                if restore_version is not None
                else None
            )
            if restored is not None:
                if restored.rollout_state is not EvolutionState.DEPRECATED:
                    raise InvalidEvolutionTransition(
                        "rollback can only restore a previously active, deprecated version"
                    )
                was_active = connection.execute(
                    """
                    SELECT 1 FROM skill_revisions
                    WHERE skill_id=? AND version=? AND rollout_state=? LIMIT 1
                    """,
                    (skill_id, restore_version, EvolutionState.ACTIVE.value),
                ).fetchone()
                if was_active is None:
                    raise InvalidEvolutionTransition(
                        "rollback cannot activate a version that was never active"
                    )
                require_transition(
                    restored.rollout_state,
                    EvolutionState.ACTIVE,
                    rollback_restore=True,
                )
                if restored.is_expired():
                    raise PromotionGateError("cannot restore an expired skill version")

            when = utc_now()
            bad_updated = bad.evolve(
                rollout_state=EvolutionState.ROLLED_BACK, updated_at=when
            )
            restored_updated = (
                restored.evolve(rollout_state=EvolutionState.ACTIVE, updated_at=when)
                if restored is not None
                else None
            )
            self._append_revision(connection, bad_updated)
            if restored_updated is None:
                connection.execute(
                    "DELETE FROM active_pointers WHERE skill_id=? AND version=?",
                    (skill_id, version),
                )
            else:
                self._append_revision(connection, restored_updated)
                connection.execute(
                    """
                    INSERT INTO active_pointers(skill_id, version, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(skill_id) DO UPDATE SET
                        version=excluded.version, updated_at=excluded.updated_at
                    """,
                    (skill_id, restored_updated.version, _iso(when)),
                )
            self._audit(
                connection,
                event_type="skill_rolled_back",
                actor=actor,
                skill_id=skill_id,
                version=version,
                from_state=bad.rollout_state,
                to_state=EvolutionState.ROLLED_BACK,
                payload={
                    "reason": reason,
                    "restored_version": restore_version,
                },
                occurred_at=when,
            )
        return bad_updated, restored_updated

    def retrieve_active(
        self,
        *,
        task_signature: str,
        context: RetrievalContext | Mapping[str, Any],
        token_budget: int,
        failure_signature: str | None = None,
        max_results: int = 8,
    ) -> tuple[RetrievedSkill, ...]:
        if token_budget < 0:
            raise ValueError("token_budget must be non-negative")
        if max_results < 0:
            raise ValueError("max_results must be non-negative")
        context = RetrievalContext.model_validate(context)
        task_key = normalize_signature(task_signature)
        failure_key = (
            normalize_signature(failure_signature) if failure_signature else None
        )
        with self._lock:
            self._require_open()
            rows = self._connection.execute(
                """
                SELECT r.manifest_json FROM active_pointers p
                JOIN skill_versions v
                  ON v.skill_id=p.skill_id AND v.version=p.version
                JOIN skill_revisions r ON r.revision=v.current_revision
                ORDER BY p.skill_id
                """
            ).fetchall()

        ranked: list[tuple[float, SkillManifest, str]] = []
        for row in rows:
            manifest = SkillManifest.model_validate_json(row["manifest_json"])
            if manifest.rollout_state is not EvolutionState.ACTIVE:
                continue
            if manifest.is_expired(at=context.at) or not manifest.scope.matches(context):
                continue
            task_match = task_key in {
                normalize_signature(value) for value in manifest.task_signatures
            }
            failure_match = bool(
                failure_key
                and failure_key
                in {
                    normalize_signature(value)
                    for value in manifest.failure_signatures
                }
            )
            if not task_match and not failure_match:
                continue
            score = (
                (100.0 if task_match else 0.0)
                + (50.0 if failure_match else 0.0)
                + manifest.scope.specificity
                + manifest.confidence_lower_bound
            )
            markdown = render_skill_markdown(manifest)
            ranked.append((score, manifest, markdown))
        ranked.sort(key=lambda item: (-item[0], item[1].estimated_tokens, item[1].skill_id))

        remaining = token_budget
        selected: list[RetrievedSkill] = []
        for score, manifest, markdown in ranked:
            if len(selected) >= max_results:
                break
            # Manifest cost is conservatively computed at publication time.
            # Re-checking mutable serialized length here could create a TOCTOU
            # mismatch between selection and the advertised budget contract.
            actual_tokens = manifest.estimated_tokens
            if actual_tokens > remaining:
                continue
            selected.append(
                RetrievedSkill(
                    manifest=manifest,
                    markdown=markdown,
                    token_cost=actual_tokens,
                    rank_score=score,
                )
            )
            remaining -= actual_tokens
        return tuple(selected)

    def audit_events(self) -> tuple[AuditEvent, ...]:
        with self._lock:
            self._require_open()
            rows = self._connection.execute(
                "SELECT * FROM evolution_audit ORDER BY sequence"
            ).fetchall()
        return tuple(
            AuditEvent(
                sequence=row["sequence"],
                event_id=row["event_id"],
                event_type=row["event_type"],
                candidate_id=row["candidate_id"],
                skill_id=row["skill_id"],
                version=row["version"],
                from_state=(
                    EvolutionState(row["from_state"]) if row["from_state"] else None
                ),
                to_state=EvolutionState(row["to_state"]) if row["to_state"] else None,
                actor=row["actor"],
                payload=json.loads(row["payload_json"]),
                occurred_at=datetime.fromisoformat(row["occurred_at"]),
            )
            for row in rows
        )

    def list_candidates(self, *, limit: int | None = None) -> tuple[CandidateRecord, ...]:
        """Return immutable candidates newest first for operator/CLI status views."""

        if limit is not None and limit < 1:
            raise ValueError("limit must be positive")
        sql = "SELECT * FROM candidates ORDER BY recorded_at DESC, candidate_id"
        params: tuple[Any, ...] = ()
        if limit is not None:
            sql += " LIMIT ?"
            params = (limit,)
        with self._lock:
            self._require_open()
            rows = self._connection.execute(sql, params).fetchall()
        return tuple(self._candidate_record(row) for row in rows)

    def list_manifests(self) -> tuple[SkillManifest, ...]:
        """Return the current append-only revision of every skill version."""

        with self._lock:
            self._require_open()
            rows = self._connection.execute(
                """
                SELECT r.manifest_json FROM skill_versions v
                JOIN skill_revisions r ON r.revision=v.current_revision
                ORDER BY v.skill_id, v.version
                """
            ).fetchall()
        return tuple(SkillManifest.model_validate_json(row[0]) for row in rows)

    def record_feedback(
        self,
        feedback: SkillFeedback,
        *,
        rollback_on_harm: bool = True,
    ) -> tuple[SkillManifest, SkillManifest | None] | None:
        """Persist one observed deployment outcome and fail closed on harm.

        The feedback row and audit record commit before a rollback is attempted,
        so an invalid lifecycle cannot erase the evidence.  Only an ACTIVE or
        CANARY version is automatically rolled back; historical feedback stays
        append-only and never resurrects a version.
        """

        manifest = self.get_manifest(feedback.skill_id, feedback.version)
        with self._transaction() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO skill_feedback(
                        feedback_id, skill_id, version, task_id, outcome,
                        feedback_json, occurred_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        feedback.feedback_id,
                        feedback.skill_id,
                        feedback.version,
                        feedback.task_id,
                        feedback.outcome.value,
                        canonical_json(feedback),
                        _iso(feedback.occurred_at),
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise DuplicateRecordError(
                    f"duplicate feedback id: {feedback.feedback_id}"
                ) from error
            self._audit(
                connection,
                event_type=f"skill_{feedback.outcome.value}_recorded",
                actor=feedback.actor,
                skill_id=feedback.skill_id,
                version=feedback.version,
                payload={
                    "feedback_id": feedback.feedback_id,
                    "task_id": feedback.task_id,
                    "evidence_refs": list(feedback.evidence_refs),
                },
                occurred_at=feedback.occurred_at,
            )
        if (
            feedback.outcome is FeedbackOutcome.HARM
            and rollback_on_harm
            and manifest.rollout_state in {EvolutionState.ACTIVE, EvolutionState.CANARY}
        ):
            return self.rollback(
                feedback.skill_id,
                feedback.version,
                actor="automatic-harm-monitor",
                reason=f"harm feedback {feedback.feedback_id}",
            )
        return None

    def list_feedback(
        self,
        *,
        skill_id: str | None = None,
        version: int | None = None,
    ) -> tuple[SkillFeedback, ...]:
        conditions: list[str] = []
        params: list[Any] = []
        if skill_id is not None:
            conditions.append("skill_id=?")
            params.append(skill_id)
        if version is not None:
            conditions.append("version=?")
            params.append(version)
        sql = "SELECT feedback_json FROM skill_feedback"
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += " ORDER BY occurred_at, feedback_id"
        with self._lock:
            self._require_open()
            rows = self._connection.execute(sql, params).fetchall()
        return tuple(SkillFeedback.model_validate_json(row[0]) for row in rows)

    def export_skill(
        self,
        skill_id: str,
        version: int,
        destination: str | os.PathLike[str],
    ) -> tuple[Path, Path]:
        """Write reviewable projections; registry rows remain authoritative."""

        manifest = self.get_manifest(skill_id, version)
        target = Path(destination)
        target.mkdir(parents=True, exist_ok=True)
        manifest_path = target / "manifest.json"
        markdown_path = target / "SKILL.md"
        self._atomic_write(manifest_path, manifest.to_canonical_json() + "\n")
        self._atomic_write(markdown_path, render_skill_markdown(manifest))
        return manifest_path, markdown_path

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_text(content, encoding="utf-8", newline="\n")
        os.replace(temporary, path)


__all__ = [
    "CandidateNotPromotableError",
    "DuplicateRecordError",
    "EvolutionRegistry",
    "EvolutionRegistryError",
    "InvalidEvolutionTransition",
    "PromotionGateError",
    "SkillNotFoundError",
]
