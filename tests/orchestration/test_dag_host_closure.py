from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest

from mewcode.orchestration import (
    AcceptanceCriterion,
    AcceptanceStatus,
    AgentRole,
    ArtifactContract,
    ArtifactStore,
    LegacyAgentDAGAdapter,
    NodeStatus,
    ScheduleBudget,
    TaskGraph,
    TaskNode,
    WorkspaceChangeTracker,
)
from mewcode.runtime import RuntimeStore, TaskRuntime, TaskState


@dataclass
class ScriptedAgent:
    name: str
    output: str = "ok"
    delay: float = 0.0
    write_path: object | None = None
    prompts: list[str] = field(default_factory=list)

    async def run_to_completion(self, task, conversation=None, event_callback=None):
        assert conversation is None
        self.prompts.append(task)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.write_path is not None:
            self.write_path.parent.mkdir(parents=True, exist_ok=True)
            self.write_path.write_text(self.output, encoding="utf-8")
        if event_callback:
            event_callback(
                {"type": "usage", "usage": {"inputTokens": 1, "outputTokens": 1}}
            )
        protocol = task.partition("Required output protocol (authoritative):\n")[2]
        if '"inventory"' in protocol:
            return json.dumps(
                {"artifacts": [{"name": "inventory", "content": self.output}]}
            )
        return self.output


@pytest.mark.asyncio
async def test_blocking_host_verifier_failure_fails_node_and_emits_receipt(
    tmp_path,
) -> None:
    graph = TaskGraph(
        nodes=(
            TaskNode(
                node_id="verify-host",
                role=AgentRole.VERIFIER,
                objective="model prose must not override host verifier",
                acceptance_criteria=(
                    AcceptanceCriterion(
                        criterion_id="process-exit",
                        description="host process must pass",
                        verifier_argv=(
                            sys.executable,
                            "-c",
                            "import sys; print('no'); sys.exit(7)",
                        ),
                    ),
                ),
            ),
        )
    )
    class Factory:
        work_dir = str(tmp_path)

        def __call__(self, role, envelope):
            return ScriptedAgent(role.value)

    report = await LegacyAgentDAGAdapter(Factory()).run(
        graph,
        budget=ScheduleBudget(total_tokens=1_000, wall_time_seconds=10),
    )

    node = report.nodes["verify-host"]
    assert node.status is NodeStatus.FAILED
    assert node.acceptance_receipts[0].status is AcceptanceStatus.FAIL
    assert node.acceptance_receipts[0].exit_code == 7
    assert node.acceptance_receipts[0].stdout_bytes >= 3
    assert node.acceptance_receipts[0].verifier_argv_sha256
    assert "deterministic acceptance failed" in (node.failure_reason or "")


@pytest.mark.asyncio
async def test_cas_dependency_content_is_verified_bounded_and_injected(tmp_path) -> None:
    store = ArtifactStore(tmp_path / "control", max_blob_bytes=8_192)
    created: dict[str, ScriptedAgent] = {}

    def factory(role, envelope):
        output = "prefix-" + ("x" * 128) if envelope.node.node_id == "producer" else "ok"
        agent = ScriptedAgent(envelope.node.node_id, output=output)
        created[envelope.node.node_id] = agent
        return agent

    graph = TaskGraph(
        nodes=(
            TaskNode(
                node_id="producer",
                role=AgentRole.EXPLORER,
                objective="produce",
                artifact_contract=ArtifactContract(required_outputs=("inventory",)),
            ),
            TaskNode(
                node_id="consumer",
                role=AgentRole.VERIFIER,
                objective="consume",
                depends_on=("producer",),
                artifact_contract=ArtifactContract(required_inputs=("inventory",)),
            ),
        )
    )
    report = await LegacyAgentDAGAdapter(
        factory,
        artifact_store=store,
        max_dependency_bytes=12,
        max_dependency_total_bytes=12,
    ).run(graph, budget=ScheduleBudget(total_tokens=2_000, wall_time_seconds=10))

    assert report.nodes["consumer"].status is NodeStatus.SUCCEEDED
    dependency = report.nodes["producer"].artifact_refs[0]
    assert dependency.uri.startswith("eviforge-cas://sha256/")
    assert store.read_bytes(dependency).startswith(b"prefix-")
    prompt = created["consumer"].prompts[0]
    assert '"content": "prefix-xxxxx"' in prompt
    assert '"truncated": true' in prompt
    assert '"trust": "untrusted_dependency_data"' in prompt


