"""Operator MCP diagnostics use the same manager and wrappers as TUI/headless."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import yaml

from eviforge.config import MCPServerConfig, resolve_env_vars
from eviforge.validator import validate_mcp_servers
from eviforge.mcp.manager import MCPManager
from eviforge.mcp.recovery import reconcile, unresolved_writes
from eviforge.permissions.capabilities import normalize_action
from eviforge.tools import ToolRegistry
from eviforge.tools.work_dir import tool_working_directory


def load_servers(config: str | None, work_dir: str = ".") -> list[MCPServerConfig]:
    paths = [Path(config)] if config else [Path.home() / ".eviforge/config.yaml", Path(work_dir) / ".eviforge/config.yaml", Path(work_dir) / ".eviforge/config.local.yaml"]
    merged = {}
    for path in paths:
        if not path.is_file():
            if config:
                raise ValueError("MCP config file does not exist")
            continue
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            raise ValueError("MCP config must be a mapping")
        for item in validate_mcp_servers(document.get("mcp_servers")):
            server = MCPServerConfig(**item)
            if server.cwd is not None:
                directory = Path(resolve_env_vars(server.cwd)).expanduser()
                server.cwd = str((directory if directory.is_absolute() else Path(work_dir) / directory).resolve())
            merged[server.name] = server
    return list(merged.values())


def register_parser(subparsers: Any) -> None:
    parser = subparsers.add_parser("mcp", help="Inspect, diagnose and explicitly invoke external MCP services")
    parser.add_argument("--config", help="YAML with mcp_servers (provider config is optional)")
    parser.add_argument("--work-dir", default=".")
    parser.set_defaults(handler=handle)
    commands = parser.add_subparsers(dest="mcp_command", required=True)
    commands.add_parser("list", help="Show configured services without connecting")
    doctor = commands.add_parser("doctor", help="Configuration checks; --live discovers tools without invoking them")
    doctor.add_argument("--live", action="store_true")
    commands.add_parser("tools", help="Connect and show exact local/remote tool mapping and schema")
    for command in ("call", "intent"):
        action = commands.add_parser(command)
        action.add_argument("--server", required=True)
        action.add_argument("--tool", required=True, help="Original remote tool name")
        action.add_argument("--arguments-file", required=True, help="JSON object file")
        if command == "call":
            action.add_argument("--allow-write", action="store_true", help="Explicit operator approval for this invocation")
    recover = commands.add_parser("reconcile", help="Record an operator's remote read-back; does not replay")
    recover.add_argument("--call-id", required=True)
    recover.add_argument("--outcome", choices=["executed", "not_executed"], required=True)
    recover.add_argument("--evidence", required=True)


async def handle(args: argparse.Namespace) -> int:
    directory = Path(args.work_dir).resolve()
    journal = directory / ".eviforge/mcp"
    if args.mcp_command == "reconcile":
        reconcile(journal, args.call_id, args.outcome, args.evidence)
        print(json.dumps({"reconciled": args.call_id, "remaining": unresolved_writes(journal)}))
        return 0
    manager = MCPManager()
    manager.load_configs(load_servers(args.config, str(directory)))
    if args.mcp_command == "list" or args.mcp_command == "doctor" and not args.live:
        print(json.dumps({"servers": manager.status(), "unresolved_writes": unresolved_writes(journal)}, indent=2))
        return 0
    registry = ToolRegistry()
    try:
        errors = await manager.register_all_tools(registry)
        if args.mcp_command == "doctor":
            result = {"servers": manager.status(), "errors": errors, "unresolved_writes": unresolved_writes(journal)}
            code = 1 if errors or manager.required_failures else 0
        elif args.mcp_command == "tools":
            result = {"servers": manager.status(), "tools": [{"server": tool.server_name, "remote_name": tool.mcp_tool_name, "category": tool.category, "capability_fingerprint": tool.capability_fingerprint, **tool.get_schema()} for tool in registry.list_tools()], "errors": errors}
            code = 1 if errors else 0
        else:
            tool = next((tool for tool in registry.list_tools() if tool.server_name == args.server and tool.mcp_tool_name == args.tool), None)
            if tool is None:
                raise ValueError("Selected MCP tool is unavailable or locally disabled")
            arguments = json.loads(Path(args.arguments_file).read_text(encoding="utf-8"))
            tool.resource_ids(arguments, str(directory))
            if args.mcp_command == "intent":
                result = normalize_action({"tool_name": tool.name, "arguments": arguments}, str(directory), registry.get).as_dict()
                code = 0
            else:
                if tool.category != "read" and not args.allow_write:
                    raise ValueError("This tool requires explicit --allow-write operator authorization")
                with tool_working_directory(directory):
                    output = await tool.execute(tool.validate_arguments(arguments))
                result = {"output": output.output, "is_error": output.is_error, "execution_status": output.execution_status, "structured_content": output.structured_content, "artifacts": output.artifacts}
                code = 4 if output.execution_status == "ambiguous" else int(output.is_error)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return code
    finally:
        await manager.shutdown()
