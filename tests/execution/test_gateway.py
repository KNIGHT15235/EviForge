from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel, Field

from mewcode.execution import (
    ExecutionGateway,
    ExecutionStatus,
    ReasonCode,
    RiskEngine,
    ToolInvocation,
    TraceStage,
)
from mewcode.permissions import (
    DangerousCommandDetector,
    PathSandbox,
    PermissionChecker,
    PermissionMode,
    RuleEngine,
)
from mewcode.tools.base import Tool, ToolResult


class ReadParams(BaseModel):
    file_path: str
    limit: int = Field(default=20, ge=1, le=100)


class RecordingReadTool(Tool):
    name = "RecordingRead"
    description = "read a fixture"
    params_model = ReadParams
    category = "read"
    is_concurrency_safe = True

    def __init__(self) -> None:
        self.calls: list[ReadParams] = []

    async def execute(self, params: ReadParams) -> ToolResult:
        self.calls.append(params)
        return ToolResult(output=f"read:{params.file_path}:{params.limit}")


class WriteParams(BaseModel):
    file_path: str
    content: str


class RecordingWriteTool(Tool):
    name = "RecordingWrite"
    description = "write a fixture"
    params_model = WriteParams
    category = "write"

    def __init__(self) -> None:
        self.calls: list[WriteParams] = []

    async def execute(self, params: WriteParams) -> ToolResult:
        self.calls.append(params)
        return ToolResult(output="written")


class CommandParams(BaseModel):
    command: str


class RecordingCommandTool(Tool):
    name = "RecordingCommand"
    description = "execute a fixture command"
    params_model = CommandParams
    category = "command"

    def __init__(self) -> None:
        self.calls: list[CommandParams] = []

    async def execute(self, params: CommandParams) -> ToolResult:
        self.calls.append(params)
        return ToolResult(output="executed")


def permission_checker(root: Path, mode: PermissionMode) -> PermissionChecker:
    return PermissionChecker(
        detector=DangerousCommandDetector(),
        sandbox=PathSandbox(str(root)),
        rule_engine=RuleEngine(),
        mode=mode,
    )


@pytest.mark.asyncio
async def test_schema_is_validated_before_tool_execution(tmp_path: Path) -> None:
    tool = RecordingReadTool()
    gateway = ExecutionGateway(risk_engine=RiskEngine(workspace_root=tmp_path))

    result = await gateway.invoke(tool, {"file_path": "a.txt", "limit": 0})

    assert result.status is ExecutionStatus.VALIDATION_ERROR
    assert result.executed is False
    assert ReasonCode.VALIDATION_SCHEMA_INVALID.value in result.reason_codes
    assert tool.calls == []


@pytest.mark.asyncio
async def test_unexpected_argument_is_rejected_even_if_model_ignores_extra(tmp_path: Path) -> None:
    tool = RecordingReadTool()
    gateway = ExecutionGateway(risk_engine=RiskEngine(workspace_root=tmp_path))

    result = await gateway.invoke(
        tool,
        {"file_path": "a.txt", "limit": 5, "approval": "pretend-allowed"},
    )

    assert result.status is ExecutionStatus.VALIDATION_ERROR
    assert ReasonCode.VALIDATION_UNEXPECTED_ARGUMENT.value in result.reason_codes
    assert tool.calls == []


@pytest.mark.asyncio
async def test_tool_name_mismatch_is_rejected(tmp_path: Path) -> None:
    tool = RecordingReadTool()
    gateway = ExecutionGateway(risk_engine=RiskEngine(workspace_root=tmp_path))
    invocation = ToolInvocation(tool_name="DifferentTool", arguments={"file_path": "a.txt"})

    result = await gateway.execute(tool, invocation)

    assert result.status is ExecutionStatus.VALIDATION_ERROR
    assert ReasonCode.VALIDATION_TOOL_MISMATCH.value in result.reason_codes
    assert tool.calls == []


@pytest.mark.asyncio
async def test_default_read_is_l0_and_executes(tmp_path: Path) -> None:
    tool = RecordingReadTool()
    gateway = ExecutionGateway(risk_engine=RiskEngine(workspace_root=tmp_path))

    result = await gateway.invoke(tool, {"file_path": "src/main.py"})

    assert result.status is ExecutionStatus.SUCCEEDED
    assert result.risk_level == "L0"
    assert ReasonCode.L0_READ_ONLY.value in result.reason_codes
    assert len(tool.calls) == 1