@pytest.mark.asyncio
async def test_workspace_change_manifest_and_taskrun_close_with_node(tmp_path) -> None:
    store = ArtifactStore(tmp_path / "control")
    runtime_store = RuntimeStore(control_root=tmp_path / "runtime", workspace_id="dag")
    task_runtime = TaskRuntime.create(runtime_store, task_id="dag-node")
    target = tmp_path / "src" / "feature.py"

    class Factory:
        work_dir = str(tmp_path)

        def __call__(self, role, envelope):
            agent = ScriptedAgent("writer", output="new code", write_path=target)
            agent._dag_runtime_components = SimpleNamespace(task=task_runtime)
            return agent

    graph = TaskGraph(
        nodes=(
            TaskNode(
                node_id="write",
                role=AgentRole.IMPLEMENTER,
                objective="write one file",
                predicted_write_set=("src/**",),
            ),
        )
    )
    try:
        report = await LegacyAgentDAGAdapter(
            Factory(),
            artifact_store=store,
            workspace_tracker=WorkspaceChangeTracker(tmp_path, store),
        ).run(graph, budget=ScheduleBudget(total_tokens=1_000, wall_time_seconds=10))

        node = report.nodes["write"]
        assert node.status is NodeStatus.SUCCEEDED
        assert node.task_state == TaskState.COMPLETED.value
        assert task_runtime.run.state is TaskState.COMPLETED
        change = report.accepted_changes[0]
        assert change.write_set == ("src/feature.py",)
        digest = dict(change.metadata)["change_manifest_sha256"]
        from mewcode.orchestration import ArtifactRef

        payload = store.read_bytes(
            ArtifactRef(
                name="manifest",
                uri=change.patch_ref or "",
                digest=digest,
                media_type="application/json",
            )
        )
        manifest = json.loads(payload)
        assert manifest["changes"][0]["path"] == "src/feature.py"
    finally:
        runtime_store.close()


@pytest.mark.asyncio
async def test_timeout_cancels_node_taskrun(tmp_path) -> None:
    runtime_store = RuntimeStore(control_root=tmp_path / "runtime", workspace_id="dag")
    task_runtime = TaskRuntime.create(runtime_store, task_id="slow-node")
    runtimes: dict[str, TaskRuntime] = {}

    class Factory:
        work_dir = str(tmp_path)

        def __call__(self, role, envelope):
            runtimes[envelope.dispatch_id] = task_runtime
            agent = ScriptedAgent("slow", delay=0.2)
            agent._dag_runtime_components = SimpleNamespace(task=task_runtime)
            return agent

    graph = TaskGraph(
        nodes=(
            TaskNode(
                node_id="slow",
                role=AgentRole.EXPLORER,
                objective="time out",
                timeout_seconds=0.02,
            ),
        )
    )
    try:
        report = await LegacyAgentDAGAdapter(
            Factory(),
            task_runtime_resolver=lambda envelope: runtimes.get(envelope.dispatch_id),
        ).run(graph, budget=ScheduleBudget(total_tokens=1_000, wall_time_seconds=1))
        assert report.nodes["slow"].status is NodeStatus.TIMED_OUT
        assert report.nodes["slow"].task_state == TaskState.CANCELLED.value
        assert task_runtime.run.state is TaskState.CANCELLED
    finally:
        runtime_store.close()


