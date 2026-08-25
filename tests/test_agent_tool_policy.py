from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from mewcode.execution import ExecutionContext, ExecutionGateway, RiskEngine
from mewcode.permissions import (
    DangerousCommandDetector,
    PathSandbox,
    PermissionChecker,
    PermissionMode,
    RuleEngine,
)
from mewcode.tools import ToolRegistry
from mewcode.tools.agent_tool import AgentTool, AgentToolParams
from mewcode.tools.base import Tool, ToolResult


class WriteParams(BaseModel):
    file_path: str


class WriteFixture(Tool):
    name = "WriteFixture"
    description = "write fixture"
    params_model = WriteParams
    category = "write"

    async def execute(self, params: BaseModel) -> ToolResult:
        return ToolResult(output="ok")


class ReadFixture(WriteFixture):
    name = "ReadFile"
    category = "read"


def _checker(root: Path) -> PermissionChecker:
    return PermissionChecker(
        detector=DangerousCommandDetector(),
        sandbox=PathSandbox(str(root)),
        rule_engine=RuleEngine(),
        mode=PermissionMode.DONT_ASK,
    )


def test_subagent_inherits_reviewed_manifest_trace_and_journal(tmp_path: Path) -> None:
    parent_root = tmp_path / "parent"
    child_root = tmp_path / "child"
    control = tmp_path / "control"
    parent_root.mkdir()
    child_root.mkdir()
    control.mkdir()
    trace_hook = object()
    journal = object()
    context = ExecutionContext(
        task_id="task-1",
        cwd=str(parent_root),
        workspace_root=str(parent_root),
        plan_hash="reviewed-plan",
        write_set=("approved.py",),
        commands=(),
        network_hosts=(),
    )
    gateway = ExecutionGateway(
        permission_checker=_checker(parent_root),
        risk_engine=RiskEngine(
            workspace_root=parent_root,
            control_plane_roots=(control,),
        ),
        trace_hook=trace_hook,
        execution_context=context,
        action_journal=journal,
    )
    parent = SimpleNamespace(
        execution_gateway=gateway,
        execution_context=context,
        work_dir=str(parent_root),
        _active_execution_context=lambda: context,
    )
    tool = AgentTool(
        agent_loader=SimpleNamespace(),
        task_manager=SimpleNamespace(),
        trace_manager=SimpleNamespace(),
        parent_agent=parent,
    )

    kwargs = tool._child_runtime_kwargs(_checker(child_root), str(child_root))
    child_gateway = kwargs["execution_gateway"]
    child_context = kwargs["execution_context"]

    assert child_gateway is not gateway
    assert child_gateway.trace_hook is trace_hook
    assert child_gateway.action_journal is journal
    assert child_context.task_id == "task-1"
    assert child_context.plan_hash == "reviewed-plan"
    assert Path(child_context.workspace_root) == child_root.resolve()
    assert kwargs["task_runtime"] is None
    assert kwargs["requirement_contract"] is None

    denied = child_gateway.preview(
        WriteFixture(),
        {"file_path": "outside.py"},
        invocation_id="child-write",
        context=child_context,
    )
    allowed = child_gateway.preview(
        WriteFixture(),
        {"file_path": "approved.py"},
        invocation_id="child-write-ok",
        context=child_context,
    )
    assert not denied.valid
    assert "manifest.write_set_violation" in denied.reason_codes
    assert allowed.valid


def test_reviewed_plan_is_detected_for_persistent_delegation(tmp_path: Path) -> None:
    context = ExecutionContext(
        task_id="task-2",
        cwd=str(tmp_path),
        plan_hash="reviewed-plan",
    )
    parent = SimpleNamespace(execution_context=context)
    tool = AgentTool(
        agent_loader=SimpleNamespace(),
        task_manager=SimpleNamespace(),
        trace_manager=SimpleNamespace(),
        parent_agent=parent,
    )
    assert tool._planned_execution_active()


def _plan_agent_tool(tmp_path: Path, *, loader: object | None = None) -> AgentTool:
    parent = SimpleNamespace(
        plan_mode=True,
        execution_context=None,
        work_dir=str(tmp_path),
    )
    return AgentTool(
        agent_loader=loader or SimpleNamespace(),
        task_manager=SimpleNamespace(),
        trace_manager=SimpleNamespace(),
        parent_agent=parent,
        enable_fork=True,
    )


def test_plan_delegate_forces_plan_permissions_and_read_only_registry(
    tmp_path: Path,
) -> None:
    tool = _plan_agent_tool(tmp_path)
    assert tool._subagent_permission_mode("dontAsk") is PermissionMode.PLAN

    registry = ToolRegistry()
    registry.register(ReadFixture())
    registry.register(WriteFixture())
    restricted = tool._restrict_plan_subagent_tools(registry)
    assert [item.name for item in restricted.list_tools()] == ["ReadFile"]


@pytest.mark.asyncio
async def test_plan_delegate_rejects_fork_team_and_worktree(tmp_path: Path) -> None:
    worktree_definition = SimpleNamespace(isolation="worktree")
    loader = SimpleNamespace(get=lambda _name: worktree_definition)
    tool = _plan_agent_tool(tmp_path, loader=loader)

    fork = await tool.execute(AgentToolParams(prompt="inspect", description="fork"))
    team = await tool.execute(
        AgentToolParams(
            prompt="inspect",
            description="team",
            team_name="reviewers",
        )
    )
    worktree = await tool.execute(
        AgentToolParams(
            prompt="inspect",
            description="worktree",
            subagent_type="custom",
        )
    )

    assert fork.is_error and "forks are disabled" in fork.output
    assert team.is_error and "disabled during Plan" in team.output
    assert worktree.is_error and "disabled in Plan" in worktree.output
