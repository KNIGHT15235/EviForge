
from __future__ import annotations

from copy import copy
from typing import TYPE_CHECKING, Any

from eviforge.tools import ToolRegistry

if TYPE_CHECKING:
    from eviforge.agents.parser import AgentDef
    from eviforge.teams.manager import TeamManager
    from eviforge.tools.base import Tool

ALL_AGENT_DISALLOWED_TOOLS: frozenset[str] = frozenset({
    "TaskOutput",
    "ExitPlanMode",
    "EnterPlanMode",
    "Agent",
    "AskUserQuestion",
    "TaskStop",
    "Workflow",
    "EnterWorktree",
    "ExitWorktree",
})

CUSTOM_AGENT_DISALLOWED_TOOLS: frozenset[str] = frozenset({
    "TaskOutput",
    "ExitPlanMode",
    "EnterPlanMode",
    "Agent",
    "AskUserQuestion",
    "TaskStop",
    "Workflow",
})

ASYNC_AGENT_ALLOWED_TOOLS: frozenset[str] = frozenset({
    "ReadFile",
    "WebSearch",
    "TodoWrite",
    "Grep",
    "WebFetch",
    "Glob",
    "Bash",
    "EditFile",
    "WriteFile",
    "NotebookEdit",
    "Skill",
    "LoadSkill",
    "SyntheticOutput",
    "ToolSearch",
})

TEAMMATE_COORDINATION_TOOLS: frozenset[str] = frozenset({
    "TaskCreate",
    "TaskGet",
    "TaskList",
    "TaskUpdate",
    "SendMessage",
})

IN_PROCESS_TEAMMATE_ALLOWED_TOOLS: frozenset[str] = (
    ASYNC_AGENT_ALLOWED_TOOLS | TEAMMATE_COORDINATION_TOOLS | frozenset({
        "CronCreate",
        "CronDelete",
        "CronList",
    })
)

COORDINATOR_MODE_ALLOWED_TOOLS: frozenset[str] = frozenset({
    "Agent",
    "TaskStop",
    "SendMessage",
    "SyntheticOutput",
    "TeamCreate",
    "TeamDelete",
})


def _is_mcp_tool(name: str) -> bool:
    return name.startswith("mcp_")


def clone_agent_registry(
    parent_registry: ToolRegistry,
    tools: list[Tool] | None = None,
    *,
    preserve_discovery: bool = False,
) -> ToolRegistry:
    """Share stateless tools while giving each Agent its own mutable tool state.

    LoadSkill's owner must be bound after the child Agent is constructed.
    Fork callers can preserve discovered schemas and registration order.
    """
    from eviforge.cache import FileCache
    from eviforge.tools.edit_file import EditFile
    from eviforge.tools.file_state_cache import FileStateCache
    from eviforge.tools.impl.tool_search import ToolSearchTool
    from eviforge.tools.load_skill import LoadSkill
    from eviforge.tools.read_file import ReadFile
    from eviforge.tools.write_file import WriteFile

    registry = ToolRegistry()
    file_cache = FileCache()
    file_state_cache = FileStateCache()
    registry.register_file_cache_clearer(file_cache.clear)
    registry.register_file_cache_clearer(file_state_cache.clear)
    for original in parent_registry.list_tools() if tools is None else tools:
        tool = original
        if isinstance(original, (ReadFile, WriteFile, EditFile)):
            tool = copy(original)
            tool._cache = file_cache
            tool._state_cache = file_state_cache
            if isinstance(tool, (WriteFile, EditFile)):
                tool.file_history = None
        elif isinstance(original, ToolSearchTool):
            tool = copy(original)
            tool._registry = registry
        elif isinstance(original, LoadSkill):
            tool = copy(original)
            tool._agent = None
        registry.register(tool)
        if not parent_registry.is_enabled(tool.name):
            registry.disable(tool.name)
        if preserve_discovery and parent_registry.is_discovered(tool.name):
            registry.mark_discovered(tool.name)
    return registry


