from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncIterator

import pytest
from pydantic import BaseModel

from mewcode.agent import (
    Agent,
    CompletionBlockedError,
    CompletionBlockedEvent,
    LoopComplete,
    PermissionRequest,
    PermissionResponse,
    ToolResultEvent,
)
from mewcode.client import LLMClient
from mewcode.conversation import ConversationManager
from mewcode.evidence import RequirementContract, RequirementCriterion
from mewcode.tools import ToolRegistry
from mewcode.tools.base import StreamEnd, StreamEvent, TextDelta, Tool, ToolCallComplete, ToolResult
from mewcode.runtime import RuntimeStore, TaskRuntime, TaskState
from mewcode.permissions import (
    DangerousCommandDetector,
    PathSandbox,
    PermissionChecker,
    PermissionMode,
    RuleEngine,
)
from mewcode.runtime import RuntimeBuilder
from mewcode.execution import ExecutionContext
from mewcode.permissions import Decision


class ScriptClient(LLMClient):
    def __init__(self, responses: list[list[StreamEvent]]) -> None:
        self.responses = responses
        self.index = 0

    async def stream(
        self,
        conversation: ConversationManager,
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        response = self.responses[self.index]
        self.index += 1
        for event in response:
            yield event


class Params(BaseModel):
    value: str


class RecordingTool(Tool):
    name = "Record"
    description = "record"
    params_model = Params
    category = "read"
    is_concurrency_safe = True

    def __init__(self) -> None:
        self.direct_calls = 0

    async def execute(self, params: BaseModel) -> ToolResult:
        self.direct_calls += 1
        return ToolResult(output=f"direct:{params.value}")


class DangerousParams(BaseModel):
    command: str


class DangerousTool(Tool):
    name = "Bash"
    description = "shell"
    params_model = DangerousParams
    category = "command"

    def __init__(self) -> None:
        self.executed = False

    async def execute(self, params: BaseModel) -> ToolResult:
        self.executed = True
        return ToolResult(output="should never run")


class ControlledWriteParams(BaseModel):
    file_path: str


class ControlledWriteTool(Tool):
    name = "BoundWrite"
    description = "write fixture"
    params_model = ControlledWriteParams
    category = "write"
    is_concurrency_safe = False

    def __init__(self) -> None:
        self.direct_calls = 0

    async def execute(self, params: BaseModel) -> ToolResult:
        self.direct_calls += 1
        return ToolResult(output=f"wrote:{params.file_path}")


class RecordingGateway:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.invocations: list[Any] = []
        self.error = error

    async def execute(self, tool: Tool, invocation: Any) -> ToolResult:
        self.invocations.append(invocation)
        if self.error:
            raise self.error
        return ToolResult(output=f"gateway:{invocation.arguments['value']}")


class FakeOrchestrator:
    def __init__(self, verdicts: list[str], *, diff_hash: str = "same-diff") -> None:
        self.verdicts = verdicts
        self.diff_hash = diff_hash
        self.calls = 0

    async def run(self, contract: Any, *, repo_root: str) -> Any:
        verdict = self.verdicts[self.calls]
        self.calls += 1
        return SimpleNamespace(
            decision=SimpleNamespace(
                verdict=SimpleNamespace(value=verdict),
                current_diff_sha256=self.diff_hash,
                reasons=() if verdict == "PASS" else ("tests are incomplete",),
            ),
            bundle=SimpleNamespace(directory=Path(repo_root) / "evidence"),
        )


class FakeEvolutionService:
    def structured_failures(self, task_id: str) -> tuple[str, ...]:
        return ("typed-tool-failure",)


class RecordingEvolutionAdapter:
    def __init__(self) -> None:
        self.service = FakeEvolutionService()
        self.queued: list[Any] = []
        self.flushed: list[str] = []

    def queue(self, draft: Any) -> None:
        self.queued.append(draft)

    def flush_task(self, task_id: str) -> object:
        self.flushed.append(task_id)
        return object()


class DenyAllChecker:
    mode = PermissionMode.DEFAULT

    def check(self, tool: Tool, arguments: dict[str, Any]) -> Decision:
        return Decision(effect="deny", reason="fixture denies every call")


def make_agent(
    responses: list[list[StreamEvent]],
    gateway: RecordingGateway,
    **kwargs: Any,
) -> tuple[Agent, RecordingTool]:
    registry = ToolRegistry()
    tool = RecordingTool()
    registry.register(tool)
    return Agent(ScriptClient(responses), registry, "anthropic", execution_gateway=gateway, **kwargs), tool


@pytest.mark.asyncio
async def test_all_three_execution_paths_use_gateway() -> None:
    gateway = RecordingGateway()
    agent, tool = make_agent([], gateway)
    tc = ToolCallComplete("one", "Record", {"value": "x"})

    direct = await agent._execute_single_tool_direct(tc)
    interactive_items = [item async for item in agent._execute_tool(tc)]
    noninteractive = await agent._execute_tool_noninteractive(tc)

    assert len(gateway.invocations) == 3
    assert tool.direct_calls == 0
    assert direct.result.output == "gateway:x"
    assert interactive_items[-1][0].output == "gateway:x"
    assert noninteractive.output == "gateway:x"


@pytest.mark.asyncio
async def test_default_gateway_parallel_read_cannot_bypass_supplied_checker() -> None:
    registry = ToolRegistry()
    tool = RecordingTool()
    registry.register(tool)
    agent = Agent(
        ScriptClient([]),
        registry,
        "anthropic",
        permission_checker=DenyAllChecker(),  # type: ignore[arg-type]
    )

    result = await agent._execute_single_tool_direct(
        ToolCallComplete("blocked-read", "Record", {"value": "secret"})
    )

    assert result.result.is_error
    assert "fixture denies every call" in result.result.output
    assert tool.direct_calls == 0


@pytest.mark.asyncio
async def test_gateway_exception_is_converted_to_tool_error() -> None:
    gateway = RecordingGateway(error=RuntimeError("L4 denied"))
    agent, _ = make_agent([], gateway)
    result = await agent._execute_tool_noninteractive(
        ToolCallComplete("l4", "Record", {"value": "x", "model_authorized": True})
    )
    assert result.is_error
    assert "L4 denied" in result.output


@pytest.mark.asyncio
async def test_interactive_allow_uses_exact_grant_without_second_ask(
    tmp_path: Path,
) -> None:
    checker = PermissionChecker(
        detector=DangerousCommandDetector(),
        sandbox=PathSandbox(str(tmp_path)),
        rule_engine=RuleEngine(),
        mode=PermissionMode.DEFAULT,
    )
    components = RuntimeBuilder(
        tmp_path,
        control_root=tmp_path / "trusted-control",
        permission_checker=checker,
    ).build(task_id="approval-task")
    registry = ToolRegistry()
    tool = ControlledWriteTool()
    registry.register(tool)
    context = ExecutionContext(
        task_id="approval-task",
        cwd=str(tmp_path),
        workspace_root=str(tmp_path),
        plan_hash="reviewed-plan",
        write_set=("approved.txt",),
        commands=(),
        network_hosts=(),
    )
    agent = Agent(
        ScriptClient([]),
        registry,
        "anthropic",
        work_dir=str(tmp_path),
        permission_checker=checker,
        execution_gateway=components.gateway,
        execution_context=context,
        task_runtime=components.task,
    )
    components.gateway.execution_context = context

    stream = agent._execute_tool(
        ToolCallComplete("write-once", tool.name, {"file_path": "approved.txt"})
    )
    request = await anext(stream)
    assert isinstance(request, PermissionRequest)
    assert request.risk_level == "L1"
    assert request.approval_scope == "audited_session"
    request.future.set_result(PermissionResponse.ALLOW)
    result, _elapsed, _unknown = await anext(stream)

    assert not result.is_error
    assert tool.direct_calls == 1
    actions = components.recovery.list_actions(task_id="approval-task")
    assert len(actions) == 1
    assert actions[0].state.value == "succeeded"
    components.close()


@pytest.mark.asyncio
async def test_manifest_violation_is_not_presented_as_approvable(tmp_path: Path) -> None:
    checker = PermissionChecker(
        detector=DangerousCommandDetector(),
        sandbox=PathSandbox(str(tmp_path)),
        rule_engine=RuleEngine(),
        mode=PermissionMode.DEFAULT,
    )
    components = RuntimeBuilder(
        tmp_path,
        control_root=tmp_path / "trusted-control",
        permission_checker=checker,
    ).build(task_id="manifest-task")
    registry = ToolRegistry()
    tool = ControlledWriteTool()
    registry.register(tool)
    context = ExecutionContext(
        task_id="manifest-task",
        cwd=str(tmp_path),
        workspace_root=str(tmp_path),
        plan_hash="reviewed-plan",
        write_set=("approved.txt",),
        commands=(),
        network_hosts=(),
    )
    agent = Agent(
        ScriptClient([]),
        registry,
        "anthropic",
        work_dir=str(tmp_path),
        permission_checker=checker,
        execution_gateway=components.gateway,
        execution_context=context,
    )
    components.gateway.execution_context = context

    items = [
        item
        async for item in agent._execute_tool(
            ToolCallComplete("outside-plan", tool.name, {"file_path": "other.txt"})
        )
    ]
    assert len(items) == 1
    result, _elapsed, _unknown = items[0]
    assert result.is_error
    assert "manifest" in result.output.lower()
    assert tool.direct_calls == 0
    components.close()


@pytest.mark.asyncio
async def test_default_gateway_l4_cannot_be_bypassed_by_model_arguments(tmp_path: Path) -> None:
    registry = ToolRegistry()
    tool = DangerousTool()
    registry.register(tool)
    agent = Agent(ScriptClient([]), registry, "anthropic", work_dir=str(tmp_path))
    result = await agent._execute_tool_noninteractive(
        ToolCallComplete("l4", "Bash", {"command": "rm -rf /"})
    )
    assert result.is_error
    assert "L4" in result.output
    assert tool.executed is False


@pytest.mark.asyncio
async def test_default_gateway_rejects_unexpected_arguments(tmp_path: Path) -> None:
    registry = ToolRegistry()
    tool = RecordingTool()
    registry.register(tool)
    agent = Agent(ScriptClient([]), registry, "anthropic", work_dir=str(tmp_path))
    result = await agent._execute_tool_noninteractive(
        ToolCallComplete("strict", "Record", {"value": "x", "model_authorized": True})
    )
    assert result.is_error
    assert "Unexpected argument" in result.output
    assert tool.direct_calls == 0


def contract() -> RequirementContract:
    return RequirementContract(
        task_id="task",
        objective="finish",
        criteria=(RequirementCriterion(criterion_id="tests", description="tests pass"),),
    )


def test_setting_contract_resets_previous_gate_state() -> None:
    agent, _ = make_agent([], RecordingGateway())
    agent._evidence_passed = True
    agent._completion_gate_exhausted = True

    agent.set_requirement_contract(contract(), orchestrator=FakeOrchestrator(["PASS"]))

    assert agent.requirement_contract is not None
    assert agent._evidence_passed is False
    assert agent._completion_gate_exhausted is False
    assert agent._last_completion_block is None


@pytest.mark.asyncio
async def test_interactive_partial_continues_and_only_pass_completes() -> None:
    responses = [
        [TextDelta("I am done"), StreamEnd("end_turn")],
        [TextDelta("now really done"), StreamEnd("end_turn")],
    ]
    orchestrator = FakeOrchestrator(["PARTIAL", "PASS"])
    agent, _ = make_agent(
        responses,
        RecordingGateway(),
        requirement_contract=contract(),
        evidence_orchestrator=orchestrator,
    )
    conversation = ConversationManager()
    events = [event async for event in agent.run(conversation)]

    assert [event.verdict for event in events if isinstance(event, CompletionBlockedEvent)] == ["PARTIAL"]
    assert len([event for event in events if isinstance(event, LoopComplete)]) == 1
    assert any("verification-feedback" in message.content for message in conversation.history)


@pytest.mark.asyncio
async def test_contract_none_preserves_old_completion() -> None:
    agent, _ = make_agent(
        [[TextDelta("done"), StreamEnd("end_turn")]],
        RecordingGateway(),
    )
    events = [event async for event in agent.run(ConversationManager())]
    assert len([event for event in events if isinstance(event, LoopComplete)]) == 1
    assert not any(isinstance(event, CompletionBlockedEvent) for event in events)


@pytest.mark.asyncio
async def test_headless_uses_same_gate_and_refuses_non_pass() -> None:
    agent, _ = make_agent(
        [
            [TextDelta("done"), StreamEnd("end_turn")],
            [TextDelta("still done"), StreamEnd("end_turn")],
        ],
        RecordingGateway(),
        requirement_contract=contract(),
        evidence_orchestrator=FakeOrchestrator(["PARTIAL", "PARTIAL"]),
    )
    callbacks: list[dict[str, Any]] = []
    with pytest.raises(CompletionBlockedError):
        await agent.run_to_completion("task", event_callback=callbacks.append)
    assert [item["verdict"] for item in callbacks if item["type"] == "completion_blocked"] == [
        "PARTIAL",
        "PARTIAL",
    ]


@pytest.mark.asyncio
async def test_headless_partial_then_pass_returns_text() -> None:
    agent, _ = make_agent(
        [
            [TextDelta("first claim"), StreamEnd("end_turn")],
            [TextDelta("verified claim"), StreamEnd("end_turn")],
        ],
        RecordingGateway(),
        requirement_contract=contract(),
        evidence_orchestrator=FakeOrchestrator(["PARTIAL", "PASS"]),
    )
    assert await agent.run_to_completion("task") == "verified claim"


@pytest.mark.asyncio
async def test_evidence_gate_drives_the_durable_runtime_fsm(tmp_path: Path) -> None:
    store = RuntimeStore(control_root=tmp_path / "control", workspace_id="agent-fsm")
    runtime = TaskRuntime.create(store, task_id="task", trace_id="trace")
    agent, _ = make_agent(
        [
            [TextDelta("first claim"), StreamEnd("end_turn")],
            [TextDelta("verified claim"), StreamEnd("end_turn")],
        ],
        RecordingGateway(),
        task_runtime=runtime,
    )
    agent.set_requirement_contract(
        contract(), orchestrator=FakeOrchestrator(["FAIL", "PASS"])
    )
    agent.begin_contract_execution()

    assert await agent.run_to_completion("task") == "verified claim"
    assert runtime.run.state is TaskState.COMPLETED
    transitions = [
        item.event.payload.get("new_state")
        for item in store.list_events(task_id="task")
        if item.event.event_type == "task_state_changed"
    ]
    assert TaskState.REPLANNING.value in transitions
    assert transitions[-1] == TaskState.COMPLETED.value
    store.close()


@pytest.mark.asyncio
async def test_verified_repair_is_queued_as_quarantined_evolution_draft(
    tmp_path: Path,
) -> None:
    # Evolution requires an immutable source commit; a tiny real repository
    # proves the Agent binds the candidate to HEAD and the verified diff hash.
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "EviForge Test"], cwd=tmp_path, check=True
    )
    (tmp_path / "seed.txt").write_text("seed", encoding="utf-8")
    subprocess.run(["git", "add", "seed.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=tmp_path, check=True)

    store = RuntimeStore(control_root=tmp_path / "control", workspace_id="evolution-fsm")
    runtime = TaskRuntime.create(store, task_id="evolution-task", trace_id="trace")
    evolution = RecordingEvolutionAdapter()
    agent, _ = make_agent(
        [[TextDelta("verified"), StreamEnd("end_turn")]],
        RecordingGateway(),
        work_dir=str(tmp_path),
        task_runtime=runtime,
        evolution_adapter=evolution,
    )
    agent.set_requirement_contract(
        contract(), orchestrator=FakeOrchestrator(["PASS"], diff_hash="a" * 64)
    )
    agent.begin_contract_execution()

    assert await agent.run_to_completion("task") == "verified"
    assert len(evolution.queued) == 1
    draft = evolution.queued[0]
    assert draft.task_id == "evolution-task"
    assert draft.source_code_hash == "a" * 64
    assert evolution.flushed == ["evolution-task"]
    assert runtime.run.state is TaskState.EVOLUTION_PENDING
    store.close()
