"""Validated task graph and critical-path calculations."""

from __future__ import annotations

from collections import deque

from pydantic import BaseModel, ConfigDict, model_validator

from .models import TaskNode


class TaskGraph(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    nodes: tuple[TaskNode, ...]

    @model_validator(mode="after")
    def _valid_dag(self) -> "TaskGraph":
        if not self.nodes:
            raise ValueError("a task graph must contain at least one node")
        ids = [node.node_id for node in self.nodes]
        if len(ids) != len(set(ids)):
            raise ValueError("task graph contains duplicate node ids")
        known = set(ids)
        missing = sorted(
            {dependency for node in self.nodes for dependency in node.depends_on} - known
        )
        if missing:
            raise ValueError(f"unknown dependencies: {', '.join(missing)}")

        indegree = {node.node_id: len(node.depends_on) for node in self.nodes}
        children = self.children_map
        queue = deque(sorted(node_id for node_id, degree in indegree.items() if degree == 0))
        visited = 0
        while queue:
            current = queue.popleft()
            visited += 1
            for child in children[current]:
                indegree[child] -= 1
                if indegree[child] == 0:
                    queue.append(child)
        if visited != len(self.nodes):
            cyclic = sorted(node_id for node_id, degree in indegree.items() if degree > 0)
            raise ValueError(f"task graph contains a cycle involving: {', '.join(cyclic)}")
        return self

    @property
    def by_id(self) -> dict[str, TaskNode]:
        return {node.node_id: node for node in self.nodes}

    @property
    def children_map(self) -> dict[str, tuple[str, ...]]:
        children: dict[str, list[str]] = {node.node_id: [] for node in self.nodes}
        for node in self.nodes:
            for dependency in node.depends_on:
                children[dependency].append(node.node_id)
        return {node_id: tuple(sorted(values)) for node_id, values in children.items()}

    def critical_path_weights(self) -> dict[str, float]:
        """Return remaining estimated path length from every node."""

        by_id = self.by_id
        children = self.children_map
        memo: dict[str, float] = {}

        def visit(node_id: str) -> float:
            if node_id not in memo:
                tail = max((visit(child) for child in children[node_id]), default=0.0)
                memo[node_id] = by_id[node_id].estimated_duration_seconds + tail
            return memo[node_id]

        for node in self.nodes:
            visit(node.node_id)
        return memo

    @property
    def critical_path_seconds(self) -> float:
        return max(self.critical_path_weights().values(), default=0.0)

    def descendants(self, node_id: str) -> set[str]:
        children = self.children_map
        result: set[str] = set()
        queue = deque(children[node_id])
        while queue:
            descendant = queue.popleft()
            if descendant in result:
                continue
            result.add(descendant)
            queue.extend(children[descendant])
        return result
