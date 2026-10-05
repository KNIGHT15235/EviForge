"""Discover actual MCP servers through EviForge; private evidence stays ignored."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

from eviforge.mcp.cli import load_servers
from eviforge.mcp.manager import MCPManager
from eviforge.tools import ToolRegistry


async def inspect(args):
    if args.credentials:
        values = json.loads(Path(args.credentials).read_text(encoding="utf-8"))
        for name, value in values.items():
            if name.startswith("EVIFORGE_") and isinstance(value, str):
                os.environ[name] = value
    manager, registry = MCPManager(), ToolRegistry()
    manager.load_configs(load_servers(args.config, str(Path.cwd())))
    try:
        errors = await manager.register_all_tools(registry)
        report = {"servers": manager.status(), "errors": errors, "tools": [{"server": tool.server_name, "remote_name": tool.mcp_tool_name, "category": tool.category, **tool.get_schema()} for tool in registry.list_tools()]}
        target = Path(args.output)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"servers": manager.status(), "errors": errors, "schema_evidence": str(target)}, ensure_ascii=False))
        return bool(errors)
    finally:
        await manager.shutdown()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--credentials", help="Explicit local ignored JSON; values are never printed")
    parser.add_argument("--output", default=".eviforge/integration/discovery.json")
    raise SystemExit(asyncio.run(inspect(parser.parse_args())))
