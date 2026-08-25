from __future__ import annotations

from datetime import datetime

import pytest
from pydantic import ValidationError

from mewcode.runtime import TraceEvent


def test_trace_event_canonical_round_trip() -> None:
    event = TraceEvent(
        event_id="evt-fixed",
        trace_id="trace-1",
        task_id="task-1",
        event_type="tool_finished",
        tool_name="Read",
        artifact_refs=("sha256:abc",),
        payload={"ok": True, "text": "中文"},
    )

    restored = TraceEvent.model_validate_json(event.canonical_json())

    assert restored == event
    assert restored.artifact_refs == ("sha256:abc",)
    assert "中文" in event.canonical_json()


def test_trace_event_rejects_naive_wall_clock() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        TraceEvent(
            trace_id="trace-1",
            event_type="invalid",
            wall_time=datetime(2026, 1, 1),
        )


def test_trace_event_rejects_unknown_or_negative_metrics() -> None:
    with pytest.raises(ValidationError):
        TraceEvent(
            trace_id="trace-1",
            event_type="llm_response",
            input_tokens=-1,
            made_up_field=True,
        )