@pytest.mark.asyncio
async def test_trace_and_result_hash_the_same_normalized_defaults(tmp_path: Path) -> None:
    tool = RecordingReadTool()
    events = []
    gateway = ExecutionGateway(
        risk_engine=RiskEngine(workspace_root=tmp_path),
        trace_hook=events.append,
    )

    result = await gateway.invoke(tool, {"file_path": "a.txt"})

    assert result.status is ExecutionStatus.SUCCEEDED
    assert all(event.arguments_hash == result.arguments_hash for event in events)


@pytest.mark.asyncio
async def test_write_is_l1_and_fails_closed_without_permission_policy(tmp_path: Path) -> None:
    tool = RecordingWriteTool()
    gateway = ExecutionGateway(risk_engine=RiskEngine(workspace_root=tmp_path))

    result = await gateway.invoke(tool, {"file_path": "src/main.py", "content": "x"})

    assert result.status is ExecutionStatus.APPROVAL_REQUIRED
    assert result.risk_level == "L1"
    assert ReasonCode.L1_REVERSIBLE_WRITE.value in result.reason_codes
    assert tool.calls == []


@pytest.mark.asyncio
async def test_existing_permission_checker_allows_legacy_tool(tmp_path: Path) -> None:
    tool = RecordingWriteTool()
    gateway = ExecutionGateway(
        permission_checker=permission_checker(tmp_path, PermissionMode.ACCEPT_EDITS),
        risk_engine=RiskEngine(workspace_root=tmp_path),
    )

    result = await gateway.invoke(tool, {"file_path": "src/main.py", "content": "x"})

    assert result.status is ExecutionStatus.SUCCEEDED
    assert result.executed is True
    assert len(tool.calls) == 1


@pytest.mark.asyncio
async def test_existing_ask_decision_can_be_resolved_by_async_callback(tmp_path: Path) -> None:
    tool = RecordingWriteTool()
    approval_calls: list[tuple[str, str]] = []

    async def approve(invocation, risk, reason):
        approval_calls.append((invocation.invocation_id, str(risk.level)))
        assert reason
        return True

    gateway = ExecutionGateway(
        permission_checker=permission_checker(tmp_path, PermissionMode.DEFAULT),
        risk_engine=RiskEngine(workspace_root=tmp_path),
        approval_resolver=approve,
    )

    result = await gateway.invoke(tool, {"file_path": "src/main.py", "content": "x"})

    assert result.status is ExecutionStatus.SUCCEEDED
    assert len(tool.calls) == 1
    assert approval_calls == [(result.invocation_id, "L1")]


@pytest.mark.asyncio
async def test_reserved_git_path_is_l4_even_in_bypass_mode(tmp_path: Path) -> None:
    tool = RecordingWriteTool()
    gateway = ExecutionGateway(
        permission_checker=permission_checker(tmp_path, PermissionMode.BYPASS),
        risk_engine=RiskEngine(workspace_root=tmp_path),
    )

    result = await gateway.invoke(
        tool,
        {"file_path": str(tmp_path / ".git" / "config"), "content": "unsafe"},
    )

    assert result.status is ExecutionStatus.PERMISSION_DENIED
    assert result.risk_level == "L4"
    assert ReasonCode.L4_RESERVED_PATH.value in result.reason_codes
    assert tool.calls == []


@pytest.mark.asyncio
async def test_relative_control_plane_path_is_l4(tmp_path: Path) -> None:
    tool = RecordingReadTool()
    gateway = ExecutionGateway(
        risk_engine=RiskEngine(
            workspace_root=tmp_path,
            control_plane_roots=[".control/runtime"],
        )
    )

    result = await gateway.invoke(tool, {"file_path": ".control/runtime/journal.db"})

    assert result.status is ExecutionStatus.PERMISSION_DENIED
    assert ReasonCode.L4_RESERVED_PATH.value in result.reason_codes
    assert tool.calls == []


