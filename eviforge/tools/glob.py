
from __future__ import annotations

from pydantic import BaseModel, Field

from eviforge.tools.base import SKIP_DIRS, Tool, ToolResult
from eviforge.tools.work_dir import (
    is_path_within,
    is_safe_relative_pattern,
    resolve_tool_path,
)


class Params(BaseModel):
    pattern: str = Field(description="Glob pattern to match (e.g. '**/*.py')")
    path: str = Field(default=".", description="Base directory to search from")


class Glob(Tool):
    name = "Glob"
    description = "Find files matching a glob pattern, returning relative paths."
    params_model = Params
    category = "read"
    is_concurrency_safe = True


    async def execute(self, params: Params) -> ToolResult:
        base = resolve_tool_path(params.path)
        if not base.exists():
            return ToolResult(output=f"Error: path not found: {params.path}", is_error=True)
        if not is_safe_relative_pattern(params.pattern):
            return ToolResult(
                output="Error: glob pattern must stay within the search path",
                is_error=True,
            )

        try:
            matches = sorted(
                str(p.relative_to(base))
                for p in base.glob(params.pattern)
                if p.is_file()
                and is_path_within(p, base)
                and not any(part in SKIP_DIRS for part in p.parts)
            )
        except Exception as e:
            return ToolResult(output=f"Error: {e}", is_error=True)

        if not matches:
            return ToolResult(output="No files matched the pattern.")
        return ToolResult(output="\n".join(matches))
