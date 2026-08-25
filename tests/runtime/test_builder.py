from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel

from mewcode.execution import ExecutionStatus
from mewcode.recovery import ActionState, EffectKind, RecoveryStore
from mewcode.runtime import RuntimeBuilder, workspace_id_for
from mewcode.tools.base import Tool, ToolResult


class ReadParams(BaseModel):
    file_path: str


class ReadTool(Tool):
    name = "ReadFixture"
    description = "read fixture"
    params_model = ReadParams
    category = "read"

    async def execute(self, params: ReadParams) -> ToolResult:
        return ToolResult(output=params.file_path)


def test_workspace_id_is_stable_and_path_specific(tmp_path: Path) -> None:
    assert workspace_id_for(tmp_path) == workspace_id_for(str(tmp_path))
    assert workspace_id_for(tmp_path) != workspace_id_for(tmp_path / "other")


@pytest.mark.asyncio
async def test_builder_wires_gateway_trace_to_durable_task(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    components = RuntimeBuilder(
        workspace,
        control_root=tmp_path / "control",
    ).build(task_id="task-builder")

    result = await components.gateway.invoke(ReadTool(), {"file_path": "README.md"})

    assert result.status is ExecutionStatus.SUCCEEDED
    events = components.store.list_events(task_id="task-builder")
    assert any(event.event.event_type == "tool_execution_stage" for event in events)
    assert components.recovery.db_path == components.store.db_path
    assert components.task.run.metadata["protection_mode"] == "policy_only"
    assert components.evolution.workspace == workspace.resolve()
    assert components.execution_context.task_id == "task-builder"
    assert components.startup_recovery.items == ()
    components.close()


def test_builder_scans_interrupted_external_action_on_startup(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    control = tmp_path / "control"
    first = RuntimeBuilder(workspace, control_root=control).build(task_id="old-task")
    action = first.recovery.prepare_action(
        task_id="old-task",
        action_id="crashed-action",
        action_type="tool:command",
        idempotency_key="crashed-invocation",
        normalized_args_hash="args-hash",
        cwd=workspace,
        plan_hash="plan-hash",
        expected_pre_state_hash="pre-hash",
        effect_kind=EffectKind.EXTERNAL,
    )
    ticket = first.recovery.issue_for_action(
        action.action_id,
        approver="user",
        ttl_seconds=60,
    )
    first.recovery.reserve_for_action(
        ticket.ticket_id,
        action.action_id,
        reservation_token="worker",
    )
    first.recovery.authorize_action(
        action.action_id,
        ticket.ticket_id,
        reservation_token="worker",
    )
    first.recovery.start_action(action.action_id, reservation_token="worker")
    first.close()

    second = RuntimeBuilder(workspace, control_root=control).build(task_id="new-task")
    assert second.recovery.get_action(action.action_id).state is ActionState.UNCERTAIN
    assert any(
        item.action.action_id == action.action_id
        and item.recommendation == "human_review_only"
        for item in second.startup_recovery.items
    )
    second.close()


def test_builder_rejects_fake_isolation_mode(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="protection_mode"):
        RuntimeBuilder(tmp_path, protection_mode="marketing_claim")
