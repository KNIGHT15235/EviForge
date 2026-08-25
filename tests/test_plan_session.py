from __future__ import annotations

from pathlib import Path

import pytest

from mewcode.permissions import PermissionMode
from mewcode.plan_session import (
    InvalidPlanTransition,
    PlanReviewError,
    PlanSession,
    PlanSessionState,
    plan_content_fingerprint,
)


def _session(tmp_path: Path) -> PlanSession:
    return PlanSession(
        session_id="plan-session-current",
        plan_path=tmp_path / "plan.md",
        pre_permission_mode=PermissionMode.ACCEPT_EDITS,
    )


def test_plan_session_happy_path_is_strictly_ordered(tmp_path: Path) -> None:
    session = _session(tmp_path)
    fingerprint = plan_content_fingerprint("reviewed bytes")

    session.mark_ready(fingerprint, turn=3)
    assert session.state is PlanSessionState.READY_FOR_REVIEW
    assert session.ready_turn == 3

    session.assert_current_review(
        session_id=session.session_id,
        displayed_fingerprint=fingerprint,
        current_fingerprint=fingerprint,
    )
    session.approve()
    session.begin_execution()
    session.close()

    assert session.state is PlanSessionState.CLOSED
    assert session.review_fingerprint == ""


@pytest.mark.parametrize(
    ("action", "initial"),
    [
        ("approve", PlanSessionState.DRAFT),
        ("begin_execution", PlanSessionState.DRAFT),
        ("begin_execution", PlanSessionState.READY_FOR_REVIEW),
        ("mark_ready", PlanSessionState.APPROVED),
    ],
)
def test_plan_session_rejects_skipped_transitions(
    tmp_path: Path,
    action: str,
    initial: PlanSessionState,
) -> None:
    session = _session(tmp_path)
    session.state = initial

    with pytest.raises(InvalidPlanTransition):
        if action == "mark_ready":
            session.mark_ready(plan_content_fingerprint("x"), turn=1)
        else:
            getattr(session, action)()


def test_cancel_returns_review_to_draft_without_approval(tmp_path: Path) -> None:
    session = _session(tmp_path)
    session.mark_ready(plan_content_fingerprint("x"), turn=1)

    session.cancel_review()

    assert session.state is PlanSessionState.DRAFT
    assert session.review_fingerprint == ""
    with pytest.raises(InvalidPlanTransition):
        session.approve()


@pytest.mark.parametrize(
    ("session_id", "displayed", "current", "message"),
    [
        ("old-session", "same", "same", "stale Plan session"),
        ("plan-session-current", "old", "same", "fingerprint is stale"),
        ("plan-session-current", "same", "changed", "changed after it was shown"),
    ],
)
def test_review_identity_and_exact_content_are_required(
    tmp_path: Path,
    session_id: str,
    displayed: str,
    current: str,
    message: str,
) -> None:
    session = _session(tmp_path)
    session.mark_ready("same", turn=1)

    with pytest.raises(PlanReviewError, match=message):
        session.assert_current_review(
            session_id=session_id,
            displayed_fingerprint=displayed,
            current_fingerprint=current,
        )
