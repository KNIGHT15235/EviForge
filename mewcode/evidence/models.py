"""Typed contracts for the evidence-first verification harness.

The models in this module deliberately contain no agent or UI state.  They are
stable, serialisable records that can be produced by a headless verifier and
checked later without trusting an LLM's completion message.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def utc_now() -> datetime:
    """Return a timezone-aware UTC timestamp."""

    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


class EvidenceModel(BaseModel):
    """Base class used for durable evidence records.

    Unknown fields are rejected so a misspelled acceptance criterion cannot be
    silently ignored.  Records are immutable after validation.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)


class CriterionStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"
    NOT_RUN = "NOT_RUN"


class GateVerdict(StrEnum):
    PASS = "PASS"
    PARTIAL = "PARTIAL"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"


class CommandStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"


class VerifierSpec(EvidenceModel):
    """A pre-declared verifier command.

    ``argv`` is an argument vector, never a shell command string.  Metacharacters
    therefore remain literal arguments when executed by :class:`TrustedArgvRunner`.
    """

    verifier_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")
    name: str = Field(min_length=1, max_length=256)
    argv: tuple[str, ...] = Field(min_length=1)
    cwd: str = "."
    timeout_seconds: float = Field(default=300.0, gt=0, le=3600)
    expected_exit_codes: frozenset[int] = Field(default_factory=lambda: frozenset({0}))
    env: dict[str, str] = Field(default_factory=dict)

    @field_validator("argv")
    @classmethod
    def validate_argv(cls, argv: tuple[str, ...]) -> tuple[str, ...]:
        if not argv[0].strip():
            raise ValueError("argv[0] must name an executable")
        if any("\x00" in item for item in argv):
            raise ValueError("argv must not contain NUL bytes")
        return argv

    @field_validator("cwd")
    @classmethod
    def validate_cwd(cls, cwd: str) -> str:
        if not cwd or "\x00" in cwd:
            raise ValueError("cwd must be a non-empty path without NUL bytes")
        return cwd

    @field_validator("env")
    @classmethod
    def validate_env(cls, env: dict[str, str]) -> dict[str, str]:
        for key, value in env.items():
            if not key or "=" in key or "\x00" in key or "\x00" in value:
                raise ValueError("environment overrides contain an invalid name or value")
        return env

    @field_validator("expected_exit_codes")
    @classmethod
    def validate_expected_exit_codes(cls, codes: frozenset[int]) -> frozenset[int]:
        if not codes:
            raise ValueError("expected_exit_codes must not be empty")
        return codes


class RequirementCriterion(EvidenceModel):
    criterion_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")
    description: str = Field(min_length=1, max_length=2000)
    required: bool = True
    verifier_ids: tuple[str, ...] = Field(default_factory=tuple)

    @field_validator("verifier_ids")
    @classmethod
    def validate_verifier_ids(cls, verifier_ids: tuple[str, ...]) -> tuple[str, ...]:
        if len(verifier_ids) != len(set(verifier_ids)):
            raise ValueError("criterion verifier_ids must be unique")
        return verifier_ids


class RequirementContract(EvidenceModel):
    """Machine-readable definition of done for one task."""

    schema_version: str = "1.0"
    contract_id: str = Field(
        default_factory=lambda: new_id("contract"),
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9_.-]+$",
    )
    task_id: str = Field(min_length=1, max_length=256)
    objective: str = Field(min_length=1, max_length=10_000)
    base_commit: str | None = None
    criteria: tuple[RequirementCriterion, ...] = Field(min_length=1)
    verifiers: tuple[VerifierSpec, ...] = Field(default_factory=tuple)
    created_at: datetime = Field(default_factory=utc_now)

    @field_validator("base_commit")
    @classmethod
    def validate_base_commit(cls, value: str | None) -> str | None:
        if value is not None and (not value.strip() or value.startswith("-") or "\x00" in value):
            raise ValueError("base_commit must be a safe git commit-ish")
        return value

    @model_validator(mode="after")
    def validate_graph(self) -> Self:
        criterion_ids = [item.criterion_id for item in self.criteria]
        verifier_ids = [item.verifier_id for item in self.verifiers]
        if len(criterion_ids) != len(set(criterion_ids)):
            raise ValueError("criterion_id values must be unique")
        if len(verifier_ids) != len(set(verifier_ids)):
            raise ValueError("verifier_id values must be unique")
        known_verifiers = set(verifier_ids)
        for criterion in self.criteria:
            unknown = set(criterion.verifier_ids) - known_verifiers
            if unknown:
                raise ValueError(
                    f"criterion {criterion.criterion_id!r} references unknown verifiers: "
                    f"{sorted(unknown)}"
                )
        return self


