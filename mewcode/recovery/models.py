"""Typed records for approval, action journaling, and crash recovery."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Mapping


class TicketState(str, Enum):
    ISSUED = "issued"
    RESERVED = "reserved"
    CONSUMED = "consumed"
    REVOKED = "revoked"
    EXPIRED = "expired"


class ActionState(str, Enum):
    PREPARED = "prepared"
    AUTHORIZED = "authorized"
    STARTED = "started"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNCERTAIN = "uncertain"


class EffectKind(str, Enum):
    """How safely an interrupted action can be reconciled."""

    FILE_REPLACE = "file_replace"
    READ_ONLY = "read_only"
    EXTERNAL = "external"


TERMINAL_ACTION_STATES = frozenset(
    {ActionState.SUCCEEDED, ActionState.FAILED, ActionState.UNCERTAIN}
)


@dataclass(frozen=True, slots=True)
class ApprovalTicket:
    ticket_id: str
    action_id: str
    normalized_args_hash: str
    cwd_realpath: str
    plan_hash: str
    expected_pre_state_hash: str
    approver: str
    issued_at: datetime
    expires_at: datetime
    max_uses: int
    use_count: int
    state: TicketState
    version: int
    reservation_token: str | None = None
    reserved_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ActionRecord:
    action_id: str
    task_id: str
    action_type: str
    state: ActionState
    idempotency_key: str
    normalized_args_hash: str
    cwd_realpath: str
    plan_hash: str
    expected_pre_state_hash: str
    effect_kind: EffectKind
    target_realpath: str | None
    expected_old_hash: str | None
    desired_hash: str | None
    temp_path: str | None
    postcondition: Mapping[str, Any]
    ticket_id: str | None
    attempt_id: str | None
    fencing_generation: int
    version: int
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    error: str | None
    reconciled: bool


@dataclass(frozen=True, slots=True)
class AttemptLease:
    action_id: str
    attempt_id: str
    fencing_generation: int


@dataclass(frozen=True, slots=True)
class JournalEntry:
    sequence: int
    action_id: str
    from_state: ActionState | None
    to_state: ActionState
    occurred_at: datetime
    attempt_id: str | None
    fencing_generation: int
    reason: str
    details: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class Checkpoint:
    checkpoint_id: str
    task_id: str
    label: str
    created_at: datetime
    action_id: str | None
    journal_sequence: int
    payload: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class RecoveryItem:
    action: ActionRecord
    recommendation: str
    reason: str


@dataclass(frozen=True, slots=True)
class RecoveryReport:
    scanned_at: datetime
    reconciled_action_ids: tuple[str, ...]
    items: tuple[RecoveryItem, ...]
    expired_ticket_ids: tuple[str, ...]


class RecoveryError(RuntimeError):
    """Base class for durable recovery failures."""


class BindingMismatchError(RecoveryError):
    pass


class TicketUnavailableError(RecoveryError):
    pass


class ConcurrentReservationError(TicketUnavailableError):
    pass


class InvalidActionTransition(RecoveryError):
    pass


class FencingConflictError(RecoveryError):
    pass


class UncertainActionError(RecoveryError):
    pass


class PreconditionsFailedError(RecoveryError):
    pass


class SimulatedCrash(RecoveryError):
    """Test seam representing process death after an external commit point."""