@pytest.mark.asyncio
async def test_workspace_boundary_can_be_enforced(tmp_path: Path) -> None:
    tool = RecordingReadTool()
    outside = tmp_path.parent / "outside.txt"
    gateway = ExecutionGateway(
        risk_engine=RiskEngine(
            workspace_root=tmp_path,
            enforce_workspace_boundary=True,
        )
    )

    result = await gateway.invoke(tool, {"file_path": str(outside)})

    assert result.status is ExecutionStatus.PERMISSION_DENIED
    assert ReasonCode.L4_SANDBOX_ESCAPE.value in result.reason_codes
    assert tool.calls == []


@pytest.mark.asyncio
async def test_dangerous_command_is_l4_and_never_reaches_tool(tmp_path: Path) -> None:
    tool = RecordingCommandTool()
    gateway = ExecutionGateway(
        permission_checker=permission_checker(tmp_path, PermissionMode.BYPASS),
        risk_engine=RiskEngine(workspace_root=tmp_path),
    )

    result = await gateway.invoke(tool, {"command": "rm -rf /"})

    assert result.status is ExecutionStatus.PERMISSION_DENIED
    assert result.risk_level == "L4"
    assert ReasonCode.L4_DANGEROUS_COMMAND.value in result.reason_codes
    assert tool.calls == []


@pytest.mark.asyncio
async def test_non_safe_command_is_l2_and_requires_legacy_approval(tmp_path: Path) -> None:
    tool = RecordingCommandTool()
    gateway = ExecutionGateway(
        permission_checker=permission_checker(tmp_path, PermissionMode.DEFAULT),
        risk_engine=RiskEngine(workspace_root=tmp_path),
    )

    result = await gateway.invoke(tool, {"command": "pytest -q"})

    assert result.status is ExecutionStatus.APPROVAL_REQUIRED
    assert result.risk_level == "L2"
    assert ReasonCode.L2_COMMAND_EXECUTION.value in result.reason_codes
    assert tool.calls == []


@pytest.mark.asyncio
async def test_trace_hook_receives_redacted_lifecycle(tmp_path: Path) -> None:
    tool = RecordingReadTool()
    events = []

    async def collect(event):
        events.append(event)

    gateway = ExecutionGateway(
        risk_engine=RiskEngine(workspace_root=tmp_path),
        trace_hook=collect,
    )

    result = await gateway.invoke(tool, {"file_path": "secret-name.txt", "limit": 3})

    assert result.status is ExecutionStatus.SUCCEEDED
    assert [event.stage for event in events] == [
        TraceStage.RECEIVED,
        TraceStage.VALIDATED,
        TraceStage.RISK_ASSESSED,
        TraceStage.PERMISSION_DECIDED,
        TraceStage.STARTED,
        TraceStage.COMPLETED,
    ]
    assert events[0].argument_keys == ("file_path", "limit")
    assert all(event.arguments_hash == result.arguments_hash for event in events)
    assert len(result.arguments_hash) == 64
    assert all("secret-name.txt" not in repr(event) for event in events)
    assert events[-1].status is ExecutionStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_tool_reported_error_is_preserved(tmp_path: Path) -> None:
    class ErrorTool(RecordingReadTool):
        async def execute(self, params: ReadParams) -> ToolResult:
            self.calls.append(params)
            return ToolResult(output="business failure", is_error=True)

    tool = ErrorTool()
    gateway = ExecutionGateway(risk_engine=RiskEngine(workspace_root=tmp_path))

    result = await gateway.invoke(tool, {"file_path": "a.txt"})

    assert result.status is ExecutionStatus.TOOL_ERROR
    assert result.is_error
    assert result.executed
    assert result.output == "business failure"
    assert ReasonCode.TOOL_REPORTED_ERROR.value in result.reason_codes


@pytest.mark.asyncio
async def test_tool_exception_is_normalized(tmp_path: Path) -> None:
    class RaisingTool(RecordingReadTool):
        async def execute(self, params: ReadParams) -> ToolResult:
            raise RuntimeError("boom")

    tool = RaisingTool()
    gateway = ExecutionGateway(risk_engine=RiskEngine(workspace_root=tmp_path))

    result = await gateway.invoke(tool, {"file_path": "a.txt"})

    assert result.status is ExecutionStatus.INTERNAL_ERROR
    assert result.is_error
    assert result.executed
    assert result.exception_type == "RuntimeError"
    assert ReasonCode.TOOL_RAISED_EXCEPTION.value in result.reason_codes
