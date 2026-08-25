from __future__ import annotations

from pathlib import Path

import pytest

from mewcode.execution import ExecutionStatus, ExecutionTraceEvent, TraceStage
from mewcode.runtime import RuntimeStore, TaskRuntime, TaskState


def ready_runtime(tmp_path: Path) -> TaskRuntime:
    store = RuntimeStore(control_root=tmp_path, workspace_id="kernel")
    runtime = TaskRuntime.create(store, task_id="task-1", trace_id="trace-1")
    runtime.prepare_contract("contract-1")
    runtime.begin_planning()
    runtime.begin_execution()
    runtime.begin_verification()
    return runtime


@pytest.mark.parametrize(
    ("verdict", "expected"),
    [
        ("PASS", TaskState.COMPLETED),
        ("PARTIAL", TaskState.PARTIAL),
        ("FAIL", TaskState.REPLANNING),
        ("BLOCKED", TaskState.NEEDS_HUMAN),
    ],
)
def test_gate_verdict_has_one_canonical_fsm_mapping(
    tmp_path: Path, verdict: str, expected: TaskState
) -> None:
    runtime = ready_runtime(tmp_path / verdict)

    updated = runtime.apply_gate_verdict(
        verdict,
        decision_id="gate-1",
        bundle_ref="bundle:sha256:abc",
        reasons=("fixture",),
    )

    assert updated.state is expected
    state_event = runtime.store.list_events(trace_id="trace-1")[-1].event
    assert state_event.payload["details"]["gate_verdict"] == verdict


def test_completed_task_can_queue_evolution(tmp_path: Path) -> None:
    runtime = ready_runtime(tmp_path)
    runtime.apply_gate_verdict("PASS", decision_id="gate-1")

    assert runtime.queue_evolution().state is TaskState.EVOLUTION_PENDING


@pytest.mark.asyncio
async def test_gateway_event_is_redacted_and_bound_to_task(tmp_path: Path) -> None:
    store = RuntimeStore(control_root=tmp_path, workspace_id="trace")
    runtime = TaskRuntime.create(store, task_id="task-1", trace_id="trace-1")

    await runtime.record_execution_event(
        ExecutionTraceEvent(
            invocation_id="invoke-1",
            tool_name="WriteFile",
            stage=TraceStage.COMPLETED,
            risk_level="L1",
            reason_codes=("risk.l1.reversible_local_write",),
            status=ExecutionStatus.SUCCEEDED,
            argument_keys=("content", "file_path"),
            arguments_hash="a" * 64,
        )
    )

    event = store.list_events(trace_id="trace-1")[-1].event
    assert event.task_id == "task-1"
    assert event.span_id == "invoke-1"
    assert event.tool_name == "WriteFile"
    assert event.status == "succeeded"
    assert event.payload["argument_keys"] == ["content", "file_path"]
    assert event.normalized_args_hash == "a" * 64
    assert event.payload["arguments_hash"] == "a" * 64
    # Argument *names* are auditable; argument values/source text are absent.
    assert "secret source body" not in event.model_dump_json()
    assert "arguments" not in event.payload


def test_unknown_gate_verdict_fails_closed(tmp_path: Path) -> None:
    runtime = ready_runtime(tmp_path)

    with pytest.raises(ValueError, match="unknown Evidence Gate verdict"):
        runtime.apply_gate_verdict("MAYBE", decision_id="gate-1")
