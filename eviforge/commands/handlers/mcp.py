
from __future__ import annotations

from eviforge.commands.registry import Command, CommandContext, CommandType


async def handle_mcp(ctx: CommandContext) -> None:
    app = ctx.ui
    info = getattr(app, "_mcp_server_info", "")
    mcp_mgr = getattr(app, "mcp_manager", None)
    if not info and not (mcp_mgr and mcp_mgr.status()):
        ctx.ui.add_system_message("No MCP servers connected")
        return

    lines = ["MCP 状态", "─────────────"]
    lines.append(info)

    if mcp_mgr:
        for state in mcp_mgr.status():
            name = state["name"]
            tool_names = mcp_mgr.tool_names(name)
            lines.append(f"\n  state: {state['state']}")
            if state.get('error'):
                lines.append(f"  error: {state['error']}")
            lines.append(f"\n  {name}: {len(tool_names)} tools")
            for tn in tool_names[:10]:
                lines.append(f"    - {tn}")
            if len(tool_names) > 10:
                lines.append(f"    … and {len(tool_names) - 10} more")

    ctx.ui.add_system_message("\n".join(lines))


MCP_COMMAND = Command(
    name="mcp",
    aliases=[],
    description="显示 MCP 服务器状态",
    usage="/mcp",
    type=CommandType.LOCAL,
    handler=handle_mcp,
)
