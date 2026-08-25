"""Typed TaskRun finite-state machine.

Only this module defines legal TaskRun transitions.  Callers cannot turn a
model-generated string directly into a persisted state change: they must first
pass this deterministic transition table.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class TaskState(str, Enum):
    RECEIVED = "RECEIVED"
    CONTRACT_READY = "CONTRACT_READY"
    PLANNING = "PLANNING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    EXECUTING = "EXECUTING"
    VERIFYING = "VERIFYING"
    RECOVERING = "RECOVERING"
    REPLANNING = "REPLANNING"
    NEEDS_HUMAN = "NEEDS_HUMAN"
    COMPLETED = "COMPLETED"
    EVOLUTION_PENDING = "EVOLUTION_PENDING"
    FAILED = "FAILED"
    PARTIAL = "PARTIAL"
    POLICY_DENIED = "POLICY_DENIED"
    CANCELLED = "CANCELLED"


# Cancellation is legal from every active/waiting state.  The other edges are
# the state diagram from the EviForge design document.
ALLOWED_TRANSITIONS: Mapping[TaskState, frozenset[TaskState]] = {
    TaskState.RECEIVED: frozenset({TaskState.CONTRACT_READY, TaskState.CANCELLED}),
    TaskState.CONTRACT_READY: frozenset({TaskState.PLANNING, TaskState.CANCELLED}),
    TaskState.PLANNING: frozenset(
        {
            TaskState.POLICY_DENIED,
            TaskState.AWAITING_APPROVAL,
            TaskState.EXECUTING,
            TaskState.CANCELLED,
        }
    ),
    TaskState.AWAITING_APPROVAL: frozenset(
        {TaskState.EXECUTING, TaskState.CANCELLED}
    ),
    TaskState.EXECUTING: frozenset(
        {TaskState.VERIFYING, TaskState.RECOVERING, TaskState.CANCELLED}
    ),
    TaskState.RECOVERING: frozenset(
        {TaskState.EXECUTING, TaskState.NEEDS_HUMAN, TaskState.CANCELLED}
    ),
    TaskState.VERIFYING: frozenset(
        {
            TaskState.REPLANNING,
            TaskState.COMPLETED,
            TaskState.PARTIAL,
            TaskState.NEEDS_HUMAN,
            TaskState.FAILED,
            TaskState.CANCELLED,
        }
    ),
    TaskState.REPLANNING: frozenset({TaskState.PLANNING, TaskState.CANCELLED}),
    TaskState.NEEDS_HUMAN: frozenset({TaskState.PLANNING, TaskState.CANCELLED}),
    TaskState.COMPLETED: frozenset({TaskState.EVOLUTION_PENDING}),
    TaskState.EVOLUTION_PENDING: frozenset(),
    TaskState.FAILED: frozenset(),
    TaskState.PARTIAL: frozenset(),
    TaskState.POLICY_DENIED: frozenset(),
    TaskState.CANCELLED: frozenset(),
}

FINAL_STATES = frozenset(
    {
        TaskState.EVOLUTION_PENDING,
        TaskState.FAILED,
        TaskState.PARTIAL,
        TaskState.POLICY_DENIED,
        TaskState.CANCELLED,
    }
)


class InvalidTransitionError(ValueError):
    """Raised when a requested state edge is absent from the FSM."""

    def __init__(self, current: TaskState, requested: TaskState) -> None:
        self.current = current
        self.requested = requested
        allowed = ", ".join(sorted(state.value for state in ALLOWED_TRANSITIONS[current]))
        super().__init__(
            f"illegal TaskRun transition {current.value} -> {requested.value}; "
            f"allowed: {allowed or '<none>'}"
        )


class ConcurrentTransitionError(RuntimeError):
    """Raised when optimistic TaskRun version checking detects a stale writer."""

    def __init__(self, task_id: str, expected: int, actual: int) -> None:
        self.task_id = task_id
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"stale TaskRun {task_id}: expected version {expected}, actual {actual}"
        )


def _coerce_state(value: TaskState | str) -> TaskState:
    if isinstance(value, TaskState):
        return value
    try:
        return TaskState(value)
    except ValueError as exc:
        raise ValueError(f"unknown TaskRun state: {value!r}") from exc


@dataclass(frozen=True, slots=True)
class TaskRun:
    task_id: str
    trace_id: str
    state: TaskState = TaskState.RECEIVED
    version: int = 0
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "state", _coerce_state(self.state))
        if not self.task_id.strip():
            raise ValueError("task_id must not be empty")
        if not self.trace_id.strip():
            raise ValueError("trace_id must not be empty")
        if self.version < 0:
            raise ValueError("version must be non-negative")
        if self.created_at.tzinfo is None or self.updated_at.tzinfo is None:
            raise ValueError("TaskRun timestamps must be timezone-aware")

    @classmethod
    def new(
        cls,
        *,
        task_id: str | None = None,
        trace_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        now: datetime | None = None,
    ) -> TaskRun:
        instant = now or utc_now()
        return cls(
            task_id=task_id or f"task_{uuid.uuid4().hex}",
            trace_id=trace_id or f"trace_{uuid.uuid4().hex}",
            created_at=instant,
            updated_at=instant,
            metadata=dict(metadata or {}),
        )

    @property
    def is_final(self) -> bool:
        return self.state in FINAL_STATES

    @property
    def allowed_transitions(self) -> frozenset[TaskState]:
        return ALLOWED_TRANSITIONS[self.state]

    def can_transition_to(self, requested: TaskState | str) -> bool:
        return _coerce_state(requested) in self.allowed_transitions

    def transitioned(
        self, requested: TaskState | str, *, now: datetime | None = None
    ) -> TaskRun:
        next_state = _coerce_state(requested)
        if next_state not in self.allowed_transitions:
            raise InvalidTransitionError(self.state, next_state)
        return replace(
            self,
            state=next_state,
            version=self.version + 1,
            updated_at=now or utc_now(),
            metadata=dict(self.metadata),
        )