class ArtifactRef(EvidenceModel):
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)
    relative_path: str
    media_type: str = "application/octet-stream"


class WorkspaceSnapshot(EvidenceModel):
    schema_version: str = "1.0"
    repo_root: str
    base_ref: str
    base_commit: str = Field(pattern=r"^[0-9a-fA-F]{40,64}$")
    head_commit: str = Field(pattern=r"^[0-9a-fA-F]{40,64}$")
    tracked_diff_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    untracked_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    diff_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    tracked_files: tuple[str, ...] = Field(default_factory=tuple)
    untracked_files: tuple[str, ...] = Field(default_factory=tuple)
    captured_at: datetime = Field(default_factory=utc_now)

    @property
    def changed_files(self) -> tuple[str, ...]:
        return tuple(sorted(set(self.tracked_files) | set(self.untracked_files)))


class CommandEvidence(EvidenceModel):
    evidence_id: str = Field(
        default_factory=lambda: new_id("command"),
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9_.-]+$",
    )
    verifier_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")
    status: CommandStatus
    argv: tuple[str, ...]
    cwd: str
    expected_exit_codes: frozenset[int]
    exit_code: int | None = None
    timed_out: bool = False
    error: str | None = None
    started_at: datetime
    finished_at: datetime
    duration_ms: float = Field(ge=0)
    workspace_diff_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    stdout: ArtifactRef
    stderr: ArtifactRef
    stdout_preview: str = ""
    stderr_preview: str = ""


class CriterionEvidence(EvidenceModel):
    criterion_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")
    status: CriterionStatus
    evidence_ids: tuple[str, ...] = Field(default_factory=tuple)
    reason: str = ""


class VerificationReceipt(EvidenceModel):
    schema_version: str = "1.0"
    receipt_id: str = Field(
        default_factory=lambda: new_id("receipt"),
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9_.-]+$",
    )
    contract_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")
    task_id: str
    workspace_diff_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    workspace_before: WorkspaceSnapshot
    workspace_after: WorkspaceSnapshot
    commands: tuple[CommandEvidence, ...] = Field(default_factory=tuple)
    criteria: tuple[CriterionEvidence, ...]
    generated_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_evidence_graph(self) -> Self:
        command_ids = [item.evidence_id for item in self.commands]
        criterion_ids = [item.criterion_id for item in self.criteria]
        if len(command_ids) != len(set(command_ids)):
            raise ValueError("command evidence_id values must be unique")
        if len(criterion_ids) != len(set(criterion_ids)):
            raise ValueError("criterion evidence values must be unique")
        known_evidence = set(command_ids)
        for criterion in self.criteria:
            unknown = set(criterion.evidence_ids) - known_evidence
            if unknown:
                raise ValueError(
                    f"criterion {criterion.criterion_id!r} references unknown evidence: {sorted(unknown)}"
                )
        if any(item.workspace_diff_sha256 != self.workspace_diff_sha256 for item in self.commands):
            raise ValueError("command evidence must bind to the receipt workspace diff")
        return self


class GateDecision(EvidenceModel):
    schema_version: str = "1.0"
    decision_id: str = Field(
        default_factory=lambda: new_id("gate"),
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9_.-]+$",
    )
    contract_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")
    receipt_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.-]+$")
    verdict: GateVerdict
    current_diff_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    stale_evidence: bool = False
    reasons: tuple[str, ...] = Field(default_factory=tuple)
    criteria: tuple[CriterionEvidence, ...] = Field(default_factory=tuple)
    decided_at: datetime = Field(default_factory=utc_now)


class EvidenceBundle(EvidenceModel):
    """Portable JSON representation exported alongside a Markdown summary."""

    schema_version: str = "1.0"
    bundle_id: str = Field(
        default_factory=lambda: new_id("bundle"),
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9_.-]+$",
    )
    contract: RequirementContract
    snapshot: WorkspaceSnapshot
    receipt: VerificationReceipt | None
    decision: GateDecision
    generated_at: datetime = Field(default_factory=utc_now)
