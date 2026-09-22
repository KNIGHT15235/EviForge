"""Dependency scheduler with transactional claims and conservative replay policy."""
from __future__ import annotations

import asyncio
import hashlib
import uuid
from pathlib import Path
from typing import Any
from eviforge.dag.agent_runner import AgentNodeRunner, NodeContext
from eviforge.dag.artifacts import ArtifactStore
from eviforge.dag.capabilities import capability_hash
from eviforge.dag.graph import (DAGError, DriftError, ReplayRequired, canonical_hash,
                                conflicts, graph_hash, validate_graph)
from eviforge.dag.journal import SQLiteJournal
from eviforge.dag.models import DAGRunResult, GraphSpec, NodeResult, OUTPUT_ADAPTER


class DAGRunner:
    def __init__(self, parent_agent: Any, journal_path: str | Path | None = None):
        self.parent = parent_agent
        self.root = Path(parent_agent.work_dir).resolve()
        self.journal_path = Path(journal_path) if journal_path else self.root / ".eviforge" / "dag" / "journal.sqlite3"

    async def run(self, graph: GraphSpec, *, run_id: str | None = None,
                  resume: bool = False, replay_nodes: set[str] | None = None,
                  replay_reason: str = "") -> DAGRunResult:
        order = validate_graph(graph)
        nodes = {node.id: node for node in graph.nodes}
        replay_nodes = replay_nodes or set()
        if replay_nodes and (not resume or not replay_reason.strip() or not replay_nodes <= set(nodes)):
            raise DAGError("Replay authorization requires resume, known nodes and a reason")
        run_id = run_id or uuid.uuid4().hex
        if not run_id or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in run_id):
            raise DAGError("Invalid run identifier")
        initial_capabilities = capability_hash(self.parent, graph.nodes)
        def guard() -> None:
            if capability_hash(self.parent, graph.nodes) != initial_capabilities:
                raise DriftError("Effective capabilities drifted during execution")
        journal = SQLiteJournal(self.journal_path)
        artifacts = ArtifactStore(self.journal_path.parent / "artifacts")
        tasks: dict[asyncio.Task, str] = {}
        active = False
        result_nodes: dict[str, NodeResult] = {}
        try:
            journal.begin_run(run_id, graph_hash(graph), initial_capabilities, order, resume=resume)
            active = True
            for path, expected in journal.expected_workspace().items():
                target = self.root / path
                if not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != expected:
                    raise DriftError(f"Workspace differs from the latest completed checkpoint: {path}")
            checkpoint = journal.checkpoint()
            for node_id in order:
                saved = checkpoint[node_id]
                status = saved["status"]
                if status == "completed":
                    if node_id in replay_nodes:
                        raise ReplayRequired("Completed nodes cannot be replayed")
                    output = OUTPUT_ADAPTER.validate_json(saved["output"])
                    if output.role != nodes[node_id].role:
                        raise DriftError("Checkpoint output role differs")
                    for ref in output.evidence:
                        if (ref.run_id != run_id or ref.node_id != node_id or
                                ref.attempt != saved["attempt"] or not journal.has_artifact(ref)):
                            raise DriftError("Checkpoint evidence is not registered")
                        artifacts.verify(ref)
                    inputs = {ref.node_id: result_nodes[ref.node_id].output.model_dump()
                              for ref in nodes[node_id].inputs}
                    if canonical_hash(inputs) != saved["input_hash"]:
                        raise DriftError("Completed node input digest differs")
                    result_nodes[node_id] = NodeResult(status="completed", output=output)
                elif status in {"running", "failed", "ambiguous", "cancelled"}:
                    if nodes[node_id].writable and node_id not in replay_nodes:
                        journal.set_node(node_id, "ambiguous", error="Writable attempt requires explicit replay authorization")
                        raise ReplayRequired(f"Writable node {node_id} requires explicit replay authorization")
                    if node_id in replay_nodes:
                        journal.append_event(node_id, saved["attempt"], "replay_authorized", {"reason": replay_reason})
                    journal.set_node(node_id, "pending")
                    result_nodes[node_id] = NodeResult(status="pending")
                else:
                    # A dependency failure may have blocked this node on the previous run.
                    journal.set_node(node_id, "pending")
                    result_nodes[node_id] = NodeResult(status="pending")
            runner = AgentNodeRunner(self.parent)

            async def execute(node_id: str) -> NodeResult:
                node = nodes[node_id]
                guard()
                inputs = {ref.node_id: result_nodes[ref.node_id].output for ref in node.inputs}
                for ref in node.inputs:
                    if inputs[ref.node_id] is None or inputs[ref.node_id].role != ref.role:
                        raise DAGError("Runtime input reference contract mismatch")
                if node.role == "integrator":
                    implementations = {key for key, value in inputs.items() if value.role == "implementer"}
                    covered = set()
                    for output in inputs.values():
                        if output.role == "verifier" and output.verdict != "pass":
                            journal.set_node(node_id, "blocked", error="Verification failed")
                            return NodeResult(status="blocked", error="Verification failed")
                        if output.role == "verifier":
                            covered.update(output.implementation_refs)
                    if not implementations <= covered:
                        journal.set_node(node_id, "blocked", error="Missing verification coverage")
                        return NodeResult(status="blocked", error="Missing verification coverage")
                attempt = journal.claim_node(node_id, canonical_hash({key: value.model_dump() for key, value in inputs.items()}))
                context = NodeContext(node, self.root, run_id, attempt, inputs, journal, artifacts, guard)
                try:
                    output = await asyncio.wait_for(runner.run(node, context), timeout=node.timeout)
                    guard()
                    workspace = {path: hashlib.sha256((self.root / path).read_bytes()).hexdigest()
                                 for path in context.writes}
                    journal.set_node(node_id, "completed", output=output, workspace=workspace)
                    return NodeResult(status="completed", output=output)
                except asyncio.CancelledError:
                    status = "ambiguous" if node.writable else "cancelled"
                    journal.set_node(node_id, status, error="Node cancelled")
                    raise
                except Exception as exc:
                    status = "ambiguous" if node.writable or type(exc).__name__ == "AmbiguousStreamError" else "failed"
                    journal.set_node(node_id, status, error=f"{type(exc).__name__}: {exc}")
                    return NodeResult(status=status, error=f"{type(exc).__name__}: {exc}")

            while True:
                guard()
                pending = [key for key in order if result_nodes[key].status == "pending" and key not in tasks.values()]
                for key in pending:
                    deps = [result_nodes[dep].status for dep in nodes[key].depends_on]
                    if any(s in {"failed", "ambiguous", "blocked", "cancelled"} for s in deps):
                        result_nodes[key] = NodeResult(status="blocked", error="Dependency did not complete")
                        journal.set_node(key, "blocked", error="Dependency did not complete")
                        continue
                    if not all(s == "completed" for s in deps):
                        continue
                    if len(tasks) >= graph.max_concurrency:
                        break
                    if any(conflicts(nodes[key], nodes[running], self.root) for running in tasks.values()):
                        continue
                    task = asyncio.create_task(execute(key), name=f"dag:{run_id}:{key}")
                    tasks[task] = key
                if not tasks:
                    break
                completed, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in completed:
                    key = tasks.pop(task)
                    result_nodes[key] = await task
            statuses = {result.status for result in result_nodes.values()}
            verification_failed = any(result.output is not None and result.output.role == "verifier"
                                      and result.output.verdict == "fail" for result in result_nodes.values())
            status = "success" if statuses == {"completed"} and not verification_failed else "ambiguous" if "ambiguous" in statuses else "failed"
            journal.finish(status)
            active = False
            return DAGRunResult(run_id=run_id, status=status, nodes=result_nodes)
        finally:
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            if active:
                journal.finish("interrupted")
            journal.close()
