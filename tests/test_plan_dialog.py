from __future__ import annotations

from mewcode.plan_dialog import InlinePlanWidget, PlanChoice


def test_escape_is_a_bound_cancel_decision(monkeypatch) -> None:
    posted: list[InlinePlanWidget.Responded] = []
    monkeypatch.setattr(
        InlinePlanWidget,
        "post_message",
        lambda _self, message: posted.append(message),
    )
    widget = InlinePlanWidget(
        session_id="current-session",
        plan_fingerprint="current-fingerprint",
    )

    widget.action_cancel()

    assert len(posted) == 1
    assert posted[0].choice is PlanChoice.CANCEL
    assert posted[0].choice is not PlanChoice.MANUAL
    assert posted[0].session_id == "current-session"
    assert posted[0].plan_fingerprint == "current-fingerprint"
