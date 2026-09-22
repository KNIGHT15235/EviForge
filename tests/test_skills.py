
"""Skill 系统的测试 —— 包括 parser、loader、executor 以及 LoadSkill 工具。"""
from __future__ import annotations

import asyncio
import json
import textwrap
from pathlib import Path
from typing import Any, AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from eviforge.skills.parser import (
    SkillDef,
    SkillParseError,
    parse_frontmatter,
    parse_skill_file,
    substitute_arguments,
)
from eviforge.skills.loader import SkillLoader
from eviforge.skills.executor import (
    SkillDependencyError,
    SkillExecutor,
    filter_fork_tool_registry,
    filter_tool_registry,
)
from eviforge.client import LLMClient
from eviforge.conversation import ConversationManager
from eviforge.tools import ToolRegistry
from eviforge.tools.base import (
    StreamEnd,
    StreamEvent,
    TextDelta,
    Tool,
    ToolCallComplete,
    ToolResult,
)

# ---------------------------------------------------------------------------
# 辅助工具
# ---------------------------------------------------------------------------

VALID_SKILL_MD = textwrap.dedent("""\
    ---
    name: test-skill
    description: A test skill
    allowedTools:
      - Bash
      - ReadFile
    mode: inline
    ---

    # Task

    Do something.

    $ARGUMENTS
""")

FORK_SKILL_MD = textwrap.dedent("""\
    ---
    name: review
    description: Review code
    mode: fork
    context: none
    allowedTools:
      - ReadFile
    ---

    Review the code changes.

    $ARGUMENTS
""")

class FakeTool(Tool):

    def __init__(
        self,
        name: str,
        system: bool = False,
        category: str = "read",
    ) -> None:
        self.name = name
        self.description = f"Fake {name}"
        from pydantic import BaseModel

        class P(BaseModel):
            model_config = {"extra": "allow"}

        self.params_model = P
        self.category = category
        self.is_concurrency_safe = False
        self.is_system_tool = system
        self.executions = 0
        self.execution_work_dirs: list[str] = []

    async def execute(self, params) -> ToolResult:
        from eviforge.tools.work_dir import get_tool_work_dir

        self.executions += 1
        self.execution_work_dirs.append(str(get_tool_work_dir()))
        return ToolResult(output="ok")


