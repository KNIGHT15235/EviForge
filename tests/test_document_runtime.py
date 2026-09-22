"""Offline integration checks for the modules named in EviForge.md."""
from __future__ import annotations

import asyncio
import json
import shlex
import sys
from pathlib import Path

import pytest

from likecc.agent import Agent
from likecc.client import LLMClient
from likecc.config import MCPServerConfig, ProviderConfig
from likecc.conversation import ConversationManager
from likecc.tools import create_default_registry
from likecc.tools.base import StreamEnd, TextDelta, ToolCallComplete


class ScriptedClient(LLMClient):
    def __init__(self, replies):
        self.replies = iter(replies)
        self.requests = []

    async def stream(self, conversation, system="", tools=None):
        self.requests.append((conversation.get_messages(), system, tools))
        for event in next(self.replies):
            yield event


def turn(*events):
    return [*events, StreamEnd("end_turn", input_tokens=10, output_tokens=5)]


@pytest.mark.asyncio
async def test_six_real_tools_complete_a_file_workflow(tmp_path):
    target = tmp_path / "sample.py"
    check = f"from pathlib import Path; assert Path({str(target)!r}).read_text() == 'value = 2\\n'; print('checked')"
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(check)}"
    client = ScriptedClient([
        turn(ToolCallComplete("write", "WriteFile", {"file_path": str(target), "content": "value = 1\n"})),
        turn(ToolCallComplete("read", "ReadFile", {"file_path": str(target)})),
        turn(ToolCallComplete("edit", "EditFile", {"file_path": str(target), "old_string": "value = 1", "new_string": "value = 2"})),
        turn(ToolCallComplete("glob", "Glob", {"pattern": "*.py"})),
        turn(ToolCallComplete("grep", "Grep", {"pattern": "value = 2"})),
        turn(ToolCallComplete("bash", "Bash", {"command": command})),
        turn(TextDelta("verified")),
    ])
    agent = Agent(client, create_default_registry(), "anthropic", work_dir=str(tmp_path))
    conversation = ConversationManager()
    assert await agent.run_to_completion("update and verify", conversation) == "verified"
    results = [r for msg in conversation.history for r in msg.tool_results]
    assert len(results) == 6
    assert all(not r.is_error for r in results), results
    assert "sample.py" in results[3].content
    assert "value = 2" in results[4].content
    assert "checked" in results[5].content
    assert target.read_text() == "value = 2\n"


@pytest.mark.asyncio
async def test_real_mcp_stdio_discover_execute_reconnect_and_close(tmp_path):
    from likecc.mcp.client import MCPClient
    from likecc.mcp.tool_wrapper import MCPToolWrapper

    server = tmp_path / "mcp_fixture.py"
    server.write_text(
        'from mcp.server.fastmcp import FastMCP\n'
        'server = FastMCP("offline-audit")\n'
        '@server.tool()\n'
        'def add(a: int, b: int) -> int:\n'
        '    return a + b\n'
        'server.run(transport="stdio")\n', encoding="utf-8",
    )
    client = MCPClient(MCPServerConfig(name="audit", command=sys.executable, args=[str(server)]))
    try:
        async with asyncio.timeout(20):
            await client.connect()
            definitions = await client.list_tools()
            definition = next(t for t in definitions if t.name == "add")
            wrapper = MCPToolWrapper("audit", definition, client)
            from likecc.agents.parser import AgentDef
            from likecc.agents.tool_filter import build_teammate_tools, resolve_agent_tools

            registry = create_default_registry()
            registry.register(wrapper)
            child_registry = resolve_agent_tools(
                registry, AgentDef(agent_type="worker", when_to_use="audit", system_prompt=""),
                is_background=True,
            )
            assert child_registry.get(wrapper.name) is wrapper
            teammate_registry = build_teammate_tools(
                registry, object(), "audit-team", "worker-id", "worker", "in-process",
            )
            assert teammate_registry.get(wrapper.name) is wrapper
            result = await wrapper.execute(wrapper.params_model(a=2, b=3))
            assert not result.is_error
            assert json.loads(result.output) == 5
            await client.close()
            assert not client.is_alive
            result = await wrapper.execute(wrapper.params_model(a=6, b=7))
            assert not result.is_error
            assert json.loads(result.output) == 13
    finally:
        await client.close()
    assert not client.is_alive
    assert client._stack is None


