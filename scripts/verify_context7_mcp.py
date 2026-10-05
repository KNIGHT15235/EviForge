"""Real Context7 documentation queries through the production MCP wrapper."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from eviforge.config import MCPServerConfig
from eviforge.mcp.manager import MCPManager
from eviforge.tools import ToolRegistry
from eviforge.tools.work_dir import tool_working_directory


async def verify():
    manager, registry = MCPManager(), ToolRegistry()
    manager.load_configs([MCPServerConfig("context7", integration="context7", required=True, url="https://mcp.context7.com/mcp", allowed_tools=["resolve-library-id", "query-docs"])])
    report = {"service": "context7", "checks": [], "status": "running"}
    try:
        errors = await manager.register_all_tools(registry)
        if errors:
            raise RuntimeError("; ".join(errors))
        tools = {tool.mcp_tool_name: tool for tool in registry.list_tools()}
        with tool_working_directory(Path.cwd()):
            tool = tools["resolve-library-id"]
            resolved = await tool.execute(tool.validate_arguments({"libraryName": "pydantic", "query": "Pydantic version 2 model validation with model_validate"}))
            if resolved.is_error or "/pydantic/pydantic" not in resolved.output:
                raise RuntimeError("Library resolution did not return the expected public library")
            report["checks"].append({"tool": "resolve-library-id", "passed": True})
            tool = tools["query-docs"]
            docs = await tool.execute(tool.validate_arguments({"libraryId": "/pydantic/pydantic", "query": "Pydantic version 2 model_validate validation errors"}))
            if docs.is_error or "model_validate" not in docs.output:
                raise RuntimeError("Documentation query did not return model_validate documentation")
            report["checks"].append({"tool": "query-docs", "passed": True})
            report["documentation_characters"] = len(docs.output)
        report["status"] = "passed"
    except Exception as exc:
        report.update(status="failed", error=f"{type(exc).__name__}: {exc}")
    finally:
        await manager.shutdown()
    path = Path(".eviforge/integration/context7-acceptance.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report))
    return report["status"] != "passed"


if __name__ == "__main__":
    raise SystemExit(asyncio.run(verify()))
