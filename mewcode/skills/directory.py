from __future__ import annotations

import importlib.util
import inspect
import json
import logging
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel

from mewcode.tools import ToolRegistry
from mewcode.tools.base import Tool, ToolResult

log = logging.getLogger(__name__)


def parse_tool_json(path: Path) -> list[dict[str, Any]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        log.warning("Failed to parse tool.json at %s: %s", path, e)
        return []

    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        log.warning("tool.json at %s must be a JSON array or object", path)
        return []

    return raw


class _LazyPythonImplementation:
    """Load project Python only inside ``Tool.execute`` (after the gateway)."""

    def __init__(self, script: Path, tool_name: str) -> None:
        self.script = script
        self.tool_name = tool_name
        self._execute_fn: Callable[..., Any] | None = None

    def _load(self) -> Callable[..., Any]:
        if self._execute_fn is not None:
            return self._execute_fn
        module_name = f"mewcode_skill_tool_{self.tool_name}"
        spec = importlib.util.spec_from_file_location(module_name, self.script)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"Cannot create module spec for {self.script}")
        module = importlib.util.module_from_spec(spec)
        # Top-level module code is itself a side effect.  Keep it inside
        # SkillCustomTool.execute so ExecutionGateway has already assessed the
        # unknown/dynamic-code descriptor (and rejected it for reviewed Plans).
        spec.loader.exec_module(module)
        execute_fn = getattr(module, "execute", None)
        if not callable(execute_fn):
            raise RuntimeError(f"Tool implementation {self.script} has no 'execute' function")
        self._execute_fn = execute_fn
        return execute_fn

    async def __call__(self, **kwargs: Any) -> Any:
        result = self._load()(**kwargs)
        return await result if inspect.isawaitable(result) else result


def load_tool_implementation(
    references_dir: Path, tool_name: str
) -> Callable[..., Any] | None:
    script = references_dir / f"{tool_name}.py"
    if not script.is_file():
        return None
    return _LazyPythonImplementation(script, tool_name)


class _DynamicParams(BaseModel):
    model_config = {"extra": "allow"}


class SkillCustomTool(Tool):


    def __init__(
        self,
        tool_name: str,
        description: str,
        schema: dict[str, Any],
        impl: Callable[..., Any] | None,
    ) -> None:
        self.name = tool_name
        self.description = description
        self.params_model = _DynamicParams
        self.category = "command"
        # Arbitrary project Python can perform effects at import and execution
        # time.  Treat it as opaque high-risk code; RiskEngine may raise but
        # never lower this classification.
        self.side_effect = "unknown"
        self.idempotency = "unknown"
        self.risk_tags = frozenset({"dynamic_python", "untrusted_code"})
        self.transport_kind = "unknown"
        self.is_concurrency_safe = False
        self._schema = schema
        self._impl = impl


    def get_schema(self) -> dict[str, Any]:
        input_schema = self._schema.get("parameters", self._schema.get("input_schema", {}))
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": input_schema,
        }


    async def execute(self, params: BaseModel) -> ToolResult:
        if self._impl is None:
            return ToolResult(
                output=f"Error: no implementation found for tool '{self.name}'",
                is_error=True,
            )
        try:
            kwargs = params.model_dump()
            result = self._impl(**kwargs)
            if inspect.isawaitable(result):
                result = await result
            return ToolResult(output=str(result))
        except Exception as e:
            return ToolResult(output=f"Tool execution error: {e}", is_error=True)


def register_skill_tools(skill_dir: Path, registry: ToolRegistry) -> int:
    tool_json_path = skill_dir / "tool.json"
    if not tool_json_path.is_file():
        return 0

    schemas = parse_tool_json(tool_json_path)
    references_dir = skill_dir / "references"
    count = 0

    for schema in schemas:
        tool_name = schema.get("name", "")
        if not tool_name:
            log.warning("Skipping tool with no name in %s", tool_json_path)
            continue

        if registry.get(tool_name) is not None:
            log.debug("Tool '%s' already registered, skipping", tool_name)
            continue

        description = schema.get("description", "")
        impl = load_tool_implementation(references_dir, tool_name) if references_dir.is_dir() else None

        if impl is None:
            log.warning("No implementation for tool '%s' in %s", tool_name, references_dir)

        tool = SkillCustomTool(tool_name, description, schema, impl)
        registry.register(tool)
        count += 1

    return count
