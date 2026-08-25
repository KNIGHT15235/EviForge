from __future__ import annotations

import hashlib
import hmac
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from mewcode.permissions import PermissionMode


class PlanSessionState(str, Enum):
    """Host-owned lifecycle for one Plan review and execution."""

    DRAFT = "draft"
    READY_FOR_REVIEW = "ready_for_review"
    APPROVED = "approved"
    EXECUTING = "executing"
    CLOSED = "closed"


class InvalidPlanTransition(RuntimeError):
    """Raised when an event attempts to skip the Plan approval lifecycle."""


class PlanReviewError(RuntimeError):
    """Raised when an approval does not identify the currently reviewed bytes."""


def plan_content_fingerprint(content: bytes | str) -> str:
    """Return a stable fingerprint of the exact Plan bytes shown for review."""

    data = content.encode("utf-8") if isinstance(content, str) else content
    return hashlib.sha256(data).hexdigest()


@dataclass
class PlanSession:
    """One isolated Plan draft, review decision, and execution hand-off.

    The model never supplies any field in this object.  In particular, an
    approval is valid only for ``session_id`` and ``review_fingerprint`` that
    the host captured after a successful ExitPlanMode call.
    """

    plan_path: Path
    pre_permission_mode: PermissionMode
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    state: PlanSessionState = PlanSessionState.DRAFT
    review_fingerprint: str = ""
    ready_turn: int | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def _transition(
        self,
        expected: PlanSessionState | tuple[PlanSessionState, ...],
        target: PlanSessionState,
    ) -> None:
        allowed = (expected,) if isinstance(expected, PlanSessionState) else expected
        if self.state not in allowed:
            expected_names = ", ".join(state.value for state in allowed)
            raise InvalidPlanTransition(
                f"cannot transition Plan session {self.session_id} from "
                f"{self.state.value} to {target.value}; expected {expected_names}"
            )
        self.state = target
        self.updated_at = datetime.now(timezone.utc)

    def mark_ready(self, fingerprint: str, *, turn: int) -> None:
        if not fingerprint:
            raise ValueError("a Plan review requires a non-empty content fingerprint")
        self._transition(PlanSessionState.DRAFT, PlanSessionState.READY_FOR_REVIEW)
        self.review_fingerprint = fingerprint
        self.ready_turn = turn

    def assert_current_review(
        self,
        *,
        session_id: str,
        displayed_fingerprint: str,
        current_fingerprint: str,
    ) -> None:
        if self.state is not PlanSessionState.READY_FOR_REVIEW:
            raise PlanReviewError("this Plan is not awaiting review")
        if not hmac.compare_digest(self.session_id, session_id):
            raise PlanReviewError("the approval belongs to a stale Plan session")
        if not hmac.compare_digest(self.review_fingerprint, displayed_fingerprint):
            raise PlanReviewError("the approval fingerprint is stale")
        if not hmac.compare_digest(self.review_fingerprint, current_fingerprint):
            raise PlanReviewError(
                "the Plan file changed after it was shown; review the updated Plan again"
            )

    def cancel_review(self) -> None:
        if self.state is PlanSessionState.DRAFT:
            return
        self._transition(PlanSessionState.READY_FOR_REVIEW, PlanSessionState.DRAFT)
        self.review_fingerprint = ""
        self.ready_turn = None

    def approve(self) -> None:
        self._transition(PlanSessionState.READY_FOR_REVIEW, PlanSessionState.APPROVED)

    def begin_execution(self) -> None:
        self._transition(PlanSessionState.APPROVED, PlanSessionState.EXECUTING)

    def close(self) -> None:
        if self.state is PlanSessionState.CLOSED:
            return
        self._transition(
            (
                PlanSessionState.DRAFT,
                PlanSessionState.READY_FOR_REVIEW,
                PlanSessionState.APPROVED,
                PlanSessionState.EXECUTING,
            ),
            PlanSessionState.CLOSED,
        )
        self.review_fingerprint = ""
        self.ready_turn = None
