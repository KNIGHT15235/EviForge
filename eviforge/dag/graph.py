"""Offline topology, reference contracts and conservative path conflicts."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from eviforge.dag.models import GraphSpec, NodeSpec, INPUT_ROLE_FIELDS


class DAGError(ValueError):
    pass


class DriftError(DAGError):
    pass


class ReplayRequired(DAGError):
    pass


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode()).hexdigest()


def validate_graph(graph: GraphSpec) -> list[str]:
    nodes = {node.id: node for node in graph.nodes}
    if len(nodes) != len(graph.nodes):
        raise DAGError("Duplicate node IDs")
    for node in graph.nodes:
        if len(set(node.depends_on)) != len(node.depends_on):
            raise DAGError(f"Duplicate dependencies: {node.id}")
        if len({ref.node_id for ref in node.inputs}) != len(node.inputs):
            raise DAGError(f"Duplicate input references: {node.id}")
        for dep in node.depends_on:
            if dep not in nodes:
                raise DAGError(f"Missing dependency {dep} for {node.id}")
        for ref in node.inputs:
            if ref.node_id not in node.depends_on:
                raise DAGError(f"Input {ref.node_id} is not a declared dependency of {node.id}")
            if nodes[ref.node_id].role != ref.role:
                raise DAGError(f"Input role mismatch: {node.id}/{ref.node_id}")
            if ref.role not in INPUT_ROLE_FIELDS[node.role]:
                raise DAGError(f"Role {node.role} cannot consume a {ref.role} input")
        roles = {ref.role for ref in node.inputs}
        if node.role == "verifier" and "implementer" not in roles:
            raise DAGError("Verifier requires an Implementer input")
        if node.role == "integrator" and not {"implementer", "verifier"} <= roles:
            raise DAGError("Integrator requires Implementer and Verifier inputs")
        if node.role in {"explorer", "verifier"} and node.write_set:
            raise DAGError(f"Role {node.role} cannot declare file writes")
        if node.role != "verifier" and node.commands:
            raise DAGError("Only Verifier may declare exact verification commands")
    order: list[str] = []
    remaining = set(nodes)
    while remaining:
        ready = sorted(n for n in remaining if set(nodes[n].depends_on) <= set(order))
        if not ready:
            raise DAGError("Graph contains a dependency cycle")
        order.extend(ready)
        remaining.difference_update(ready)
    return order


def graph_hash(graph: GraphSpec) -> str:
    data = graph.model_dump(mode="json")
    data["nodes"] = sorted(data["nodes"], key=lambda n: n["id"])
    for node in data["nodes"]:
        node["depends_on"].sort()
        node["inputs"].sort(key=lambda r: r["node_id"])
    return canonical_hash(data)


def within(path: Path, scope: Path) -> bool:
    # Case-folding is conservative across Windows and mounted Windows drives.
    p, s = str(path).replace("\\", "/").casefold(), str(scope).replace("\\", "/").casefold()
    return p == s or p.startswith(s.rstrip("/") + "/")


def conflicts(a: NodeSpec, b: NodeSpec, root: Path) -> bool:
    if a.commands or b.commands:
        return True  # Arbitrary test programs may touch any file.
    def overlap(left: list[str], right: list[str]) -> bool:
        return any(within((root / x).resolve(), (root / y).resolve()) or
                   within((root / y).resolve(), (root / x).resolve())
                   for x in left for y in right)
    return (overlap(a.write_set, b.write_set + b.read_set) or
            overlap(b.write_set, a.read_set))
