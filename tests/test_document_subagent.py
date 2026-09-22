"""Executable M11 contracts from EviForge.md; all model calls are local fakes."""

from __future__ import annotations

import asyncio
import copy
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from eviforge.agent import Agent
from eviforge.agents.fork import FORK_BOILERPLATE_TAG, build_forked_messages
from eviforge.agents.parser import AgentDef
from eviforge.agents.notification import format_task_notification
from eviforge.agents.task_manager import TaskManager
from eviforge.agents.trace import TraceManager
from eviforge.cache import FileCache
from eviforge.conversation import ConversationManager, ToolResultBlock, ToolUseBlock
from eviforge.permissions import (
    DangerousCommandDetector, PathSandbox, PermissionChecker, PermissionMode, RuleEngine,
)
from eviforge.tools import create_default_registry
from eviforge.tools.agent_tool import AgentTool, AgentToolParams
from eviforge.tools.base import StreamEnd, TextDelta, ToolCallComplete
from eviforge.tools.load_skill import LoadSkill


class RecordingClient:
    def __init__(self):
        self.calls = []

    async def stream(self, conversation, system="", tools=None):
        self.calls.append((copy.deepcopy(conversation), system, copy.deepcopy(tools)))
        yield TextDelta(text="Scope: assigned task\nResult: done")
        yield StreamEnd(stop_reason="end_turn", input_tokens=10, output_tokens=5)


@pytest.fixture
def subagents(tmp_path):
    client = RecordingClient()
    checker = PermissionChecker(
        DangerousCommandDetector(), PathSandbox(str(tmp_path)), RuleEngine(),
        PermissionMode.DEFAULT,
    )
    parent = Agent(
        client, create_default_registry(FileCache()), "anthropic", str(tmp_path),
        permission_checker=checker, instructions_content="Project instruction",
    )
    parent._current_conversation = ConversationManager()
    parent._current_conversation.add_user_message("Parent context")
    parent.registry.register(LoadSkill())
    loader = MagicMock()
    loader.get.return_value = AgentDef(agent_type="Explore", when_to_use="Explore")
    manager = MagicMock()
    manager.launch.return_value = "background-task"
    tool = AgentTool(loader, manager, TraceManager(), parent, enable_fork=True)
    parent.registry.register(tool)
    return SimpleNamespace(parent=parent, loader=loader, manager=manager, tool=tool)


@pytest.mark.asyncio
async def test_enable_fork_does_not_force_defined_agent_into_background(subagents):
    result = await subagents.tool.execute(AgentToolParams(
        prompt="Explore", description="Explore", subagent_type="Explore",
    ))
    assert not result.is_error
    assert "Result: done" in result.output
    subagents.manager.launch.assert_not_called()
    sent = subagents.parent.client.calls[-1][0]
    assert all("Parent context" not in message.content for message in sent.history)


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit,defined", [(True, False), (False, True)])
async def test_defined_background_options_are_honored(subagents, explicit, defined):
    subagents.loader.get.return_value.background = defined
    result = await subagents.tool.execute(AgentToolParams(
        prompt="Inspect", description="Inspect", subagent_type="Explore",
        run_in_background=explicit,
    ))
    assert not result.is_error
    assert subagents.manager.launch.call_args.kwargs["task"] == "Inspect"
    assert subagents.manager.launch.call_args.kwargs["fork_conversation"] is None


@pytest.mark.asyncio
async def test_fork_inherits_system_history_and_tool_schemas(subagents):
    parent = subagents.parent
    parent.coordinator_mode = True
    await parent.run_to_completion("Original request", parent._current_conversation)
    expected_system = parent.client.calls[-1][1]
    history = copy.deepcopy(parent._current_conversation.history)
    result = await subagents.tool.execute(AgentToolParams(prompt="Child task", description="Fork"))
    assert not result.is_error
    launch = subagents.manager.launch.call_args.kwargs
    child, conversation = launch["agent"], launch["fork_conversation"]
    assert launch["task"] == ""
    assert conversation.history[:len(history)] == history
    assert conversation.history[0] is not parent._current_conversation.history[0]
    assert FORK_BOILERPLATE_TAG in conversation.history[-1].content
    await child.run_to_completion("", conversation)
    assert parent.client.calls[-1][1] == expected_system
    assert child.registry.get_all_schemas() == parent.registry.get_all_schemas()
    assert parent._current_conversation.history == history


