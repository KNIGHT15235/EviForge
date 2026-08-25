from __future__ import annotations

from mewcode.commands.registry import Command, CommandContext, CommandType


def _format_statuses(manager) -> str:
    statuses = manager.statuses()
    if not statuses:
        return "No MCP servers configured"
    lines = ["MCP 状态", "─────────────"]
    for status in statuses:
        marker = "connected" if status.connected else "unavailable"
        lines.append(
            f"  {status.name}: {marker}, {len(status.tool_names)} tools "
            f"[{status.transport}]"
        )
        for tool_name in status.tool_names[:10]:
            lines.append(f"    - {tool_name}")
        if len(status.tool_names) > 10:
            lines.append(f"    … and {len(status.tool_names) - 10} more")
        if status.error:
            lines.append(f"    error: {status.error}")
    return "\n".join(lines)


async def handle_mcp(ctx: CommandContext) -> None:
    manager = getattr(ctx.ui, "mcp_manager", None)
    if manager is None:
        ctx.ui.add_system_message("MCP manager is not initialized")
        return

    parts = ctx.args.split()
    subcommand = parts[0] if parts else "list"
    if subcommand in {"list", "status"}:
        ctx.ui.add_system_message(_format_statuses(manager))
        return

    if subcommand in {"reconnect", "test", "enable", "disable"}:
        if len(parts) != 2:
            ctx.ui.add_system_message(f"用法: /mcp {subcommand} <server>")
            return
        name = parts[1]
        registry = ctx.agent.registry if ctx.agent else None
        if registry is None:
            ctx.ui.add_system_message("Tool registry is not initialized")
            return
        try:
            if subcommand in {"reconnect", "test"}:
                status = await manager.reconnect(name, registry)
                message = (
                    f"MCP {name}: connected, {len(status.tool_names)} tools"
                    if status.connected
                    else f"MCP {name}: failed: {status.error}"
                )
            else:
                enabled = subcommand == "enable"
                manager.set_enabled(name, registry, enabled)
                message = f"MCP {name} tools {'enabled' if enabled else 'disabled'}"
        except KeyError as exc:
            message = str(exc)
        except Exception as exc:
            message = f"MCP {name} operation failed: {type(exc).__name__}: {exc}"
        ctx.ui.add_system_message(message)
        return

    ctx.ui.add_system_message(
        "用法: /mcp [list | test <server> | reconnect <server> | "
        "enable <server> | disable <server>]"
    )


MCP_COMMAND = Command(
    name="mcp",
    aliases=[],
    description="查看、测试和管理 MCP 服务器",
    usage=(
        "/mcp [list | test <server> | reconnect <server> | "
        "enable <server> | disable <server>]"
    ),
    type=CommandType.LOCAL,
    handler=handle_mcp,
)
