from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from pydantic import BaseModel

from mewcode.execution import (
    ExecutionContext,
    ExecutionGateway,
    ExecutionStatus,
    ReasonCode,
    RiskEngine,
    ToolInvocation,
)
from mewcode.recovery import ActionState, RecoveryExecutionCoordinator, RecoveryStore
from mewcode.recovery import canonical_realpath
from mewcode.tools.base import Tool, ToolResult


class WriteParams(BaseModel):
    file_path: str
    content: str


class WriteTool(Tool):
    name = "BoundWrite"
    description = "write fixture"
    params_model = WriteParams
    category = "write"

    def __init__(self, *, error: bool = False, raise_error: bool = False) -> None:
        self.calls = 0
        self.error = error
        self.raise_error = raise_error

    async def execute(self, params: WriteParams) -> ToolResult:
        self.calls += 1
        if self.raise_error:
            raise RuntimeError("write stopped")
        return ToolResult("write failed" if self.error else "written", is_error=self.error)


class CommandParams(BaseModel):
    command: str


class InterruptingCommand(Tool):
    name = "BoundCommand"
    description = "command fixture"
    params_model = CommandParams
    category = "command"

    async def execute(self, params: CommandParams) -> ToolResult:
        raise asyncio.CancelledError("worker lost")


class NetworkParams(BaseModel):
    url: str


class NetworkTool(Tool):
    name = "BoundNetwork"
    description = "network fixture"
    params_model = NetworkParams
    category = "read"
    side_effect = "network"
    risk_tags = ("network",)

    async def execute(self, params: NetworkParams) -> ToolResult:
        return ToolResult("downloaded")


class ExternalWriteTool(NetworkTool):
    name = "BoundExternalWrite"
    side_effect = "external_write"
    risk_tags = ("external_write", "network")


class InternalCommandParams(BaseModel):
    task: str


class InternalCommandTool(Tool):
    name = "InternalTask"
    description = "structured orchestration fixture"
    params_model = InternalCommandParams
    category = "command"

    async def execute(self, params: InternalCommandParams) -> ToolResult:
        return ToolResult("scheduled")


class CheckerMustNotRun:
    def check(self, tool, arguments):
        raise AssertionError("external approval must not be checked a second time")


def _context(tmp_path: Path, *, writes=("approved.txt",), commands=None) -> ExecutionContext:
    return ExecutionContext(
        task_id="task-bound",
        cwd=str(tmp_path),
        workspace_root=str(tmp_path),
        plan_hash="plan-sha256",
        expected_pre_state_hash="pre-sha256",
        write_set=writes,
        commands=commands,
        network_hosts=(),
    )


def _gateway(tmp_path: Path, store: RecoveryStore, context: ExecutionContext) -> ExecutionGateway:
    return ExecutionGateway(
        permission_checker=CheckerMustNotRun(),
        risk_engine=RiskEngine(workspace_root=tmp_path),
        execution_context=context,
        action_journal=RecoveryExecutionCoordinator(store),
    )


@pytest.mark.asyncio
async def test_preview_then_exact_grant_skips_second_ask_and_journals(tmp_path: Path) -> None:
    with RecoveryStore(database_path=tmp_path / "runtime.db") as store:
        context = _context(tmp_path)
        gateway = _gateway(tmp_path, store, context)
        tool = WriteTool()
        assessment = gateway.preview(
            tool,
            {"file_path": "approved.txt", "content": "secret value"},
            invocation_id="call-1",
        )
        assert assessment.valid
        assert assessment.risk is not None and str(assessment.risk.level) == "L1"
        grant = gateway.issue_grant(assessment, approver="user:interactive")

        result = await gateway.execute(
            tool,
            ToolInvocation(
                invocation_id="call-1",
                tool_name=tool.name,
                arguments={"file_path": "approved.txt", "content": "secret value"},
            ),
            grant=grant,
        )

        assert result.status is ExecutionStatus.SUCCEEDED
        action = store.list_actions(task_id="task-bound")[0]
        ticket = store.get_ticket(action.ticket_id or "")
        assert action.state is ActionState.SUCCEEDED
        assert action.normalized_args_hash == assessment.arguments_hash == result.arguments_hash
        assert action.cwd_realpath == canonical_realpath(tmp_path)
        assert action.plan_hash == "plan-sha256"
        assert ticket is not None and ticket.approver == "invocation_grant:user:interactive"


@pytest.mark.asyncio
async def test_grant_cannot_be_replayed_or_used_after_argument_change(tmp_path: Path) -> None:
    with RecoveryStore(database_path=tmp_path / "runtime.db") as store:
        context = _context(tmp_path)
        gateway = _gateway(tmp_path, store, context)
        first_tool = WriteTool()
        assessment = gateway.preview(
            first_tool,
            {"file_path": "approved.txt", "content": "v1"},
            invocation_id="call-exact",
        )
        grant = gateway.issue_grant(assessment, approver="user")
        invocation = ToolInvocation(
            invocation_id="call-exact",
            tool_name=first_tool.name,
            arguments={"file_path": "approved.txt", "content": "v1"},
        )
        assert (await gateway.execute(first_tool, invocation, grant=grant)).status is ExecutionStatus.SUCCEEDED

        replay = await gateway.execute(first_tool, invocation, grant=grant)
        assert replay.status is ExecutionStatus.PERMISSION_DENIED
        assert ReasonCode.INVOCATION_GRANT_CONSUMED.value in replay.reason_codes

        changed_assessment = gateway.preview(
            WriteTool(),
            {"file_path": "approved.txt", "content": "old"},
            invocation_id="call-changed",
        )
        changed_grant = gateway.issue_grant(changed_assessment, approver="user")
        changed_tool = WriteTool()
        changed = await gateway.execute(
            changed_tool,
            ToolInvocation(
                invocation_id="call-changed",
                tool_name=changed_tool.name,
                arguments={"file_path": "approved.txt", "content": "new"},
            ),
            grant=changed_grant,
        )
        assert changed.status is ExecutionStatus.PERMISSION_DENIED
        assert ReasonCode.INVOCATION_GRANT_INVALID.value in changed.reason_codes
        assert changed_tool.calls == 0