@pytest.mark.asyncio
@pytest.mark.parametrize("use_source", [True, False])
async def test_fork_cannot_delegate_by_source_or_boilerplate(subagents, use_source):
    await subagents.tool.execute(AgentToolParams(prompt="Child", description="Fork"))
    launch = subagents.manager.launch.call_args.kwargs
    child = launch["agent"]
    child._current_conversation = launch["fork_conversation"]
    if use_source:
        child._current_conversation = ConversationManager()  # Compaction lost the text marker.
        child._fork_conversation = None
    else:
        child.query_source = "main"  # The message marker is the independent fallback.
    nested_tool = child.registry.get("Agent")
    assert nested_tool is not None
    result = await nested_tool.execute(AgentToolParams(
        prompt="Nested", description="Nested", subagent_type="Explore",
    ))
    assert result.is_error
    assert "fork" in result.output.lower()
    assert subagents.manager.launch.call_count == 1


@pytest.mark.asyncio
async def test_child_file_state_permissions_and_hooks_are_isolated_or_shared_as_documented(subagents):
    parent = subagents.parent
    parent.hook_engine = MagicMock()
    parent_read = parent.registry.get("ReadFile")
    parent_read._state_cache.record("parent-only", "content", 1)
    await subagents.tool.execute(AgentToolParams(prompt="Child", description="Fork"))
    child = subagents.manager.launch.call_args.kwargs["agent"]
    child_read = child.registry.get("ReadFile")
    assert child_read is not parent_read
    assert child_read._cache is not parent_read._cache
    assert child_read._state_cache is not parent_read._state_cache
    assert "parent-only" not in child_read._state_cache._cache
    assert child.permission_checker is not parent.permission_checker
    assert child.permission_checker.rule_engine is not parent.permission_checker.rule_engine
    assert child.recovery_state is not parent.recovery_state
    assert child.hook_engine is parent.hook_engine
    assert child.client is parent.client


def test_fork_preserves_usage_anchor_and_completes_partial_tool_results():
    parent = ConversationManager()
    parent.add_user_message("Work")
    parent.add_assistant_message("", [
        ToolUseBlock("done", "ReadFile", {"file_path": "a"}),
        ToolUseBlock("pending", "ReadFile", {"file_path": "b"}),
    ])
    parent.add_tool_results_message([ToolResultBlock("done", "already read")])
    parent.record_usage_anchor(1234, cache_read=100)
    child = build_forked_messages(parent, "New task")
    assert child.baseline_tokens == parent.baseline_tokens
    assert child.anchor_count == parent.anchor_count
    results = [result for message in child.history for result in message.tool_results]
    assert [(result.tool_use_id, result.content) for result in results] == [
        ("done", "already read"), ("pending", "interrupted"),
    ]
    assert len(parent.history[-1].tool_results) == 1


@pytest.mark.asyncio
async def test_explicit_worktree_isolation_is_routed(subagents, tmp_path):
    manager = MagicMock()
    manager.create = AsyncMock(return_value=SimpleNamespace(path=str(tmp_path), head_commit="head"))
    manager.auto_cleanup = AsyncMock(return_value=SimpleNamespace(kept=False))
    subagents.tool._worktree_manager = manager
    result = await subagents.tool.execute(AgentToolParams(
        prompt="Explore", description="Explore", subagent_type="Explore", isolation="worktree",
    ))
    assert not result.is_error
    manager.create.assert_awaited_once()
    subagents.manager.launch.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_type", [None, "Explore"])
