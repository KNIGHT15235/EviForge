
from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from likecc.tools.base import Tool, ToolResult
from likecc.tools.work_dir import resolve_tool_path

if TYPE_CHECKING:
    from likecc.cache import FileCache
    from likecc.tools.file_state_cache import FileStateCache


class Params(BaseModel):
    file_path: str = Field(description="Absolute or relative path to the file to read")
    offset: int = Field(default=0, description="Line offset to start reading from (0-based)")
    limit: int = Field(default=2000, description="Maximum number of lines to read")


class ReadFile(Tool):
    name = "ReadFile"
    description = "Read a file and return its contents with line numbers."
    params_model = Params
    category = "read"
    is_concurrency_safe = True


    def __init__(self, file_cache: FileCache | None = None, file_state_cache: FileStateCache | None = None) -> None:
        self._cache = file_cache
        self._state_cache = file_state_cache


    async def execute(self, params: Params) -> ToolResult:
        path = resolve_tool_path(params.file_path)
        if not path.exists():
            return ToolResult(output=f"Error: file not found: {params.file_path}", is_error=True)
        if not path.is_file():
            return ToolResult(output=f"Error: not a file: {params.file_path}", is_error=True)

        resolved = str(path.resolve())

        try:
            before = path.stat()
            version = (before.st_mtime_ns, before.st_ctime_ns, before.st_size, before.st_ino)
            text = self._cache.get(resolved, version=version) if self._cache is not None else None
            if text is None:
                text = path.read_text(encoding="utf-8")
            after = path.stat()
            after_version = (after.st_mtime_ns, after.st_ctime_ns, after.st_size, after.st_ino)
            if version != after_version:
                if self._cache is not None:
                    self._cache.invalidate(resolved)
                return ToolResult(
                    output="Error: file changed while being read. Read it again before editing.",
                    is_error=True,
                )
            if self._cache is not None:
                self._cache.put(resolved, text, version=version)
        except Exception as e:
            return ToolResult(output=f"Error reading file: {e}", is_error=True)

        if self._state_cache is not None:
            self._state_cache.record(resolved, text, before.st_mtime_ns)

        lines = text.splitlines()
        selected = lines[params.offset : params.offset + params.limit]
        numbered = [f"{i + params.offset + 1}\t{line}" for i, line in enumerate(selected)]
        return ToolResult(output="\n".join(numbered))
