from __future__ import annotations

from typing import TYPE_CHECKING, Any

from mewcode.context_router import (
    ArtifactKind,
    HybridContextRouter,
    RouterArtifact,
    RouterMetrics,
    RouterQuery,
)
from mewcode.tools.base import Tool

if TYPE_CHECKING:
    from mewcode.cache import FileCache


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._disabled: set[str] = set()
        self._discovered: set[str] = set()
        self._hybrid_router = HybridContextRouter()
        self._search_metrics: list[RouterMetrics] = []

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool
        self._index_tool(tool)

    @staticmethod
    def _schema_token_estimate(tool: Tool) -> int:
        import json

        return max(1, (len(json.dumps(tool.get_schema(), ensure_ascii=False)) + 3) // 4)

    def _index_tool(self, tool: Tool) -> None:
        if not getattr(tool, "should_defer", False):
            self._hybrid_router.remove(f"tool:{tool.name}")
            return
        self._hybrid_router.upsert(
            RouterArtifact(
                artifact_id=f"tool:{tool.name}",
                kind=ArtifactKind.TOOL,
                name=tool.name,
                description=tool.description or "",
                keywords=(getattr(tool, "category", ""),),
                risk_level=getattr(tool, "risk_level", None),
                estimated_tokens=self._schema_token_estimate(tool),
            )
        )

    @property
    def hybrid_router(self) -> HybridContextRouter:
        return self._hybrid_router

    @property
    def search_metrics(self) -> tuple[RouterMetrics, ...]:
        return tuple(self._search_metrics)

    @property
    def latest_search_metrics(self) -> RouterMetrics | None:
        return self._search_metrics[-1] if self._search_metrics else None

    def record_search_metrics(self, metrics: RouterMetrics) -> None:
        self._search_metrics.append(metrics)
        if len(self._search_metrics) > 100:
            del self._search_metrics[:-100]

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)


    def is_enabled(self, name: str) -> bool:
        return name in self._tools and name not in self._disabled

    def enable(self, name: str) -> None:
        self._disabled.discard(name)


    def disable(self, name: str) -> None:
        if name in self._tools:
            self._disabled.add(name)

    def unregister(self, name: str) -> Tool | None:
        tool = self._tools.pop(name, None)
        self._disabled.discard(name)
        self._discovered.discard(name)
        self._hybrid_router.remove(f"tool:{name}")
        return tool

    def enable_all(self) -> None:
        self._disabled.clear()


    def mark_discovered(self, name: str) -> None:
        self._discovered.add(name)

    def is_discovered(self, name: str) -> bool:
        return name in self._discovered


    def get_deferred_tool_names(self) -> list[str]:
        return [
            name
            for name, tool in self._tools.items()
            if getattr(tool, "should_defer", False)
            and name not in self._discovered
            and name not in self._disabled
        ]

    def search_deferred(
        self,
        query: str,
        max_results: int,
        protocol: str = "anthropic",
        *,
        already_surfaced: set[str] | frozenset[str] | None = None,
        token_budget: int = 8_192,
    ) -> list[dict[str, Any]]:
        result = self._hybrid_router.search(
            RouterQuery(
                text=query,
                kinds=frozenset({ArtifactKind.TOOL}),
                max_results=max_results,
                token_budget=token_budget,
                already_surfaced=frozenset(
                    self._discovered if already_surfaced is None else already_surfaced
                ),
            )
        )
        self.record_search_metrics(result.metrics)
        routed_names = [item.artifact.name for item in result.items]
        if routed_names:
            return self.find_deferred_by_names(routed_names, protocol)
        # Compatibility fallback for legacy edge cases (for example punctuation
        # only tool descriptions). This keeps discovery usable and observable.
        query_lower = query.lower()
        scored: list[tuple[int, str, Tool]] = []
        surfaced = self._discovered if already_surfaced is None else already_surfaced
        for name, tool in self._tools.items():
            if not getattr(tool, "should_defer", False):
                continue
            if name in self._disabled:
                continue
            if name in surfaced:
                continue
            score = 0
            name_lower = name.lower()
            desc_lower = (tool.description or "").lower()
            if query_lower in name_lower:
                score += 10
            if query_lower in desc_lower:
                score += 5
            for word in query_lower.split():
                if word in name_lower:
                    score += 3
                if word in desc_lower:
                    score += 1
            if score > 0:
                scored.append((score, name, tool))
        scored.sort(key=lambda x: x[0], reverse=True)
        results: list[dict[str, Any]] = []
        for _, _name, tool in scored[:max_results]:
            base = tool.get_schema()
            if protocol in ("openai", "openai-compat"):
                results.append({
                    "type": "function",
                    "name": base["name"],
                    "description": base["description"],
                    "parameters": base["input_schema"],
                })
            else:
                results.append(base)
        if results:
            fallback_metrics = result.metrics.model_copy(update={"fallback_used": True})
            self._search_metrics[-1] = fallback_metrics
        return results

    def find_deferred_by_names(
        self, names: list[str], protocol: str = "anthropic"
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for name in names:
            tool = self._tools.get(name)
            if tool is None:
                continue
            if not getattr(tool, "should_defer", False):
                continue
            if name in self._disabled:
                continue
            base = tool.get_schema()
            if protocol in ("openai", "openai-compat"):
                results.append({
                    "type": "function",
                    "name": base["name"],
                    "description": base["description"],
                    "parameters": base["input_schema"],
                })
            else:
                results.append(base)
        return results

    def list_tools(self) -> list[Tool]:
        return list(self._tools.values())


    def get_all_schemas(self, protocol: str = "anthropic") -> list[dict[str, Any]]:
        schemas: list[dict[str, Any]] = []
        for name, tool in self._tools.items():
            if name in self._disabled:
                continue
            if getattr(tool, "should_defer", False) and name not in self._discovered:
                continue
            base = tool.get_schema()
            if protocol in ("openai", "openai-compat"):
                schemas.append({
                    "type": "function",
                    "name": base["name"],
                    "description": base["description"],
                    "parameters": base["input_schema"],
                })
            else:
                schemas.append(base)
        return schemas


def create_default_registry(file_cache: FileCache | None = None, file_history: Any = None) -> ToolRegistry:
    from mewcode.tools.bash import Bash
    from mewcode.tools.edit_file import EditFile
    from mewcode.tools.file_state_cache import FileStateCache
    from mewcode.tools.glob import Glob
    from mewcode.tools.grep import Grep
    from mewcode.tools.read_file import ReadFile
    from mewcode.tools.write_file import WriteFile

    file_state_cache = FileStateCache()

    registry = ToolRegistry()
    registry.register(ReadFile(file_cache=file_cache, file_state_cache=file_state_cache))
    registry.register(WriteFile(file_cache=file_cache, file_history=file_history, file_state_cache=file_state_cache))
    registry.register(EditFile(file_cache=file_cache, file_history=file_history, file_state_cache=file_state_cache))
    registry.register(Bash())
    registry.register(Glob())
    registry.register(Grep())
    return registry