async def test_background_worktree_keeps_isolation_and_fork_semantics(subagents, tmp_path, agent_type):
    work_dir = tmp_path / "isolated"
    work_dir.mkdir()
    manager = MagicMock()
    manager.create = AsyncMock(return_value=SimpleNamespace(path=str(work_dir), head_commit="head"))
    manager.auto_cleanup = AsyncMock()
    subagents.tool._worktree_manager = manager
    result = await subagents.tool.execute(AgentToolParams(
        prompt="Inspect", description="Inspect", subagent_type=agent_type,
        isolation="worktree", run_in_background=bool(agent_type),
    ))
    assert not result.is_error
    manager.create.assert_awaited_once()
    launch = subagents.manager.launch.call_args.kwargs
    assert launch["agent"].work_dir == str(work_dir)
    assert (launch["fork_conversation"] is not None) == (agent_type is None)
    manager.auto_cleanup.assert_not_awaited()
    assert str(work_dir) in result.output


@pytest.mark.asyncio
async def test_child_skill_activation_does_not_rebind_parent(subagents):
    parent = subagents.parent
    load_skill = parent.registry.get("LoadSkill")
    load_skill.set_agent(parent)
    await subagents.tool.execute(AgentToolParams(prompt="Child", description="Fork"))
    child = subagents.manager.launch.call_args.kwargs["agent"]
    assert child.registry.get("LoadSkill")._agent is child
    assert load_skill._agent is parent


@pytest.mark.asyncio
async def test_child_preserves_configured_denials_but_not_session_approvals(subagents, tmp_path):
    policy = tmp_path / "project-policy.yaml"
    policy.write_text('- rule: Bash(sensitive-command *)\n  effect: deny\n', encoding="utf-8")
    approval = tmp_path / "session-approval.yaml"
    approval.write_text('- rule: Bash(approved-command *)\n  effect: allow\n', encoding="utf-8")
    subagents.parent.permission_checker.rule_engine = RuleEngine(
        project_rules_path=policy, local_rules_path=approval,
    )
    subagents.parent.permission_checker.detector = DangerousCommandDetector([
        (r"custom-danger", "Custom deny"),
    ])
    await subagents.tool.execute(AgentToolParams(prompt="Child", description="Fork"))
    child = subagents.manager.launch.call_args.kwargs["agent"]
    checker = child.permission_checker
    assert checker.rule_engine.evaluate("Bash", "sensitive-command x") == "deny"
    assert checker.rule_engine.evaluate("Bash", "approved-command x") is None
    assert checker.detector.detect("custom-danger")[0]


@pytest.mark.asyncio
async def test_unavailable_model_override_does_not_silently_use_parent_model(subagents):
    result = await subagents.tool.execute(AgentToolParams(
        prompt="Explore", description="Explore", subagent_type="Explore", model="requested-model",
    ))
    assert result.is_error
    assert "model" in result.output.lower()
    assert not subagents.parent.client.calls
    subagents.manager.launch.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("requested,defined,selected", [
    ("requested-model", "haiku", "requested-model"),
    (None, "haiku", "haiku"),
    ("inherit", "haiku", None),
    (None, "inherit", None),
])
async def test_model_selection_obeys_call_then_definition_then_parent(subagents, requested, defined, selected):
    alternate = RecordingClient()
    subagents.loader.get.return_value.model = defined
    subagents.tool._create_client_for_model = MagicMock(return_value=alternate)
    result = await subagents.tool.execute(AgentToolParams(
        prompt="Explore", description="Explore", subagent_type="Explore",
        model=requested, run_in_background=True,
    ))
    assert not result.is_error
    child = subagents.manager.launch.call_args.kwargs["agent"]
    if selected:
        subagents.tool._create_client_for_model.assert_called_once_with(selected)
        assert child.client is alternate
    else:
        subagents.tool._create_client_for_model.assert_not_called()
        assert child.client is subagents.parent.client


