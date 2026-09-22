"""Cross-module acceptance tests exercising the product entry points and services."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from eviforge import __main__ as cli
from eviforge.automation import RunEvent, RunResult
from eviforge.client import LLMClient, NetworkError, AmbiguousStreamError
from eviforge.hooks import Action, Hook, HookContext, HookEngine
from eviforge.permissions import PermissionMode
from eviforge.runtime import RuntimeServices
from eviforge.tools.base import TextDelta, ToolCallComplete, StreamEnd
from test_document_headless import isolated_project


class Scripted(LLMClient):
    def __init__(self, turns):
        self.turns = iter(turns)
        self.requests = []
        self.closed = 0

    async def stream(self, conversation, system="", tools=None):
        self.requests.append((list(conversation.history), system, tools))
        for item in next(self.turns):
            if isinstance(item, BaseException):
                raise item
            yield item

    async def aclose(self):
        self.closed += 1


def turn(*events):
    return [*events, StreamEnd("end_turn", input_tokens=3, output_tokens=2)]


@pytest.mark.parametrize("output", ["json", "jsonl"])
def test_real_cli_machine_output_and_session_resume(isolated_project, monkeypatch, capsys, output):
    client = Scripted([turn(TextDelta("one")), turn(TextDelta("two"))])
    monkeypatch.setattr("eviforge.client.create_client", lambda _: client)
    monkeypatch.setattr(sys, "argv", ["eviforge", "-p", "first-marker", "--output", output])
    cli.main()
    lines = capsys.readouterr().out.strip().splitlines()
    if output == "jsonl":
        events = [RunEvent.model_validate_json(line) for line in lines]
        assert [e.sequence for e in events] == list(range(1, len(events) + 1))
        assert events[-1].type == "run_result"
        result = RunResult.model_validate(events[-1].data)
    else:
        assert len(lines) == 1
        result = RunResult.model_validate_json(lines[0])
    assert (result.status, result.exit_code, result.output) == ("success", 0, "one")
    assert result.input_tokens == 3 and result.output_tokens == 2
    saved = Path(".eviforge/runs") / f"{result.run_id}.result.json"
    assert RunResult.model_validate_json(saved.read_text()) == result
    monkeypatch.setattr(sys, "argv", ["eviforge", "-p", "second-marker", "--output", "json",
                                      "--resume-session", result.session_id])
    cli.main()
    resumed = RunResult.model_validate_json(capsys.readouterr().out.strip())
    assert resumed.session_id == result.session_id and resumed.run_id != result.run_id
    assert {"first-marker", "second-marker", "one"} <= {m.content for m in client.requests[-1][0]}
    assert client.closed == 2


@pytest.mark.parametrize("output", ["json", "jsonl"])
def test_cli_partial_is_ambiguous_once_and_persists_result(isolated_project, monkeypatch, capsys, output):
    client = Scripted([[TextDelta("partial"), NetworkError("connection dropped")]])
    monkeypatch.setattr("eviforge.client.create_client", lambda _: client)
    monkeypatch.setattr(sys, "argv", ["eviforge", "-p", "test", "--output", output])
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    assert exit_info.value.code == 4
    raw = capsys.readouterr().out.strip().splitlines()
    result = RunResult.model_validate_json(raw[-1]) if output == "json" else RunResult.model_validate(json.loads(raw[-1])["data"])
    assert result.status == "ambiguous" and result.output == "partial"
    assert len(client.requests) == 1 and client.closed == 1
    assert Path(result.events_path).exists()


def test_headless_denied_action_is_blocked_even_if_model_claims_success(isolated_project, monkeypatch, capsys):
    client = Scripted([turn(ToolCallComplete("x", "WriteFile", {"file_path": "denied.txt", "content": "no"})),
                       turn(TextDelta("I completed it"))])
    monkeypatch.setattr("eviforge.client.create_client", lambda _: client)
    monkeypatch.setattr(sys, "argv", ["eviforge", "-p", "test", "--mode", "default", "--output", "json"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    result = RunResult.model_validate_json(capsys.readouterr().out)
    assert exc.value.code == 3 and result.status == "blocked"
    assert not Path("denied.txt").exists()


@pytest.mark.asyncio
async def test_plan_mode_submit_then_real_cli_approve_exact_hash(isolated_project, monkeypatch, capsys):
    config = isolated_project
    client = Scripted([])
    runtime = RuntimeServices.create(config, config.providers[0], client=client,
        work_dir=str(Path.cwd()), permission_mode=PermissionMode.PLAN)
    runtime.begin_turn()
    plan_path = runtime.agent._get_plan_path()
    args = {"file_path": "approved.txt", "content": "approved"}
    client.turns = iter([
        turn(ToolCallComplete("draft", "WriteFile", {"file_path": str(plan_path), "content": "Write approved.txt"})),
        turn(ToolCallComplete("exit", "ExitPlanMode", {"actions": [{"tool_name": "WriteFile", "arguments": args}]})),
    ])
    try:
        await runtime.agent.run_to_completion("Plan the change", runtime.conversation)
        assert runtime.agent.last_run_status == "approval_required"
        assert not Path("approved.txt").exists()
        snapshot = runtime.plan_service.current_plan(runtime.agent)
        for message in runtime.conversation.history:
            runtime.session.append(message)
        session_id = runtime.agent.session_id
    finally:
        await runtime.close()
    approved_client = Scripted([turn(ToolCallComplete("write", "WriteFile", args)), turn(TextDelta("done"))])
    monkeypatch.setattr("eviforge.client.create_client", lambda _: approved_client)
    options = cli.AutomationOptions(output_format="json", resume_session=session_id,
        approved_plan=snapshot.plan_id, plan_hash=snapshot.content_hash)
    result = await cli._run_prompt_with_hook_cleanup(config, PermissionMode.DEFAULT, None, "Execute it", options=options)
    assert result.status == "success" and Path("approved.txt").read_text() == "approved"
    assert result.metadata["plan"]["state"] == "completed"
    assert len(approved_client.requests) == 2 and approved_client.closed == 1


@pytest.mark.asyncio
async def test_wrong_approval_hash_never_calls_provider(isolated_project, monkeypatch):
    from eviforge.memory.session import SessionManager
    from eviforge.planning import PlanService, PlanError
    session = SessionManager(str(Path.cwd())).create()
    service = PlanService(Path.cwd())
    snapshot = service.submit(service.create(session.session_id, "source", "reviewed").plan_id)
    session.close()
    client = Scripted([])
    monkeypatch.setattr("eviforge.client.create_client", lambda _: client)
    options = cli.AutomationOptions(output_format="json", resume_session=session.session_id,
                                   approved_plan=snapshot.plan_id, plan_hash="0" * 64)
    with pytest.raises(PlanError, match="PLAN_CHANGED"):
        await cli._run_prompt(isolated_project, PermissionMode.BYPASS, None, "Execute", options=options)
    assert not client.requests and client.closed == 1 and options.result.status == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("interactive", [False, True])
async def test_common_assembly_and_public_lifecycle(isolated_project, interactive):
    client = Scripted([])
    runtime = RuntimeServices.create(isolated_project, isolated_project.providers[0], client=client,
        work_dir=str(Path.cwd()), interactive=interactive)
    tools = {tool.name for tool in runtime.registry.list_tools()}
    assert {"Agent", "TeamCreate", "TeamDelete", "LoadSkill", "ToolSearch", "ExitPlanMode", "HttpRequest"} <= tools
    assert runtime.agent.memory_manager.governance is runtime.governance
    assert runtime.agent.skill_loader.governance is runtime.governance
    assert runtime.agent.plan_service is runtime.plan_service
    assert runtime.registry.is_enabled("AskUserQuestion") == interactive
    started, finalized = asyncio.Event(), asyncio.Event()
    async def worker():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            finalized.set()
    runtime.agent.spawn_background(worker(), name="test-runtime-lifecycle")
    await started.wait()
    with pytest.raises(TimeoutError):
        await runtime.wait(timeout=0.001)
    await asyncio.gather(runtime.close(), runtime.close())
    await runtime.close()
    assert finalized.is_set() and client.closed == 1
    assert not runtime.pending_workers() and not runtime.lifecycle.pending()


@pytest.mark.asyncio
async def test_hook_agent_uses_real_readonly_child_before_client_close(isolated_project, monkeypatch):
    Path("input.txt").write_text("hook-evidence")
    client = Scripted([
        turn(ToolCallComplete("read", "ReadFile", {"file_path": "input.txt"})), turn(TextDelta("startup checked")),
        turn(TextDelta("main result")), turn(TextDelta("shutdown checked")),
    ])
    engine = HookEngine([
        Hook(id="start", event="startup", action=Action(type="agent", prompt="read input.txt")),
        Hook(id="stop", event="shutdown", action=Action(type="agent", prompt="review shutdown")),
    ])
    monkeypatch.setattr("eviforge.client.create_client", lambda _: client)
    result = await cli._run_prompt_with_hook_cleanup(isolated_project, PermissionMode.BYPASS, engine, "main")
    assert result.status == "success" and result.output == "main result"
    assert len(client.requests) == 4 and client.closed == 1
    for i in (0, 1, 3):
        assert {t["name"] for t in client.requests[i][2]} == {"ReadFile", "Glob", "Grep"}
    assert any("hook-evidence" in r.content for m in client.requests[1][0] for r in m.tool_results)


@pytest.mark.asyncio
async def test_two_stdio_mcp_servers_close_from_different_tasks(tmp_path):
    from eviforge.config import MCPServerConfig
    from eviforge.mcp.client import MCPClient
    server = tmp_path / "server.py"
    server.write_text('from mcp.server.fastmcp import FastMCP\ns=FastMCP("owned")\n@s.tool()\ndef ping() -> str: return "pong"\ns.run(transport="stdio")\n')
    clients = [MCPClient(MCPServerConfig(name=f"server{i}", command=sys.executable, args=[str(server)])) for i in range(2)]
    try:
        async with asyncio.timeout(25):
            await asyncio.gather(*(c.connect() for c in clients))
            results = await asyncio.gather(*(c.call_tool("ping", {}) for c in clients))
            assert all("pong" in str(result) for result in results)
            await asyncio.gather(*(c.close() for c in clients))
            assert all(c._owner_task is None or c._owner_task.done() for c in clients)
    finally:
        await asyncio.gather(*(c.close() for c in clients))
