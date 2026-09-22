"""Regression checks for the dynamic definitions and isolation described in M11."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest
from pydantic import BaseModel

from eviforge.agents.loader import AgentLoader
from eviforge.agents.parser import AgentDef, AgentParseError, parse_agent_file
from eviforge.agents.tool_filter import resolve_agent_tools
from eviforge.cache import FileCache
from eviforge.tools import ToolRegistry, create_default_registry
from eviforge.tools.base import Tool, ToolResult
from eviforge.tools.impl.tool_search import ToolSearchParams, ToolSearchTool
from eviforge.tools.load_skill import LoadSkill
from eviforge.worktree.changes import count_worktree_changes
from eviforge.worktree.cleanup import cleanup_stale_worktrees
from eviforge.worktree.manager import WorktreeError, WorktreeManager
from eviforge.worktree.models import Worktree


def write_definition(directory: Path, name: str, filename: str = "role.md") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / filename
    path.write_text(f"---\nname: {name}\ndescription: role\n---\nAct as {name}.", encoding="utf-8")
    return path


@pytest.fixture
def loader(tmp_path, monkeypatch):
    monkeypatch.setattr("eviforge.agents.loader.USER_AGENTS_DIR", str(tmp_path / "user"))
    result = AgentLoader(str(tmp_path))
    result.load_all()
    return result


def test_definition_created_after_start_is_available_on_next_call(loader, tmp_path):
    write_definition(tmp_path / ".eviforge" / "agents", "late")
    assert loader.get("late").system_prompt == "Act as late."
    assert ("late", "role") in loader.list_agents()


def test_deleted_definition_is_no_longer_callable(loader, tmp_path):
    path = write_definition(tmp_path / ".eviforge" / "agents", "deleted")
    loader.load_all()
    path.unlink()
    assert loader.get("deleted") is None


def test_renamed_definition_does_not_keep_old_identity(loader, tmp_path):
    directory = tmp_path / ".eviforge" / "agents"
    write_definition(directory, "before")
    loader.load_all()
    write_definition(directory, "after")
    assert loader.get("before") is None
    assert loader.get("after").agent_type == "after"


def test_project_override_added_after_start_takes_priority(loader, tmp_path):
    write_definition(tmp_path / ".eviforge" / "agents", "Explore")
    assert loader.get("Explore").source == "project"


@pytest.mark.parametrize("field", [
    "name: []", "description: null", "tools: ReadFile", "tools: [ReadFile, 42]",
    "disallowedTools: Bash", "background: 'false'", "maxTurns: true",
])
def test_malformed_definition_is_rejected_before_loading(tmp_path, field):
    key = field.split(":", 1)[0]
    fields = {"name": "name: broken", "description": "description: broken", key: field}
    path = tmp_path / "broken.md"
    path.write_text("---\n" + "\n".join(fields.values()) + "\n---\nbody", encoding="utf-8")
    with pytest.raises(AgentParseError):
        parse_agent_file(path)


def test_bad_definition_does_not_stop_loading_valid_siblings(loader, tmp_path):
    directory = tmp_path / ".eviforge" / "agents"
    write_definition(directory, "valid")
    (directory / "bad.md").write_text("---\nname: []\ndescription: bad\n---\nbody", encoding="utf-8")
    assert loader.load_all()["valid"].agent_type == "valid"


class EmptyParams(BaseModel):
    pass


class DeferredTool(Tool):
    params_model = EmptyParams
    should_defer = True
    description = "An external tool"

    def __init__(self, name: str):
        self.name = name

    async def execute(self, params):
        return ToolResult("ok")


def test_mcp_tools_follow_explicit_definition_allowlist():
    parent = create_default_registry()
    parent.register(DeferredTool("mcp__service__write"))
    child = resolve_agent_tools(parent, AgentDef("reader", "reader", tools=["ReadFile"]))
    assert {tool.name for tool in child.list_tools()} == {"ReadFile"}


def test_mcp_tools_follow_explicit_definition_denylist():
    parent = ToolRegistry()
    parent.register(DeferredTool("mcp__service__write"))
    child = resolve_agent_tools(parent, AgentDef("reader", "reader", disallowed_tools=["mcp__service__write"]))
    assert child.get("mcp__service__write") is None


def test_unrestricted_background_agent_retains_mcp_tools():
    parent = ToolRegistry()
    parent.register(DeferredTool("mcp__service__read"))
    child = resolve_agent_tools(parent, AgentDef("worker", "worker"), is_background=True)
    assert child.get("mcp__service__read") is not None


def test_child_does_not_reenable_parent_disabled_tools():
    parent = create_default_registry()
    parent.disable("Bash")
    child = resolve_agent_tools(parent, AgentDef("worker", "worker"))
    assert not child.is_enabled("Bash")


def test_child_must_read_file_even_if_parent_has_already_read_it(tmp_path):
    path = tmp_path / "module.py"
    path.write_text("original", encoding="utf-8")
    parent = create_default_registry()
    read = parent.get("ReadFile")
    asyncio.run(read.execute(read.params_model(file_path=str(path))))
    child = resolve_agent_tools(parent, AgentDef("worker", "worker"))
    edit = child.get("EditFile")
    result = asyncio.run(edit.execute(edit.params_model(file_path=str(path), old_string="original", new_string="changed")))
    assert result.is_error
    assert "not been read" in result.output
    assert path.read_text(encoding="utf-8") == "original"
    child_read = child.get("ReadFile")
    asyncio.run(child_read.execute(child_read.params_model(file_path=str(path))))
    result = asyncio.run(edit.execute(edit.params_model(file_path=str(path), old_string="original", new_string="changed")))
    assert not result.is_error


def test_child_read_does_not_reuse_stale_parent_content_cache(tmp_path):
    path = tmp_path / "module.py"
    path.write_text("fresh", encoding="utf-8")
    cache = FileCache()
    cache.put(str(path.resolve()), "stale parent version")
    parent = create_default_registry(file_cache=cache)
    child = resolve_agent_tools(parent, AgentDef("worker", "worker"))
    read = child.get("ReadFile")
    result = asyncio.run(read.execute(read.params_model(file_path=str(path))))
    assert "fresh" in result.output
    assert "stale" not in result.output
    child.clear_file_caches()
    assert cache.get(str(path.resolve())) == "stale parent version"


def test_tool_search_only_discovers_tools_in_child_and_leaves_parent_unchanged():
    parent = ToolRegistry()
    parent.register(DeferredTool("mcp__service__allowed"))
    parent.register(DeferredTool("mcp__service__denied"))
    parent.register(ToolSearchTool(parent))
    child = resolve_agent_tools(parent, AgentDef("worker", "worker", disallowed_tools=["mcp__service__denied"]))
    search = child.get("ToolSearch")
    result = asyncio.run(search.execute(ToolSearchParams(query="select:mcp__service__denied")))
    assert "Found 1" not in result.output
    asyncio.run(search.execute(ToolSearchParams(query="select:mcp__service__allowed")))
    assert child.is_discovered("mcp__service__allowed")
    assert not parent.is_discovered("mcp__service__allowed")


def test_tool_search_cannot_select_disabled_tool():
    registry = ToolRegistry()
    registry.register(DeferredTool("mcp__service__disabled"))
    registry.disable("mcp__service__disabled")
    result = asyncio.run(ToolSearchTool(registry).execute(ToolSearchParams(query="select:mcp__service__disabled")))
    assert "Found 1" not in result.output
    assert not registry.is_discovered("mcp__service__disabled")


def test_load_skill_is_not_bound_to_parent_agent_in_child():
    parent = ToolRegistry()
    skill = LoadSkill()
    skill.set_agent(object())
    parent.register(skill)
    child = resolve_agent_tools(parent, AgentDef("worker", "worker"))
    assert child.get("LoadSkill") is not skill
    assert child.get("LoadSkill")._agent is None


def test_fork_registry_retains_schema_order_and_isolates_discovery():
    from eviforge.agents.tool_filter import clone_agent_registry

    parent = create_default_registry()
    parent.register(DeferredTool("mcp__service__first"))
    parent.register(ToolSearchTool(parent, protocol="openai"))
    parent.register(DeferredTool("mcp__service__second"))
    parent.mark_discovered("mcp__service__first")
    parent.disable("Bash")
    child = clone_agent_registry(parent, preserve_discovery=True)
    assert child.get_all_schemas("openai") == parent.get_all_schemas("openai")
    assert child.get("ToolSearch")._protocol == "openai"
    asyncio.run(child.get("ToolSearch").execute(ToolSearchParams(query="select:mcp__service__second")))
    assert child.is_discovered("mcp__service__second")
    assert not parent.is_discovered("mcp__service__second")


@pytest.mark.parametrize("failed_command", ["status", "rev-list"])
def test_failed_git_inspection_is_not_treated_as_a_clean_worktree(monkeypatch, failed_command):
    def fake_git(args, cwd):
        return subprocess.CompletedProcess(args, 128 if args[0] == failed_command else 0, "" if args[0] == "status" else "0", "failure")
    monkeypatch.setattr("eviforge.worktree.changes._run_git", fake_git)
    changes = count_worktree_changes("unused", "original")
    assert changes.uncommitted > 0 or changes.new_commits > 0


def test_failed_worktree_removal_preserves_active_record_and_branch(tmp_path, monkeypatch):
    manager = WorktreeManager(str(tmp_path))
    worktree = Worktree("worker", str(tmp_path / "worker"), "worktree-worker", "HEAD", "original")
    manager.active["worker"] = worktree
    monkeypatch.setattr("eviforge.worktree.manager.has_worktree_changes", lambda *args: False)
    calls = []
    def fake_git(args, cwd=None):
        calls.append(args)
        return subprocess.CompletedProcess(args, 128, "", "cannot remove")
    monkeypatch.setattr(manager, "_run_git", fake_git)
    result = asyncio.run(manager.auto_cleanup("worker", "original"))
    assert result.kept
    assert manager.active["worker"] is worktree
    assert not any(args[0] == "branch" for args in calls)


def test_worktree_creation_does_not_reset_an_existing_branch(tmp_path):
    def git(*args):
        return subprocess.run(["git", *args], cwd=tmp_path, capture_output=True, text=True, check=True).stdout.strip()
    git("init")
    git("config", "user.name", "Audit")
    git("config", "user.email", "audit@example.invalid")
    (tmp_path / "module.py").write_text("base", encoding="utf-8")
    git("add", ".")
    git("commit", "-m", "base")
    base = git("rev-parse", "HEAD")
    git("checkout", "-b", "worktree-worker")
    (tmp_path / "module.py").write_text("unmerged work", encoding="utf-8")
    git("commit", "-am", "keep this change")
    original = git("rev-parse", "HEAD")
    git("checkout", "--detach", base)
    manager = WorktreeManager(str(tmp_path))
    with pytest.raises(WorktreeError):
        asyncio.run(manager.create("worker"))
    assert git("rev-parse", "worktree-worker") == original
    assert not manager.active


def test_stale_cleanup_does_not_report_failed_removal_as_success(tmp_path, monkeypatch):
    manager = WorktreeManager(str(tmp_path))
    path = Path(manager.worktree_dir) / "agent-a1234567"
    path.mkdir(parents=True)
    monkeypatch.setattr(WorktreeManager, "read_worktree_head_sha", staticmethod(lambda _: "head"))
    monkeypatch.setattr("eviforge.worktree.cleanup.has_worktree_changes", lambda *args: False)
    monkeypatch.setattr("eviforge.worktree.cleanup.has_unpushed_commits", lambda *args: False)
    monkeypatch.setattr(manager, "_run_git", lambda args: subprocess.CompletedProcess(args, 128, "", "cannot remove"))
    assert asyncio.run(cleanup_stale_worktrees(manager, cutoff_hours=-1)) == 0
    assert path.exists()