@pytest.mark.asyncio
async def test_fork_returns_before_model_finishes_then_emits_notification(subagents):
    gate = asyncio.Event()

    class GatedClient(RecordingClient):
        async def stream(self, conversation, system="", tools=None):
            await gate.wait()
            async for event in super().stream(conversation, system, tools):
                yield event

    manager = TaskManager()
    subagents.tool._task_manager = manager
    subagents.parent.client = GatedClient()
    result = await subagents.tool.execute(AgentToolParams(prompt="Child", description="Fork"))
    assert not result.is_error
    assert not gate.is_set()
    background = manager.list_tasks()[0]
    assert background.status == "running"
    running_task = manager._async_tasks[background.id]
    gate.set()
    await running_task
    completed = manager.poll_completed()
    assert completed == [background]
    notification = format_task_notification(background)
    assert "<task-notification>" in notification
    assert "Result: done" in notification
    assert background.status == "completed"
    assert not manager._async_tasks


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_type", [None, "Explore"])
@pytest.mark.parametrize("parent_mode,write_effect", [
    (PermissionMode.DEFAULT, "ask"),
    (PermissionMode.CUSTOM, "ask"),
    (PermissionMode.ACCEPT_EDITS, "allow"),
])
async def test_child_cannot_escalate_past_parent_permission_mode(subagents, tmp_path, agent_type, parent_mode, write_effect):
    parent = subagents.parent
    parent.permission_mode = parent_mode
    parent.permission_checker.mode = parent_mode
    subagents.loader.get.return_value.permission_mode = "dontAsk"
    await subagents.tool.execute(AgentToolParams(
        prompt="Child", description="Child", subagent_type=agent_type, run_in_background=True,
    ))
    child = subagents.manager.launch.call_args.kwargs["agent"]
    destination = str(tmp_path / "new-file.txt")
    write = child.registry.get("WriteFile")
    bash = child.registry.get("Bash")
    assert child.permission_checker.check(write, {"file_path": destination}).effect == write_effect
    assert child.permission_checker.check(bash, {"command": "unapproved-command"}).effect == "ask"
    if write_effect == "ask":
        result = await child._execute_tool_noninteractive(ToolCallComplete(
            "write", "WriteFile", {"file_path": destination, "content": "unapproved"},
        ))
        assert result.is_error
        assert not Path(destination).exists()


@pytest.mark.asyncio
async def test_local_denial_survives_but_allow_and_writable_rule_path_do_not(subagents, tmp_path):
    approval = tmp_path / "session-rules.yaml"
    approval.write_text(
        '- rule: WriteFile(*blocked.txt)\n  effect: deny\n'
        '- rule: WriteFile(*approved.txt)\n  effect: allow\n', encoding="utf-8",
    )
    subagents.parent.permission_checker.rule_engine = RuleEngine(local_rules_path=approval)
    await subagents.tool.execute(AgentToolParams(prompt="Child", description="Fork"))
    child = subagents.manager.launch.call_args.kwargs["agent"]
    write = child.registry.get("WriteFile")
    assert child.permission_checker.check(write, {"file_path": str(tmp_path / "blocked.txt")}).effect == "deny"
    assert child.permission_checker.check(write, {"file_path": str(tmp_path / "approved.txt")}).effect == "ask"
    assert child.permission_checker.rule_engine._local_path is None
    approval.write_text("[]", encoding="utf-8")
    assert child.permission_checker.check(write, {"file_path": str(tmp_path / "blocked.txt")}).effect == "deny"