@pytest.mark.asyncio
async def test_l3_external_ticket_is_bound_and_successfully_closed(tmp_path: Path) -> None:
    with RecoveryStore(database_path=tmp_path / "runtime.db") as store:
        context = ExecutionContext(
            task_id="task-l3",
            cwd=str(tmp_path),
            workspace_root=str(tmp_path),
            plan_hash="plan-l3",
            expected_pre_state_hash="pre-l3",
            network_hosts=("api.example",),
        )
        gateway = _gateway(tmp_path, store, context)
        tool = ExternalWriteTool()
        assessment = gateway.preview(
            tool,
            {"url": "https://api.example/releases"},
            invocation_id="external-l3",
        )
        assert assessment.valid
        assert assessment.risk is not None and str(assessment.risk.level) == "L3"
        result = await gateway.execute(
            tool,
            ToolInvocation(
                invocation_id="external-l3",
                tool_name=tool.name,
                arguments={"url": "https://api.example/releases"},
            ),
            grant=gateway.issue_grant(assessment, approver="release-owner"),
        )
        assert result.status is ExecutionStatus.SUCCEEDED
        action = store.list_actions(task_id="task-l3")[0]
        ticket = store.get_ticket(action.ticket_id or "")
        assert action.state is ActionState.SUCCEEDED
        assert action.normalized_args_hash == assessment.arguments_hash
        assert action.plan_hash == "plan-l3"
        assert action.expected_pre_state_hash == "pre-l3"
        assert ticket is not None and ticket.use_count == 1


def test_preview_rejects_schema_and_manifest_violations(tmp_path: Path) -> None:
    context = _context(tmp_path, commands=(("pytest", "-q"),))
    gateway = ExecutionGateway(
        risk_engine=RiskEngine(workspace_root=tmp_path),
        execution_context=context,
    )
    write = gateway.preview(
        WriteTool(),
        {"file_path": "not-approved.txt", "content": "x"},
    )
    assert not write.valid
    assert "manifest.write_set_violation" in write.reason_codes

    command = gateway.preview(InterruptingCommand(), {"command": "python setup.py publish"})
    assert not command.valid
    assert "manifest.command_violation" in command.reason_codes
    assert gateway.preview(InternalCommandTool(), {"task": "verify"}).valid

    forged = gateway.preview(
        WriteTool(),
        {"file_path": "approved.txt", "content": "x", "grant": "model-says-yes"},
    )
    assert not forged.valid
    assert ReasonCode.VALIDATION_UNEXPECTED_ARGUMENT.value in forged.reason_codes

    allowed_network = ExecutionContext(
        task_id="task-network",
        cwd=str(tmp_path),
        workspace_root=str(tmp_path),
        plan_hash="network-plan",
        network_hosts=("packages.example",),
    )
    network_gateway = ExecutionGateway(
        risk_engine=RiskEngine(workspace_root=tmp_path),
        execution_context=allowed_network,
    )
    assert network_gateway.preview(
        NetworkTool(), {"url": "https://packages.example/archive.whl"}
    ).valid
    denied_network = network_gateway.preview(
        NetworkTool(), {"url": "https://evil.example/exfiltrate"}
    )
    assert not denied_network.valid
    assert "manifest.network_violation" in denied_network.reason_codes


@pytest.mark.asyncio
async def test_tool_error_is_failed_but_command_interruption_is_uncertain(tmp_path: Path) -> None:
    with RecoveryStore(database_path=tmp_path / "runtime.db") as store:
        context = _context(tmp_path, commands=(("pytest", "-q"),))
        gateway = _gateway(tmp_path, store, context)
        write = WriteTool(error=True)
        write_assessment = gateway.preview(
            write,
            {"file_path": "approved.txt", "content": "x"},
            invocation_id="write-error",
        )
        write_result = await gateway.execute(
            write,
            ToolInvocation(
                invocation_id="write-error",
                tool_name=write.name,
                arguments={"file_path": "approved.txt", "content": "x"},
            ),
            grant=gateway.issue_grant(write_assessment, approver="user"),
        )
        assert write_result.status is ExecutionStatus.TOOL_ERROR
        assert store.list_actions(task_id="task-bound")[0].state is ActionState.FAILED

        command = InterruptingCommand()
        command_assessment = gateway.preview(
            command,
            {"command": "pytest -q"},
            invocation_id="command-interrupt",
        )
        with pytest.raises(asyncio.CancelledError):
            await gateway.execute(
                command,
                ToolInvocation(
                    invocation_id="command-interrupt",
                    tool_name=command.name,
                    arguments={"command": "pytest -q"},
                ),
                grant=gateway.issue_grant(command_assessment, approver="user"),
            )
        states = {action.idempotency_key: action.state for action in store.list_actions()}
        assert states["invocation:command-interrupt"] is ActionState.UNCERTAIN
