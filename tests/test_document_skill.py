"""Run forked skills through real Agents with an offline scripted LLM."""

from __future__ import annotations

import copy

import pytest

from likecc.agent import Agent
from likecc.cache import FileCache
from likecc.client import LLMClient
from likecc.conversation import ConversationManager
from likecc.hooks.engine import HookEngine
from likecc.hooks.models import Action, Hook
from likecc.permissions import DangerousCommandDetector, PathSandbox, PermissionChecker, PermissionMode, RuleEngine
from likecc.skills.executor import SkillExecutor
from likecc.skills.parser import SkillDef
from likecc.tools import create_default_registry
from likecc.tools.base import StreamEnd, TextDelta, ToolCallComplete
from likecc.tools.impl.tool_search import ToolSearchTool
from likecc.tools.read_file import ReadFile


class FakeLLM(LLMClient):
    def __init__(self, responses=None):
        self.responses = responses or [[TextDelta("finished"), StreamEnd("end_turn")]]
        self.histories = []
        self.schemas = []

    async def stream(self, conversation, system="", tools=None):
        index = len(self.histories)
        self.histories.append(copy.deepcopy(conversation.history))
        self.schemas.append(copy.deepcopy(tools))
        for event in self.responses[index]:
            yield event


def parent_agent(tmp_path, registry=None, hooks=None):
    checker = PermissionChecker(DangerousCommandDetector(), PathSandbox(str(tmp_path)), RuleEngine(), PermissionMode.ACCEPT_EDITS)
    return Agent(FakeLLM(), registry or create_default_registry(), "anthropic", work_dir=str(tmp_path), permission_checker=checker, hook_engine=hooks)


def skill(context="none", allowed=None):
    return SkillDef("audit-skill", "Run an isolated skill", "Inspect $ARGUMENTS", allowed_tools=allowed or [], mode="fork", context=context)


def tool_response(name, arguments):
    return [[ToolCallComplete("call_1", name, arguments), StreamEnd("tool_use")],
            [TextDelta("finished"), StreamEnd("end_turn")]]


def tool_results(client):
    return [result for message in client.histories[-1] for result in message.tool_results]


@pytest.mark.asyncio
@pytest.mark.parametrize("context", ["recent", "full", "none"])
async def test_skill_context_comes_from_the_production_parent_conversation(tmp_path, context):
    parent = parent_agent(tmp_path)
    conversation = ConversationManager()
    for index in range(4):
        conversation.add_user_message(f"user-context-{index}")
        conversation.add_assistant_message(f"assistant-context-{index}")
    # Set the active conversation using the production runner, not a test-only field.
    await parent.run_to_completion("", conversation=conversation)
    assert parent._current_conversation is conversation
    assert not hasattr(parent, "_conversation")
    original_history = copy.deepcopy(conversation.history)
    client = FakeLLM()
    assert await SkillExecutor(parent, client, "anthropic").execute_fork(skill(context), "target") == "finished"
    sent = "\n".join(message.content for message in client.histories[0])
    assert "Inspect target" in sent
    if context == "recent":
        assert "assistant-context-3" in sent and "user-context-2" in sent
        assert "user-context-0" not in sent
    elif context == "full":
        assert "Previous conversation summary" in sent
        assert "user-context-0" in sent and "assistant-context-3" in sent
    else:
        assert "user-context-" not in sent and "assistant-context-" not in sent
    assert conversation.history == original_history


@pytest.mark.asyncio
async def test_skill_child_cannot_edit_based_only_on_parent_read_state(tmp_path):
    path = tmp_path / "module.py"
    path.write_text("original", encoding="utf-8")
    parent = parent_agent(tmp_path)
    await parent._execute_tool_noninteractive(ToolCallComplete("parent_read", "ReadFile", {"file_path": "module.py"}))
    client = FakeLLM(tool_response("EditFile", {"file_path": "module.py", "old_string": "original", "new_string": "changed"}))
    await SkillExecutor(parent, client, "anthropic").execute_fork(skill(allowed=["ReadFile", "EditFile"]), "module.py")
    results = tool_results(client)
    assert results[0].is_error and "not been read" in results[0].content
    assert path.read_text(encoding="utf-8") == "original"


@pytest.mark.asyncio
async def test_skill_child_reads_current_disk_content_instead_of_parent_cache(tmp_path):
    path = tmp_path / "module.py"
    path.write_text("current content", encoding="utf-8")
    cache = FileCache()
    cache.put(str(path.resolve()), "stale parent cache")
    parent = parent_agent(tmp_path, create_default_registry(file_cache=cache))
    client = FakeLLM(tool_response("ReadFile", {"file_path": "module.py"}))
    await SkillExecutor(parent, client, "anthropic").execute_fork(skill(allowed=["ReadFile"]), "module.py")
    results = tool_results(client)
    assert not results[0].is_error and "current content" in results[0].content
    assert "stale parent cache" not in results[0].content
    assert cache.get(str(path.resolve())) == "stale parent cache"


@pytest.mark.asyncio
async def test_parent_hook_actually_blocks_a_skill_child_write(tmp_path):
    hook = Hook("block-child-write", "pre_tool_use", Action("prompt", message="blocked by shared hook"), reject=True)
    engine = HookEngine([hook])
    parent = parent_agent(tmp_path, hooks=engine)
    client = FakeLLM(tool_response("WriteFile", {"file_path": "blocked.txt", "content": "should not exist"}))
    await SkillExecutor(parent, client, "anthropic").execute_fork(skill(allowed=["WriteFile"]), "blocked.txt")
    assert hook.executed
    assert parent.hook_engine is engine
    assert not (tmp_path / "blocked.txt").exists()
    assert tool_results(client)[0].is_error
    assert "blocked by shared hook" in tool_results(client)[0].content


@pytest.mark.asyncio
async def test_skill_filter_preserves_disabled_discovered_tools_and_schema_order(tmp_path):
    registry = create_default_registry()
    registry.register(ToolSearchTool(registry, protocol="anthropic"))
    deferred = ReadFile()
    deferred.name = "DeferredRead"
    deferred.should_defer = True
    registry.register(deferred)
    registry.mark_discovered("DeferredRead")
    registry.disable("Bash")
    parent = parent_agent(tmp_path, registry)
    client = FakeLLM()
    await SkillExecutor(parent, client, "anthropic").execute_fork(skill(allowed=["ReadFile", "ToolSearch", "DeferredRead", "Bash"]), "target")
    assert [schema["name"] for schema in client.schemas[0]] == ["ReadFile", "DeferredRead", "ToolSearch"]
    assert not registry.is_enabled("Bash")
    assert registry.is_discovered("DeferredRead")
