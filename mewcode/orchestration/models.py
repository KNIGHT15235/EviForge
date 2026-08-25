"""Typed contracts for EviForge's budget-aware multi-agent DAG runtime.

The orchestration package deliberately does not depend on the interactive
``Agent`` implementation.  Executors receive an immutable, capability-scoped
envelope and return structured artifacts/changes, which keeps the scheduler
deterministic and makes it possible to benchmark it without an LLM.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


_NODE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def normalize_write_scope(value: str) -> str:
    """Return a stable workspace-relative write scope.

    Scopes are paths or subtree expressions ending in ``/**``.  Rejecting
    absolute paths and traversal is important: a predicted write-set is a
    capability boundary, not merely scheduling metadata.
    """

    scope = value.strip().replace("\\", "/")
    while scope.startswith("./"):
        scope = scope[2:]
    if not scope:
        raise ValueError("write scope must not be blank")
    if scope.startswith("/") or re.match(r"^[A-Za-z]:/", scope):
        raise ValueError("write scope must be workspace-relative")
    if "*" in scope and not scope.endswith("/**"):
        raise ValueError("only a trailing '/**' wildcard is supported")
    path_part = scope[:-3] if scope.endswith("/**") else scope
    parts = [part for part in path_part.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise ValueError("write scope must not traverse outside the workspace")
    normalized = "/".join(parts) or "."
    if scope.endswith("/**"):
        normalized = "./**" if normalized == "." else normalized + "/**"
    return normalized


def scope_contains(container: str, candidate: str) -> bool:
    """Return whether ``container`` authorizes ``candidate``.

    Exact paths authorize exactly one path; only an explicit ``/**`` scope
    authorizes descendants.  This distinction keeps a prediction such as
    ``pyproject.toml`` from silently becoming a subtree capability.
    """

    left = normalize_write_scope(container)
    right = normalize_write_scope(candidate)
    if not left.endswith("/**"):
        return left == right
    left_base = left[:-3]
    right_base = right[:-3] if right.endswith("/**") else right
    return right_base == left_base or right_base.startswith(left_base + "/")


def write_scopes_conflict(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
    """Conservatively detect overlap between two predicted write-sets."""

    return any(
        scope_contains(a, b) or scope_contains(b, a)
        for a in left
        for b in right
    )


class AgentRole(StrEnum):
    EXPLORER = "explorer"
    IMPLEMENTER = "implementer"
    VERIFIER = "verifier"
    INTEGRATOR = "integrator"

    @property
    def read_only(self) -> bool:
        return self in {AgentRole.EXPLORER, AgentRole.VERIFIER}


class ContextPolicy(StrEnum):
    DEPENDENCY_ARTIFACTS_ONLY = "dependency_artifacts_only"
    FRESH_ISOLATED = "fresh_isolated"


class NodeStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    SKIPPED_DEPENDENCY_FAILED = "skipped_dependency_failed"
    CANCELLED = "cancelled"
    BUDGET_EXCEEDED = "budget_exceeded"
    STALE_CHANGE_REJECTED = "stale_change_rejected"
    POLICY_VIOLATION = "policy_violation"

    @property
    def successful(self) -> bool:
        return self is NodeStatus.SUCCEEDED

    @property
    def terminal(self) -> bool:
        return self not in {NodeStatus.PENDING, NodeStatus.RUNNING}


class RunStatus(StrEnum):
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"
    BUDGET_EXCEEDED = "budget_exceeded"


class AcceptanceStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"
    NOT_RUN = "NOT_RUN"


class AcceptanceCriterion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    criterion_id: str
    description: str
    verifier: str = "deterministic"
    verifier_argv: tuple[str, ...] = ()
    verifier_cwd: str = "."
    timeout_seconds: float = Field(default=300.0, gt=0.0, le=3600.0)
    blocking: bool = True

    @field_validator("criterion_id", "description", "verifier")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value

    @field_validator("verifier_argv")
    @classmethod
    def _valid_argv(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value or "\x00" in value for value in values):
            raise ValueError("verifier argv entries must be non-empty and contain no NUL")
        return values

    @field_validator("verifier_cwd")
    @classmethod
    def _valid_cwd(cls, value: str) -> str:
        if not value.strip() or "\x00" in value:
            raise ValueError("verifier_cwd must be a safe relative path")
        from pathlib import PurePath

        path = PurePath(value)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("verifier_cwd must remain inside the workspace")
        return value

    @model_validator(mode="after")
    def _blocking_criterion_has_runner(self) -> "AcceptanceCriterion":
        if self.blocking and self.verifier != "deterministic":
            raise ValueError("blocking acceptance criterion must use deterministic verifier")
        if self.blocking and not self.verifier_argv:
            raise ValueError("blocking acceptance criterion requires verifier_argv")
        return self


class AcceptanceReceipt(BaseModel):
    """Metadata-only receipt produced by a host-owned argv verifier."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    criterion_id: str
    blocking: bool
    status: AcceptanceStatus
    verifier_argv_sha256: str | None = None
    verifier_cwd: str | None = None
    exit_code: int | None = None
    duration_seconds: float = Field(default=0.0, ge=0.0)
    stdout_sha256: str | None = None
    stderr_sha256: str | None = None
    stdout_bytes: int = Field(default=0, ge=0)
    stderr_bytes: int = Field(default=0, ge=0)
    error_class: str | None = None


class ArtifactContract(BaseModel):
    """Names of typed inputs and outputs promised by a node."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    required_inputs: tuple[str, ...] = ()
    required_outputs: tuple[str, ...] = ()

    @field_validator("required_inputs", "required_outputs")
    @classmethod
    def _clean_names(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = tuple(dict.fromkeys(value.strip() for value in values))
        if any(not value for value in cleaned):
            raise ValueError("artifact names must not be blank")
        return cleaned


class ArtifactRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    uri: str
    digest: str | None = None
    media_type: str = "application/octet-stream"
    size_bytes: int | None = Field(default=None, ge=0)

    @field_validator("name", "uri", "media_type")
    @classmethod
    def _artifact_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value


class ArtifactPayload(BaseModel):
    """One explicitly named Agent output before host CAS publication."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    content: str
    media_type: str = "text/plain"

    @field_validator("name", "media_type")
    @classmethod
    def _payload_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value


class ArtifactManifest(BaseModel):
    """Strict final-response protocol for nodes with declared outputs."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    artifacts: tuple[ArtifactPayload, ...]
    summary: str = ""

    @field_validator("artifacts")
    @classmethod
    def _unique_payload_names(
        cls, values: tuple[ArtifactPayload, ...]
    ) -> tuple[ArtifactPayload, ...]:
        names = [item.name for item in values]
        if len(names) != len(set(names)):
            raise ValueError("artifact manifest contains duplicate names")
        return values


class TaskNode(BaseModel):
    """A role-scoped unit of work in a typed task graph."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    node_id: str
    role: AgentRole
    objective: str
    depends_on: tuple[str, ...] = ()
    acceptance_criteria: tuple[AcceptanceCriterion, ...] = ()
    artifact_contract: ArtifactContract = Field(default_factory=ArtifactContract)
    token_budget: int = Field(default=1_000, ge=1)
    timeout_seconds: float = Field(default=60.0, gt=0.0)
    estimated_duration_seconds: float = Field(default=1.0, gt=0.0)
    predicted_write_set: tuple[str, ...] = ()

    @field_validator("node_id")
    @classmethod
    def _valid_node_id(cls, value: str) -> str:
        value = value.strip()
        if not _NODE_ID_RE.fullmatch(value):
            raise ValueError("node_id must be a stable alphanumeric identifier")
        return value

    @field_validator("objective")
    @classmethod
    def _objective_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("objective must not be blank")
        return value

    @field_validator("depends_on")
    @classmethod
    def _unique_dependencies(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = tuple(dict.fromkeys(value.strip() for value in values))
        if any(not value for value in cleaned):
            raise ValueError("dependencies must not be blank")
        return cleaned

    @field_validator("predicted_write_set")
    @classmethod
    def _normalize_write_set(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(dict.fromkeys(normalize_write_scope(value) for value in values))

    @model_validator(mode="after")
    def _role_capability_is_valid(self) -> "TaskNode":
        if self.role.read_only and self.predicted_write_set:
            raise ValueError(f"{self.role.value} nodes must have an empty write-set")
        if self.node_id in self.depends_on:
            raise ValueError("a node cannot depend on itself")
        criterion_ids = [item.criterion_id for item in self.acceptance_criteria]
        if len(criterion_ids) != len(set(criterion_ids)):
            raise ValueError("acceptance criteria ids must be unique within a node")
        return self


class ScheduleBudget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    total_tokens: int = Field(ge=1)
    wall_time_seconds: float = Field(gt=0.0)


class ExecutionLease(BaseModel):
    """A generation-fenced, single-node capability lease."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    node_id: str
    lease_id: str = Field(default_factory=lambda: f"lease_{uuid.uuid4().hex}")
    generation: int = Field(ge=1)
    predicted_write_set: tuple[str, ...] = ()


class RoleCapability(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    role: AgentRole
    read_only: bool
    allowed_write_set: tuple[str, ...] = ()


class TaskEnvelope(BaseModel):
    """Immutable executor input; it intentionally has no conversation field."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    dispatch_id: str = Field(default_factory=lambda: f"dispatch_{uuid.uuid4().hex}")
    node: TaskNode
    lease: ExecutionLease
    capability: RoleCapability
    context_policy: ContextPolicy
    dependency_artifacts: tuple[ArtifactRef, ...] = ()
    allocated_token_budget: int = Field(ge=1)
    allocated_time_seconds: float = Field(gt=0.0)

    @model_validator(mode="after")
    def _consistent_scope(self) -> "TaskEnvelope":
        if self.node.node_id != self.lease.node_id:
            raise ValueError("lease does not belong to node")
        if self.node.role != self.capability.role:
            raise ValueError("capability role does not match node")
        if self.node.role is AgentRole.VERIFIER:
            if self.context_policy is not ContextPolicy.FRESH_ISOLATED:
                raise ValueError("verifiers require a fresh isolated context")
            if not self.capability.read_only:
                raise ValueError("verifiers must be read-only")
        return self


class ChangeEnvelope(BaseModel):
    """A proposed workspace mutation bound to a lease generation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    node_id: str
    lease_id: str
    lease_generation: int = Field(ge=1)
    write_set: tuple[str, ...]
    patch_ref: str | None = None
    metadata: tuple[tuple[str, str], ...] = ()

    @field_validator("write_set")
    @classmethod
    def _normalize_changes(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(dict.fromkeys(normalize_write_scope(value) for value in values))
        if not normalized:
            raise ValueError("a change must name at least one written path")
        return normalized

    @classmethod
    def from_lease(
        cls,
        lease: ExecutionLease,
        *,
        write_set: tuple[str, ...],
        patch_ref: str | None = None,
        metadata: tuple[tuple[str, str], ...] = (),
    ) -> "ChangeEnvelope":
        return cls(
            node_id=lease.node_id,
            lease_id=lease.lease_id,
            lease_generation=lease.generation,
            write_set=write_set,
            patch_ref=patch_ref,
            metadata=metadata,
        )


class NodeExecutionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    success: bool = True
    tokens_used: int = Field(default=0, ge=0)
    artifacts: tuple[ArtifactRef, ...] = ()
    change: ChangeEnvelope | None = None
    acceptance_receipts: tuple[AcceptanceReceipt, ...] = ()
    task_state: str | None = None
    error: str | None = None

    @model_validator(mode="after")
    def _error_semantics(self) -> "NodeExecutionResult":
        if not self.success and not (self.error and self.error.strip()):
            raise ValueError("failed results require an error")
        return self


class NodeReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    node_id: str
    status: NodeStatus
    generation: int = Field(default=0, ge=0)
    runtime_seconds: float = Field(default=0.0, ge=0.0)
    tokens_used: int = Field(default=0, ge=0)
    artifact_refs: tuple[ArtifactRef, ...] = ()
    acceptance_receipts: tuple[AcceptanceReceipt, ...] = ()
    task_state: str | None = None
    failure_reason: str | None = None


class SchedulerMetrics(BaseModel):
    """Machine-readable scheduling metrics with explicit denominators."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    makespan_seconds: float = Field(ge=0.0)
    node_runtime_seconds: dict[str, float]
    peak_parallelism: int = Field(ge=0)
    max_concurrency: int = Field(ge=1)
    peak_normalized_utilization: float = Field(ge=0.0, le=1.0)
    prevented_conflicts: int = Field(ge=0)
    total_tokens_used: int = Field(ge=0)
    total_token_budget: int = Field(ge=1)
    budget_overrun_tokens: int = Field(ge=0)
    critical_path_seconds: float = Field(ge=0.0)


class ScheduleReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "1.0"
    run_id: str
    status: RunStatus
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    nodes: dict[str, NodeReport]
    accepted_changes: tuple[ChangeEnvelope, ...] = ()
    metrics: SchedulerMetrics

    def machine_readable(self) -> dict[str, Any]:
        return self.model_dump(mode="json")