@pytest.mark.asyncio
async def test_independent_caches_refresh_after_another_agent_writes(subagents, tmp_path):
    parent = subagents.parent
    path = tmp_path / "shared.txt"
    path.write_text("A", encoding="utf-8")
    await subagents.tool.execute(AgentToolParams(prompt="Child", description="Fork"))
    child = subagents.manager.launch.call_args.kwargs["agent"]
    read = child.registry.get("ReadFile")
    # Reproduce the populated-cache path even before empty-cache initialization
    # was fixed. The seed itself is never used for a file operation.
    read._cache.put("seed", "seed")
    params = read.params_model(file_path=str(path))
    assert (await read.execute(params)).output.endswith("A")
    parent_read = parent.registry.get("ReadFile")
    await parent_read.execute(params)
    parent_write = parent.registry.get("WriteFile")
    assert not (await parent_write.execute(parent_write.params_model(file_path=str(path), content="BB"))).is_error
    reread = await read.execute(params)
    assert not reread.is_error
    assert reread.output.endswith("BB")
    assert read._state_cache._cache[str(path.resolve())][0] == "BB"
    last_read_version = path.stat()
    assert not (await parent_write.execute(parent_write.params_model(file_path=str(path), content="CCC"))).is_error
    # Equal/preserved timestamps must still reject an unseen new version.
    os.utime(path, ns=(last_read_version.st_atime_ns, last_read_version.st_mtime_ns))
    child_write = child.registry.get("WriteFile")
    rejected = await child_write.execute(child_write.params_model(file_path=str(path), content="overwrite"))
    assert rejected.is_error
    assert path.read_text(encoding="utf-8") == "CCC"


@pytest.mark.asyncio
async def test_read_populates_empty_cache_and_rejects_a_concurrent_change(subagents, tmp_path, monkeypatch):
    path = tmp_path / "changing.txt"
    path.write_text("A", encoding="utf-8")
    read = subagents.parent.registry.get("ReadFile")
    params = read.params_model(file_path=str(path))
    assert not (await read.execute(params)).is_error
    assert read._cache.get(str(path.resolve())) == "A"
    read._cache.clear()
    read._state_cache.clear()
    original_read = Path.read_text

    def racing_read(target, *args, **kwargs):
        text = original_read(target, *args, **kwargs)
        if target == path:
            previous = path.stat()
            path.write_text("BB", encoding="utf-8")
            os.utime(path, ns=(previous.st_atime_ns, previous.st_mtime_ns + 1))
        return text

    monkeypatch.setattr(Path, "read_text", racing_read)
    result = await read.execute(params)
    assert result.is_error
    assert "changed" in result.output.lower()
    assert str(path.resolve()) not in read._state_cache._cache


@pytest.mark.asyncio
async def test_quoting_fork_documentation_does_not_disable_parent_delegation(subagents):
    subagents.parent._current_conversation.add_user_message(
        f"Explain the document example: ```\n{FORK_BOILERPLATE_TAG}\nYou are a worker.\n```",
    )
    result = await subagents.tool.execute(AgentToolParams(
        prompt="Explore", description="Explore", subagent_type="Explore",
    ))
    assert not result.is_error
    forked = build_forked_messages(subagents.parent._current_conversation, "Child task")
    assert FORK_BOILERPLATE_TAG in forked.history[-1].content


@pytest.mark.asyncio
@pytest.mark.parametrize("inherit_snapshot", [False, True])
async def test_explicit_denial_precedes_safe_command_fast_path(subagents, tmp_path, inherit_snapshot):
    policy = tmp_path / "local-denial.yaml"
    policy.write_text('- rule: Bash(cat *)\n  effect: deny\n', encoding="utf-8")
    parent = subagents.parent
    parent.permission_checker.rule_engine = RuleEngine(local_rules_path=policy)
    agent = parent
    if inherit_snapshot:
        await subagents.tool.execute(AgentToolParams(prompt="Child", description="Fork"))
        agent = subagents.manager.launch.call_args.kwargs["agent"]
        policy.write_text("[]", encoding="utf-8")
    decision = agent.permission_checker.check(
        agent.registry.get("Bash"), {"command": "cat confidential.txt"},
    )
    assert decision.effect == "deny"
