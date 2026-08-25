from mewcode.agents.parser import AgentDef
from mewcode.agents.tool_filter import resolve_agent_tools
from mewcode.tools import ToolRegistry
from mewcode.tools.synthetic_output import SyntheticOutputTool


def _registry_with_mcp_tool() -> ToolRegistry:
    registry = ToolRegistry()
    tool = SyntheticOutputTool()
    tool.name = "mcp_demo_write_remote"
    registry.register(tool)
    return registry


def test_mcp_tool_obeys_explicit_whitelist():
    definition = AgentDef(
        agent_type="read-only",
        when_to_use="test",
        tools=["ReadFile"],
        source="project",
    )

    filtered = resolve_agent_tools(_registry_with_mcp_tool(), definition)

    assert filtered.get("mcp_demo_write_remote") is None


def test_mcp_wildcard_must_be_explicit():
    definition = AgentDef(
        agent_type="mcp-worker",
        when_to_use="test",
        tools=["mcp_*"],
        source="project",
    )

    filtered = resolve_agent_tools(_registry_with_mcp_tool(), definition)

    assert filtered.get("mcp_demo_write_remote") is not None
