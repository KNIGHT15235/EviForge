from __future__ import annotations

import pytest

from mewcode.runtime import InvalidTransitionError, TaskRun, TaskState


def test_task_run_follows_canonical_happy_path() -> None:
    run = TaskRun.new(task_id="task-1", trace_id="trace-1")

    for state in (
        TaskState.CONTRACT_READY,
        TaskState.PLANNING,
        TaskState.EXECUTING,
        TaskState.VERIFYING,
        TaskState.COMPLETED,
        TaskState.EVOLUTION_PENDING,
    ):
        run = run.transitioned(state)

    assert run.state is TaskState.EVOLUTION_PENDING
    assert run.version == 6
    assert run.is_final
    assert not run.allowed_transitions


def test_task_run_rejects_jump_and_preserves_original() -> None:
    run = TaskRun.new(task_id="task-1", trace_id="trace-1")

    with pytest.raises(InvalidTransitionError) as caught:
        run.transitioned(TaskState.EXECUTING)

    assert caught.value.current is TaskState.RECEIVED
    assert caught.value.requested is TaskState.EXECUTING
    assert run.state is TaskState.RECEIVED
    assert run.version == 0


def test_policy_denied_is_final_and_cannot_be_approved() -> None:
    run = TaskRun.new(task_id="task-1", trace_id="trace-1")
    run = run.transitioned(TaskState.CONTRACT_READY)
    run = run.transitioned(TaskState.PLANNING)
    run = run.transitioned(TaskState.POLICY_DENIED)

    assert run.is_final
    assert not run.can_transition_to(TaskState.AWAITING_APPROVAL)
    with pytest.raises(InvalidTransitionError):
        run.transitioned(TaskState.EXECUTING)