class ScriptedClient(LLMClient):
    def __init__(
        self,
        responses: list[list[StreamEvent]],
        *,
        started: asyncio.Event | None = None,
        release: asyncio.Event | None = None,
    ) -> None:
        self._responses = responses
        self._index = 0
        self.started = started
        self.release = release

    async def stream(
        self,
        conversation: ConversationManager,
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        index = self._index
        self._index += 1
        if index == 0 and self.started is not None:
            self.started.set()
            if self.release is not None:
                await self.release.wait()
        for event in self._responses[index]:
            yield event


def _tool_then_finish(
    tool_name: str,
    arguments: dict[str, Any],
) -> list[list[StreamEvent]]:
    return [
        [
            ToolCallComplete("tool-1", tool_name, arguments),
            StreamEnd("end_turn", input_tokens=1, output_tokens=1),
        ],
        [
            TextDelta("finished"),
            StreamEnd("end_turn", input_tokens=1, output_tokens=1),
        ],
    ]

# ---------------------------------------------------------------------------
# Parser 测试
# ---------------------------------------------------------------------------

class TestParseFrontmatter:
    def test_valid(self) -> None:
        meta, body = parse_frontmatter(VALID_SKILL_MD)
        assert meta["name"] == "test-skill"
        assert meta["description"] == "A test skill"
        assert "Do something" in body

    def test_missing_opening(self) -> None:
        with pytest.raises(SkillParseError, match="Missing YAML frontmatter"):
            parse_frontmatter("no frontmatter here")

    def test_unclosed(self) -> None:
        with pytest.raises(SkillParseError, match="Unclosed YAML"):
            parse_frontmatter("---\nname: foo\n")

    def test_invalid_yaml(self) -> None:
        with pytest.raises(SkillParseError, match="Invalid YAML"):
            parse_frontmatter("---\n: :\n  bad: [yaml\n---\nbody")

    def test_non_dict_frontmatter(self) -> None:
        with pytest.raises(SkillParseError, match="must be a YAML mapping"):
            parse_frontmatter("---\n- list\n- item\n---\nbody")

class TestParseSkillFile:
    def test_valid_file(self, tmp_path: Path) -> None:
        f = tmp_path / "test.md"
        f.write_text(VALID_SKILL_MD)
        skill = parse_skill_file(f)
        assert skill.name == "test-skill"
        assert skill.description == "A test skill"
        assert skill.mode == "inline"
        assert skill.allowed_tools == ["Bash", "ReadFile"]
        assert "$ARGUMENTS" in skill.prompt_body

    def test_missing_name(self, tmp_path: Path) -> None:
        f = tmp_path / "bad.md"
        f.write_text("---\ndescription: oops\n---\nbody")
        with pytest.raises(SkillParseError, match="Missing required field 'name'"):
            parse_skill_file(f)

    def test_missing_description(self, tmp_path: Path) -> None:
        f = tmp_path / "bad.md"
        f.write_text("---\nname: foo\n---\nbody")
        with pytest.raises(SkillParseError, match="Missing required field 'description'"):
            parse_skill_file(f)

    def test_invalid_name_format(self, tmp_path: Path) -> None:
        f = tmp_path / "bad.md"
        f.write_text("---\nname: UPPER\ndescription: x\n---\nbody")
        with pytest.raises(SkillParseError, match="Invalid skill name"):
            parse_skill_file(f)

    def test_invalid_mode(self, tmp_path: Path) -> None:
        f = tmp_path / "bad.md"
        f.write_text("---\nname: foo\ndescription: x\nmode: bad\n---\nbody")
        with pytest.raises(SkillParseError, match="Invalid mode"):
            parse_skill_file(f)

    def test_nonexistent_file(self, tmp_path: Path) -> None:
        with pytest.raises(SkillParseError, match="Cannot read"):
            parse_skill_file(tmp_path / "nope.md")

    def test_fork_mode_with_context(self, tmp_path: Path) -> None:
        f = tmp_path / "fork.md"
        f.write_text(FORK_SKILL_MD)
        skill = parse_skill_file(f)
        assert skill.mode == "fork"
        assert skill.context == "none"

class TestSubstituteArguments:
    def test_with_args(self) -> None:
        result = substitute_arguments("Do $ARGUMENTS now", "something cool")
        assert result == "Do something cool now"

    def test_without_args(self) -> None:
        result = substitute_arguments("Do $ARGUMENTS now", "")
        assert result == "Do  now"

    def test_no_placeholder(self) -> None:
        result = substitute_arguments("No placeholder here", "args")
        assert result == "No placeholder here"

    def test_multiple_placeholders(self) -> None:
        result = substitute_arguments("$ARGUMENTS and $ARGUMENTS", "x")
        assert result == "x and x"

# ---------------------------------------------------------------------------
# Loader 测试
# ---------------------------------------------------------------------------

class TestSkillLoader:
    def test_load_builtins(self) -> None:
        loader = SkillLoader("/nonexistent")
        skills = loader.load_all()
        assert "commit" in skills
        assert "review" in skills
        assert "test" in skills
        assert skills["commit"].mode == "inline"
        assert skills["review"].mode == "fork"

    def test_project_overrides_builtin(self, tmp_path: Path) -> None:
        skills_dir = tmp_path / ".eviforge" / "skills"
        skills_dir.mkdir(parents=True)
        custom = skills_dir / "commit.md"
        custom.write_text(textwrap.dedent("""\
            ---
            name: commit
            description: Custom commit
            mode: inline
            ---
            Custom prompt
        """))
        loader = SkillLoader(str(tmp_path))
        skills = loader.load_all()
        assert skills["commit"].description == "Custom commit"
        assert "Custom prompt" in skills["commit"].prompt_body

    def test_catalog(self) -> None:
        loader = SkillLoader("/nonexistent")
        loader.load_all()
        catalog = loader.get_catalog()
        names = [n for n, _ in catalog]
        assert "commit" in names
        assert "review" in names
        assert "test" in names

    def test_get_returns_skill(self) -> None:
        loader = SkillLoader("/nonexistent")
        loader.load_all()
        skill = loader.get("commit")
        assert skill is not None
        assert skill.name == "commit"

    def test_get_unknown(self) -> None:
        loader = SkillLoader("/nonexistent")
        loader.load_all()
        assert loader.get("nonexistent") is None

    def test_hot_reload(self, tmp_path: Path) -> None:
        skills_dir = tmp_path / ".eviforge" / "skills"
        skills_dir.mkdir(parents=True)
        f = skills_dir / "custom.md"
        f.write_text(textwrap.dedent("""\
            ---
            name: custom
            description: v1
            ---
            Prompt v1
        """))
        loader = SkillLoader(str(tmp_path))
        loader.load_all()
        assert loader.get("custom").description == "v1"

        f.write_text(textwrap.dedent("""\
            ---
            name: custom
            description: v2
            ---
            Prompt v2
        """))
        skill = loader.get("custom")
        assert skill.description == "v2"
        assert "v2" in skill.prompt_body

    def test_hot_reload_fallback_on_error(self, tmp_path: Path) -> None:
        skills_dir = tmp_path / ".eviforge" / "skills"
        skills_dir.mkdir(parents=True)
        f = skills_dir / "custom.md"
        f.write_text(textwrap.dedent("""\
            ---
            name: custom
            description: good
            ---
            Good prompt
        """))
        loader = SkillLoader(str(tmp_path))
        loader.load_all()

        f.write_text("broken content no frontmatter")
        skill = loader.get("custom")
        assert skill is not None
        assert skill.description == "good"

    def test_directory_skill_detected(self, tmp_path: Path) -> None:
        skills_dir = tmp_path / ".eviforge" / "skills"
        skill_dir = skills_dir / "my-skill"
        skill_dir.mkdir(parents=True)
        skill_md = skill_dir / "SKILL.md"
        skill_md.write_text(textwrap.dedent("""\
            ---
            name: my-skill
            description: A directory skill
            ---
            SOP here
        """))
        loader = SkillLoader(str(tmp_path))
        skills = loader.load_all()
        assert "my-skill" in skills
        assert skills["my-skill"].is_directory is True

    def test_source_label(self, tmp_path: Path) -> None:
        loader = SkillLoader(str(tmp_path))
        loader.load_all()
        assert loader.get_source_label("commit") == "builtin"
        assert loader.get_source_label("nonexistent") == "unknown"

    def test_malformed_file_skipped(self, tmp_path: Path) -> None:
        skills_dir = tmp_path / ".eviforge" / "skills"
        skills_dir.mkdir(parents=True)
        bad = skills_dir / "broken.md"
        bad.write_text("not valid frontmatter")
        good = skills_dir / "valid.md"
        good.write_text(textwrap.dedent("""\
            ---
            name: valid
            description: Works fine
            ---
            Prompt
        """))
        loader = SkillLoader(str(tmp_path))
        skills = loader.load_all()
        assert "valid" in skills
        assert "broken" not in skills

    def test_reload(self) -> None:
        loader = SkillLoader("/nonexistent")
        loader.load_all()
        skills = loader.reload()
        assert "commit" in skills

# ---------------------------------------------------------------------------
# Executor：filter_tool_registry
# ---------------------------------------------------------------------------

class TestFilterToolRegistry:
    def test_empty_allowed_returns_original(self) -> None:
        registry = ToolRegistry()
        registry.register(FakeTool("Bash"))
        result = filter_tool_registry(registry, [])
        assert result is registry

    def test_filters_to_allowed(self) -> None:
        registry = ToolRegistry()
        registry.register(FakeTool("Bash"))
        registry.register(FakeTool("ReadFile"))
        registry.register(FakeTool("WriteFile"))
        result = filter_tool_registry(registry, ["Bash", "ReadFile"])
        assert result.get("Bash") is not None
        assert result.get("ReadFile") is not None
        assert result.get("WriteFile") is None

    def test_system_tools_always_included(self) -> None:
        registry = ToolRegistry()
        registry.register(FakeTool("Bash"))
        registry.register(FakeTool("LoadSkill", system=True))
        result = filter_tool_registry(registry, ["Bash"])
        assert result.get("LoadSkill") is not None

    def test_missing_tool_raises(self) -> None:
        registry = ToolRegistry()
        registry.register(FakeTool("Bash"))
        with pytest.raises(SkillDependencyError, match="NoSuchTool"):
            filter_tool_registry(registry, ["NoSuchTool"])

    @pytest.mark.asyncio
    async def test_fork_filter_blocks_parent_control_and_rebinds_tool_search(
        self,
    ) -> None:
        from eviforge.tools.impl.tool_search import ToolSearchParams, ToolSearchTool

        registry = ToolRegistry()
        registry.register(FakeTool("ReadFile"))
        deferred_agent = FakeTool("Agent")
        deferred_agent.should_defer = True
        registry.register(deferred_agent)
        registry.register(FakeTool("EnterWorktree"))
        registry.register(FakeTool("ExitWorktree"))
        parent_search = ToolSearchTool(registry, protocol="anthropic")
        registry.register(parent_search)
        registry.disable("ReadFile")

        filtered = filter_fork_tool_registry(registry, [], "anthropic")
        names = {tool.name for tool in filtered.list_tools()}

        assert "ReadFile" in names
        assert "Agent" not in names
        assert "EnterWorktree" not in names
        assert "ExitWorktree" not in names
        assert not filtered.is_enabled("ReadFile")
        fork_search = filtered.get("ToolSearch")
        assert fork_search is not None
        assert fork_search is not parent_search
        assert fork_search._registry is filtered

        result = await fork_search.execute(ToolSearchParams(query="select:Agent"))
        assert "No matching deferred tools" in result.output
        assert not registry.is_discovered("Agent")


# ---------------------------------------------------------------------------
# Executor：fork 权限继承与非交互执行
# ---------------------------------------------------------------------------

class TestSkillForkPermissions:
    @staticmethod
    def _skill(tool_name: str) -> SkillDef:
        return SkillDef(
            name="secure-fork",
            description="Exercise a forked skill",
            prompt_body="Use the requested tool",
            allowed_tools=[tool_name],
            mode="fork",
            context="none",
        )

    @staticmethod
    def _checker(root: Path, mode, *, extra_patterns=None):
        from eviforge.permissions import (
            DangerousCommandDetector,
            PathSandbox,
            PermissionChecker,
            RuleEngine,
        )

        return PermissionChecker(
            detector=DangerousCommandDetector(extra_patterns=extra_patterns),
            sandbox=PathSandbox(str(root)),
            rule_engine=RuleEngine(),
            mode=mode,
        )

    @pytest.mark.asyncio
    async def test_parent_without_checker_gets_noninteractive_default_policy(
        self, tmp_path: Path
    ) -> None:
        """An unobserved `ask` is denied promptly instead of hanging forever."""
        from eviforge.agent import Agent

        tool = FakeTool("WriteFile", category="write")
        registry = ToolRegistry()
        registry.register(tool)
        client = ScriptedClient(
            _tool_then_finish(
                "WriteFile", {"file_path": "inside.txt", "content": "x"}
            )
        )
        parent = Agent(client, registry, "anthropic", work_dir=str(tmp_path))
        executor = SkillExecutor(parent, client, "anthropic")

        result = await asyncio.wait_for(
            executor.execute_fork(self._skill("WriteFile"), ""), timeout=1
        )

        assert result == "finished"
        assert tool.executions == 0

    @pytest.mark.asyncio
    async def test_fork_inherits_custom_dangerous_command_detector(
        self, tmp_path: Path
    ) -> None:
        from eviforge.agent import Agent
        from eviforge.permissions import PermissionMode

        tool = FakeTool("Bash", category="command")
        registry = ToolRegistry()
        registry.register(tool)
        client = ScriptedClient(
            _tool_then_finish("Bash", {"command": "blocked-command"})
        )
        checker = self._checker(
            tmp_path,
            PermissionMode.BYPASS,
            extra_patterns=[(r"blocked-command", "test dangerous command")],
        )
        parent = Agent(
            client,
            registry,
            "anthropic",
            work_dir=str(tmp_path),
            permission_checker=checker,
        )

        result = await SkillExecutor(parent, client, "anthropic").execute_fork(
            self._skill("Bash"), ""
        )

        assert result == "finished"
        assert tool.executions == 0

    @pytest.mark.asyncio
    async def test_fork_inherits_path_sandbox_even_in_bypass_mode(
        self, tmp_path: Path
    ) -> None:
        from eviforge.agent import Agent
        from eviforge.permissions import PermissionMode

        tool = FakeTool("WriteFile", category="write")
        registry = ToolRegistry()
        registry.register(tool)
        client = ScriptedClient(
            _tool_then_finish(
                "WriteFile",
                {"file_path": "/etc/eviforge-skill-outside.txt", "content": "x"},
            )
        )
        checker = self._checker(tmp_path, PermissionMode.BYPASS)
        parent = Agent(
            client,
            registry,
            "anthropic",
            work_dir=str(tmp_path),
            permission_checker=checker,
        )

        result = await SkillExecutor(parent, client, "anthropic").execute_fork(
            self._skill("WriteFile"), ""
        )

        assert result == "finished"
        assert tool.executions == 0

    @pytest.mark.asyncio
    async def test_fork_work_dir_and_policy_are_snapshotted_before_parent_rebase(
        self, tmp_path: Path
    ) -> None:
        from eviforge.agent import Agent
        from eviforge.permissions import PermissionMode

        old_root = tmp_path / "old"
        new_root = tmp_path / "new"
        old_root.mkdir()
        new_root.mkdir()
        started = asyncio.Event()
        release = asyncio.Event()
        tool = FakeTool("WriteFile", category="write")
        registry = ToolRegistry()
        registry.register(tool)
        client = ScriptedClient(
            _tool_then_finish(
                "WriteFile", {"file_path": "relative.txt", "content": "x"}
            ),
            started=started,
            release=release,
        )
        checker = self._checker(old_root, PermissionMode.ACCEPT_EDITS)
        parent = Agent(
            client,
            registry,
            "anthropic",
            work_dir=str(old_root),
            permission_checker=checker,
        )
        task = asyncio.create_task(
            SkillExecutor(parent, client, "anthropic").execute_fork(
                self._skill("WriteFile"), ""
            )
        )
        await asyncio.wait_for(started.wait(), timeout=1)

        parent.work_dir = str(new_root)
        parent.permission_checker.sandbox.set_project_root(str(new_root))
        parent.set_permission_mode(PermissionMode.PLAN)
        release.set()

        assert await asyncio.wait_for(task, timeout=1) == "finished"
        assert tool.executions == 1
        assert tool.execution_work_dirs == [str(old_root.resolve())]

    @pytest.mark.asyncio
    async def test_plan_mode_allow_rule_cannot_enable_fork_write(
        self, tmp_path: Path
    ) -> None:
        from eviforge.agent import Agent
        from eviforge.permissions import (
            DangerousCommandDetector,
            PathSandbox,
            PermissionChecker,
            PermissionMode,
            RuleEngine,
        )

        rules = tmp_path / "permissions.yaml"
        rules.write_text(
            "- rule: WriteFile(*)\n  effect: allow\n", encoding="utf-8"
        )
        tool = FakeTool("WriteFile", category="write")
        registry = ToolRegistry()
        registry.register(tool)
        client = ScriptedClient(
            _tool_then_finish(
                "WriteFile", {"file_path": "inside.txt", "content": "x"}
            )
        )
        checker = PermissionChecker(
            detector=DangerousCommandDetector(),
            sandbox=PathSandbox(str(tmp_path)),
            rule_engine=RuleEngine(project_rules_path=rules),
            mode=PermissionMode.PLAN,
        )
        parent = Agent(
            client,
            registry,
            "anthropic",
            work_dir=str(tmp_path),
            permission_checker=checker,
        )

        result = await SkillExecutor(parent, client, "anthropic").execute_fork(
            self._skill("WriteFile"), ""
        )

        assert result == "finished"
        assert tool.executions == 0

# ---------------------------------------------------------------------------
# 目录型 Skill：tool.json 解析
# ---------------------------------------------------------------------------

class TestDirectorySkill:
    def test_parse_tool_json(self, tmp_path: Path) -> None:
        from eviforge.skills.directory import parse_tool_json

        tool_json = tmp_path / "tool.json"
        tool_json.write_text(json.dumps([
            {
                "name": "my_tool",
                "description": "Does stuff",
                "parameters": {"type": "object", "properties": {}},
            }
        ]))
        schemas = parse_tool_json(tool_json)
        assert len(schemas) == 1
        assert schemas[0]["name"] == "my_tool"

    def test_parse_tool_json_single_object(self, tmp_path: Path) -> None:
        from eviforge.skills.directory import parse_tool_json

        tool_json = tmp_path / "tool.json"
        tool_json.write_text(json.dumps({
            "name": "single",
            "description": "One tool",
            "parameters": {},
        }))
        schemas = parse_tool_json(tool_json)
        assert len(schemas) == 1

    def test_register_skill_tools(self, tmp_path: Path) -> None:
        from eviforge.skills.directory import register_skill_tools

        skill_dir = tmp_path / "my-skill"
        skill_dir.mkdir()
        refs = skill_dir / "references"
        refs.mkdir()

        (skill_dir / "tool.json").write_text(json.dumps([{
            "name": "my_tool",
            "description": "A tool",
            "parameters": {"type": "object", "properties": {}},
        }]))

        (refs / "my_tool.py").write_text(
            "async def execute(**kwargs):\n    return 'hello'\n"
        )

        registry = ToolRegistry()
        count = register_skill_tools(skill_dir, registry)
        assert count == 1
        assert registry.get("my_tool") is not None

    def test_register_no_tool_json(self, tmp_path: Path) -> None:
        from eviforge.skills.directory import register_skill_tools

        registry = ToolRegistry()
        count = register_skill_tools(tmp_path, registry)
        assert count == 0

    @pytest.mark.asyncio
    async def test_custom_tool_execution(self, tmp_path: Path) -> None:
        from eviforge.skills.directory import register_skill_tools

        skill_dir = tmp_path / "skill"
        skill_dir.mkdir()
        refs = skill_dir / "references"
        refs.mkdir()

        (skill_dir / "tool.json").write_text(json.dumps([{
            "name": "greet",
            "description": "Greet someone",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
            },
        }]))
        (refs / "greet.py").write_text(
            "async def execute(**kwargs):\n"
            "    return f\"Hello {kwargs.get('name', 'world')}!\"\n"
        )

        registry = ToolRegistry()
        register_skill_tools(skill_dir, registry)
        tool = registry.get("greet")
        assert tool is not None

        from pydantic import BaseModel

        class P(BaseModel):
            model_config = {"extra": "allow"}

        params = P(name="Alice")
        result = await tool.execute(params)
        assert "Hello Alice!" in result.output

