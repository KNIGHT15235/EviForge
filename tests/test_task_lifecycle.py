from __future__ import annotations

import asyncio

import pytest

from mewcode.agents.task_manager import BackgroundTask, ProgressInfo, TaskManager
from mewcode.agents.trace import TraceManager
from mewcode.tools.agent_tool import AgentTool


class _FakeAgent:
    def __init__(self, *, block: bool = False) -> None:
        self.block = block
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.total_input_tokens = 3
        self.total_output_tokens = 5
        self.team_name = ""
        self._team_manager = None

    async def run_to_completion(self, prompt: str, conversation=None) -> str:
        self.started.set()
        if self.block:
            await self.release.wait()
        return f"done:{prompt}"


@pytest.mark.asyncio
async def test_snapshot_and_wait_for_idle_use_public_state() -> None:
    manager = TaskManager()
    agent = _FakeAgent()

    task_id = manager.launch(agent, "inspect")
    assert await manager.wait_for_idle(timeout=1.0)

    snapshot = manager.snapshot()
    assert snapshot.running_ids == ()
    assert snapshot.completed_ids == (task_id,)
    assert snapshot.failed_ids == ()
    completed = manager.drain_events()
    assert [item.id for item in completed] == [task_id]


@pytest.mark.asyncio
async def test_terminal_callback_observes_final_status_and_usage() -> None:
    manager = TaskManager()
    agent = _FakeAgent()
    observed: list[tuple[str, str, int, int]] = []

    task_id = manager.launch(
        agent,
        "inspect",
        on_terminal=lambda task: observed.append(
            (
                task.id,
                task.status,
                task.progress.input_tokens,
                task.progress.output_tokens,
            )
        ),
    )
    assert await manager.wait_for_idle(timeout=1.0)

    assert observed == [(task_id, "completed", 3, 5)]


def test_agent_tool_terminal_callback_closes_trace_for_team_or_agent() -> None:
    trace_manager = TraceManager()
    node = trace_manager.create("worker")
    tool = object.__new__(AgentTool)
    tool._trace_manager = trace_manager
    background = BackgroundTask(
        id="task-1",
        name="worker",
        agent=object(),
        task="work",
        status="timed_out",
        progress=ProgressInfo(input_tokens=13, output_tokens=8),
    )

    tool._background_trace_callback(node.agent_id)(background)

    updated = trace_manager.get(node.agent_id)
    assert updated.status == "timed_out"
    assert updated.input_tokens == 13
    assert updated.output_tokens == 8
    assert updated.end_time is not None


@pytest.mark.asyncio
async def test_shutdown_cancel_reaps_running_tasks() -> None:
    manager = TaskManager()
    agent = _FakeAgent(block=True)
    task_id = manager.launch(agent, "hang")
    await asyncio.wait_for(agent.started.wait(), timeout=1.0)

    snapshot = await manager.shutdown(policy="cancel", timeout=1.0)

    assert snapshot.running_count == 0
    assert snapshot.cancelled_ids == (task_id,)
    assert manager.get(task_id).status == "cancelled"


@pytest.mark.asyncio
async def test_shutdown_wait_finishes_cooperative_task() -> None:
    manager = TaskManager()
    agent = _FakeAgent(block=True)
    task_id = manager.launch(agent, "finish")
    await asyncio.wait_for(agent.started.wait(), timeout=1.0)
    agent.release.set()

    snapshot = await manager.shutdown(policy="wait", timeout=1.0)

    assert snapshot.completed_ids == (task_id,)
    assert snapshot.running_ids == ()


@pytest.mark.asyncio
async def test_shutdown_wait_marks_budget_exhaustion_as_timed_out() -> None:
    manager = TaskManager()
    agent = _FakeAgent(block=True)
    task_id = manager.launch(agent, "never-finishes")
    await asyncio.wait_for(agent.started.wait(), timeout=1.0)

    snapshot = await manager.shutdown(policy="wait", timeout=0.0)

    assert snapshot.running_count == 0
    assert snapshot.timed_out_ids == (task_id,)
    assert snapshot.cancelled_ids == ()
    assert manager.get(task_id).status == "timed_out"


@pytest.mark.asyncio
async def test_shutdown_rejects_unknown_policy() -> None:
    manager = TaskManager()
    with pytest.raises(ValueError, match="background policy"):
        await manager.shutdown(policy="forever")