@pytest.mark.asyncio
async def test_memory_extract_reload_and_inject_next_session(tmp_path, monkeypatch):
    from likecc.memory.auto_memory import MemoryManager

    home = tmp_path / "home"
    project = tmp_path / "project"
    home.mkdir()
    project.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    text = "### 用户偏好\n- use four spaces\n### 项目知识\n- tests use pytest\n"
    extractor = ScriptedClient([turn(TextDelta(text))])
    first = MemoryManager(str(project))
    source = ConversationManager()
    source.add_user_message("Remember my preferences")
    await first.extract(extractor, source, "anthropic")
    second = MemoryManager(str(project))
    assert "use four spaces" in second.load()
    assert "tests use pytest" in second.load()
    client = ScriptedClient([turn(TextDelta("ready"))])
    agent = Agent(client, create_default_registry(), "anthropic", work_dir=str(project), memory_manager=second)
    await agent.run_to_completion("continue", ConversationManager())
    sent = repr(client.requests[0][0])
    assert "use four spaces" in sent
    assert "tests use pytest" in sent


@pytest.mark.asyncio
@pytest.mark.parametrize("control", ["Agent", "TeamCreate", "TeamDelete", "EnterWorktree", "ExitWorktree", "AskUserQuestion", "TaskStop", "TaskOutput", "ExitPlanMode"])
async def test_fork_runtime_source_blocks_control_even_without_history_marker(tmp_path, control):
    agent = Agent(ScriptedClient([]), create_default_registry(), "anthropic", work_dir=str(tmp_path), query_source="fork")
    result = await agent._execute_tool_noninteractive(ToolCallComplete("call", control, {}))
    assert result.is_error
    assert "Fork workers" in result.output


@pytest.mark.asyncio
@pytest.mark.parametrize("interactive", [False, True])
async def test_frozen_fork_system_does_not_discard_pending_shared_hook_prompts(tmp_path, interactive):
    from likecc.hooks.engine import HookEngine
    from likecc.hooks.models import Action, Hook, HookContext

    hooks = HookEngine([Hook("new-rule", "audit", Action(type="prompt", message="retain this rule"))])
    await hooks.run_hooks("audit", HookContext(event_name="audit"))
    client = ScriptedClient([turn(TextDelta("done"))])
    agent = Agent(client, create_default_registry(), "anthropic", work_dir=str(tmp_path),
                  hook_engine=hooks, query_source="fork", system_prompt_override="parent-system")
    if interactive:
        conversation = ConversationManager()
        conversation.add_user_message("task")
        async for _ in agent.run(conversation):
            pass
    else:
        await agent.run_to_completion("task")
    assert client.requests[0][1] == "parent-system"
    assert hooks.get_prompt_messages() == ["retain this rule"]


@pytest.mark.asyncio
async def test_textual_input_help_and_cancellation(tmp_path, monkeypatch):
    import likecc.app as ui

    home = tmp_path / "home"
    project = tmp_path / "project"
    home.mkdir()
    project.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.chdir(project)
    started = asyncio.Event()
    cancelled = asyncio.Event()

    class UIClient(LLMClient):
        async def stream(self, conversation, system="", tools=None):
            if any(m.role == "user" and m.content == "wait" for m in conversation.history):
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()
            yield TextDelta("ui-response")
            yield StreamEnd("end_turn", input_tokens=2, output_tokens=2)

    monkeypatch.setattr(ui, "create_client", lambda _: UIClient())

    async def resolve(_):
        return 32000

    monkeypatch.setattr(ui, "resolve_context_window", resolve)
    provider = ProviderConfig("offline", "anthropic", "http://unused.invalid", "offline", api_key="audit-only", context_window=32000)
    app = ui.LikeCCApp([provider])
    async with app.run_test(size=(100, 35)) as pilot:
        await pilot.pause()
        box = app.query_one("#chat-input", ui.ChatInput)
        box.load_text("hello")
        await pilot.press("enter")
        await pilot.pause()
        assert any(m.role == "assistant" and m.content == "ui-response" for m in app.conversation.history)
        box.load_text("/help")
        await pilot.press("enter")
        await pilot.pause()
        assert not app._streaming
        box.load_text("wait")
        await pilot.press("enter")
        await asyncio.wait_for(started.wait(), 3)
        await pilot.press("escape")
        await asyncio.wait_for(cancelled.wait(), 3)
        await pilot.pause()
        assert not app._streaming
        assert not box.disabled