# ---------------------------------------------------------------------------
# LoadSkill 工具
# ---------------------------------------------------------------------------

class TestLoadSkillTool:
    @pytest.mark.asyncio
    async def test_load_existing_skill(self) -> None:
        from eviforge.tools.load_skill import LoadSkill, LoadSkillParams

        tool = LoadSkill()
        loader = MagicMock()
        agent = MagicMock()
        agent.registry = ToolRegistry()

        skill = SkillDef(
            name="commit",
            description="Commit",
            prompt_body="Do commit",
            is_directory=False,
        )
        loader.get.return_value = skill

        tool.set_loader(loader)
        tool.set_agent(agent)

        result = await tool.execute(LoadSkillParams(name="commit"))
        assert not result.is_error
        assert "activated" in result.output
        agent.activate_skill.assert_called_once_with("commit", "Do commit")

    @pytest.mark.asyncio
    async def test_load_unknown_skill(self) -> None:
        from eviforge.tools.load_skill import LoadSkill, LoadSkillParams

        tool = LoadSkill()
        loader = MagicMock()
        loader.get.return_value = None
        loader.get_catalog.return_value = [("commit", "x")]
        agent = MagicMock()

        tool.set_loader(loader)
        tool.set_agent(agent)

        result = await tool.execute(LoadSkillParams(name="nonexistent"))
        assert result.is_error
        assert "unknown skill" in result.output

    @pytest.mark.asyncio
    async def test_not_initialized(self) -> None:
        from eviforge.tools.load_skill import LoadSkill, LoadSkillParams

        tool = LoadSkill()
        result = await tool.execute(LoadSkillParams(name="test"))
        assert result.is_error
        assert "not properly initialized" in result.output

    def test_is_system_tool(self) -> None:
        from eviforge.tools.load_skill import LoadSkill

        tool = LoadSkill()
        assert tool.is_system_tool is True
        assert tool.category == "read"

# ---------------------------------------------------------------------------
# Agent 集成
# ---------------------------------------------------------------------------

class TestAgentSkillIntegration:
    def test_activate_and_clear(self) -> None:
        from eviforge.agent import Agent
        from eviforge.prompts import build_environment_context

        env = build_environment_context(
            "/test",
            active_skills={"commit": "Do commit stuff"},
            skill_catalog="Available: commit",
        )
        assert "Active Skills" in env
        assert "commit" in env
        assert "Do commit stuff" in env
        assert "Available: commit" in env

    def test_empty_active_skills(self) -> None:
        from eviforge.prompts import build_environment_context

        env = build_environment_context("/test")
        assert "Active Skills" not in env

    def test_agent_activate_and_clear(self) -> None:
        agent = MagicMock()
        agent.active_skills = {}

        from eviforge.agent import Agent

        real_agent = MagicMock(spec=Agent)
        real_agent.active_skills = {}
        real_agent.activate_skill = Agent.activate_skill.__get__(real_agent)
        real_agent.clear_active_skills = Agent.clear_active_skills.__get__(real_agent)

        real_agent.activate_skill("test", "SOP")
        assert "test" in real_agent.active_skills

        real_agent.clear_active_skills()
        assert len(real_agent.active_skills) == 0