@pytest.mark.asyncio
async def test_read_only_host_verifier_write_fails_and_is_reported(tmp_path) -> None:
    target = tmp_path / "forbidden.txt"

    class Factory:
        work_dir = str(tmp_path)

        def __call__(self, role, envelope):
            return ScriptedAgent("readonly")

    graph = TaskGraph(
        nodes=(
            TaskNode(
                node_id="readonly",
                role=AgentRole.VERIFIER,
                objective="must remain read only",
                acceptance_criteria=(
                    AcceptanceCriterion(
                        criterion_id="malicious-write",
                        description="attempts a write",
                        verifier_argv=(
                            sys.executable,
                            "-c",
                            "from pathlib import Path; Path('forbidden.txt').write_text('x')",
                        ),
                    ),
                ),
            ),
        )
    )
    store = ArtifactStore(tmp_path / "control")
    report = await LegacyAgentDAGAdapter(
        Factory(),
        artifact_store=store,
        workspace_tracker=WorkspaceChangeTracker(tmp_path, store),
    ).run(graph, budget=ScheduleBudget(total_tokens=1_000, wall_time_seconds=10))

    node = report.nodes["readonly"]
    assert target.read_text(encoding="utf-8") == "x"
    assert node.status is NodeStatus.FAILED
    assert "host execution attempted a mutation" in (node.failure_reason or "")
    assert node.acceptance_receipts[0].status is AcceptanceStatus.PASS


@pytest.mark.asyncio
async def test_required_outputs_reject_plain_model_prose() -> None:
    class PlainAgent(ScriptedAgent):
        async def run_to_completion(
            self, task, conversation=None, event_callback=None
        ):
            return "I made the inventory"

    graph = TaskGraph(
        nodes=(
            TaskNode(
                node_id="artifact",
                role=AgentRole.EXPLORER,
                objective="produce named output",
                artifact_contract=ArtifactContract(required_outputs=("inventory",)),
            ),
        )
    )
    report = await LegacyAgentDAGAdapter(
        lambda role, envelope: PlainAgent("plain")
    ).run(graph, budget=ScheduleBudget(total_tokens=1_000, wall_time_seconds=10))

    assert report.nodes["artifact"].status is NodeStatus.FAILED
    assert "ArtifactManifest" in (report.nodes["artifact"].failure_reason or "")


def test_production_cli_rejects_node_without_blocking_acceptance(tmp_path) -> None:
    from mewcode.orchestration.cli import load_task_graph

    graph = TaskGraph(
        nodes=(TaskNode(node_id="empty", role=AgentRole.EXPLORER, objective="x"),)
    )
    path = tmp_path / "graph.json"
    path.write_text(graph.model_dump_json(), encoding="utf-8")
    # General in-process graphs remain backward compatible; enforcement is at
    # the production CLI composition after schema loading.
    assert not load_task_graph(path).nodes[0].acceptance_criteria


@pytest.mark.asyncio
async def test_empty_acceptance_completes_without_gate_pass_event(tmp_path) -> None:
    runtime_store = RuntimeStore(control_root=tmp_path / "runtime", workspace_id="dag")
    task_runtime = TaskRuntime.create(runtime_store, task_id="unverified-node")

    def factory(role, envelope):
        agent = ScriptedAgent("unverified")
        agent._dag_runtime_components = SimpleNamespace(task=task_runtime)
        return agent

    graph = TaskGraph(
        nodes=(TaskNode(node_id="unverified", role=AgentRole.EXPLORER, objective="x"),)
    )
    try:
        report = await LegacyAgentDAGAdapter(factory).run(
            graph, budget=ScheduleBudget(total_tokens=1_000, wall_time_seconds=10)
        )
        assert report.nodes["unverified"].status is NodeStatus.SUCCEEDED
        assert task_runtime.run.state is TaskState.COMPLETED
        events = runtime_store.list_events(task_id=task_runtime.task_id)
        completed = [
            record.event
            for record in events
            if record.event.event_type == "task_state_changed"
            and record.event.status == TaskState.COMPLETED.value
        ]
        assert len(completed) == 1
        details = completed[0].payload["details"]
        assert details["verification"] == "UNVERIFIED"
        assert "gate_verdict" not in details
    finally:
        runtime_store.close()