def resolve_agent_tools(
    parent_registry: ToolRegistry,
    definition: AgentDef,
    is_background: bool = False,
) -> ToolRegistry:
    all_tools = {t.name: t for t in parent_registry.list_tools()}

    # MCP tools bypass the built-in background whitelist, but must still obey
    # an Agent definition's explicit capability restrictions below.
    mcp_tools = {name: tool for name, tool in all_tools.items() if _is_mcp_tool(name)}
    all_tools = {name: tool for name, tool in all_tools.items() if not _is_mcp_tool(name)}

    # 第 1 层：全局禁用工具
    for name in ALL_AGENT_DISALLOWED_TOOLS:
        all_tools.pop(name, None)

    # 第 2 层：自定义 agent 额外限制
    if definition.source in ("project", "user", "plugin"):
        for name in CUSTOM_AGENT_DISALLOWED_TOOLS:
            all_tools.pop(name, None)

    # 第 3 层：后台任务白名单
    if is_background:
        all_tools = {
            name: tool
            for name, tool in all_tools.items()
            if name in ASYNC_AGENT_ALLOWED_TOOLS
        }

    all_tools = {**mcp_tools, **all_tools}

    # 第 4 层：按 agent 定义中的禁用/允许列表过滤
    if definition.disallowed_tools:
        for name in definition.disallowed_tools:
            all_tools.pop(name, None)

    if definition.tools:
        allowed_set = set(definition.tools)
        all_tools = {
            name: tool
            for name, tool in all_tools.items()
            if name in allowed_set
        }

    return clone_agent_registry(parent_registry, list(all_tools.values()))


def build_teammate_tools(
    parent_registry: ToolRegistry,
    team_manager: TeamManager,
    team_name: str,
    agent_id: str,
    agent_name: str,
    backend_type: str,
    definition: AgentDef | None = None,
) -> ToolRegistry:
    from eviforge.teams.models import BackendType
    from eviforge.tools.send_message import SendMessageTool
    from eviforge.tools.task_create import TaskCreateTool
    from eviforge.tools.task_get import TaskGetTool
    from eviforge.tools.task_list import TaskListTool
    from eviforge.tools.task_update import TaskUpdateTool

    if backend_type == BackendType.IN_PROCESS.value:
        all_tools = {t.name: t for t in parent_registry.list_tools()}
        filtered = {
            name: tool
            for name, tool in all_tools.items()
            if name in IN_PROCESS_TEAMMATE_ALLOWED_TOOLS or _is_mcp_tool(name)
        }
    else:
        filtered = {t.name: t for t in parent_registry.list_tools()}
        filtered.pop("TeamCreate", None)
        filtered.pop("TeamDelete", None)

    # 应用 agent 定义中的工具限制
    if definition is not None:
        if definition.disallowed_tools:
            for name in definition.disallowed_tools:
                filtered.pop(name, None)
        if definition.tools:
            allowed_set = set(definition.tools) | TEAMMATE_COORDINATION_TOOLS
            filtered = {
                name: tool
                for name, tool in filtered.items()
                if name in allowed_set
            }

    coordination_tools = [
        TaskCreateTool(team_manager, team_name, agent_name),
        TaskGetTool(team_manager, team_name),
        TaskListTool(team_manager, team_name),
        TaskUpdateTool(team_manager, team_name),
        SendMessageTool(team_manager, team_name, agent_id, agent_name),
    ]

    registry = clone_agent_registry(parent_registry, list(filtered.values()))
    for tool in coordination_tools:
        registry.register(tool)

    return registry


def apply_coordinator_filter(registry: ToolRegistry) -> ToolRegistry:
    all_tools = {t.name: t for t in registry.list_tools()}
    filtered = ToolRegistry()
    for name, tool in all_tools.items():
        if name in COORDINATOR_MODE_ALLOWED_TOOLS:
            filtered.register(tool)
    return filtered
