from __future__ import annotations

import io
import json
import sys
from dataclasses import dataclass
from types import SimpleNamespace
from typing import ClassVar

import pytest

import mewcode.orchestration.cli as orchestration_cli
from mewcode.config import ProviderConfig
from mewcode.orchestration import (
    AgentRole,
    ArtifactContract,
    LegacyAgentDAGAdapter,
    LegacyAgentFactoryAdapter,
    NodeExecutionResult,
    NodeStatus,
    RunStatus,
    ScheduleBudget,
    TaskEnvelope,
    TaskGraph,
    TaskNode,
)
from mewcode.orchestration.cli import load_task_graph, run_dag_file


def write_graph(tmp_path, graph: TaskGraph):
    path = tmp_path / "graph.json"
    path.write_text(graph.model_dump_json(indent=2), encoding="utf-8")
    return path


def config():
    return SimpleNamespace(
        providers=(
            ProviderConfig(
                name="test",
                protocol="anthropic",
                base_url="https://invalid.local",
                model="test-model",
                api_key="not-used",
                context_window=16_000,
            ),
        )
    )


class FakeRuntime:
    def __init__(self, task_id):
        self.task = SimpleNamespace(task_id=task_id)
        self.gateway = SimpleNamespace(execution_context=None)
        self.execution_context = None
        self.closed = False

    def close(self):
        self.closed = True


class FakeRuntimeBuilder:
    built: list[FakeRuntime] = []

    def __init__(self, work_dir, **kwargs):
        self.work_dir = work_dir
        self.kwargs = kwargs

    def build(self, *, task_id=None):
        value = FakeRuntime(task_id)
        value.task_runtime_checker = self.kwargs["permission_checker"]
        self.built.append(value)
        return value


@dataclass
class FakeAgent:
    instances: ClassVar[list["FakeAgent"]] = []
    kwargs: dict

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.registry = kwargs["registry"]
        self.instances.append(self)

    async def run_to_completion(self, task, conversation=None, event_callback=None):
        assert conversation is None
        if event_callback:
            event_callback(
                {
                    "type": "usage",
                    "usage": {"inputTokens": 1, "outputTokens": 1},
                }
            )
        protocol = task.partition("Required output protocol (authoritative):\n")[2]
        required = [name for name in ("inventory",) if name in protocol]
        if required:
            return json.dumps(
                {"artifacts": [{"name": name, "content": "verified"} for name in required]}
            )
        return "verified"


class FakeClient:
    instances: ClassVar[list["FakeClient"]] = []

    def __init__(self) -> None:
        self.close_calls = 0
        self.instances.append(self)

    async def aclose(self) -> None:
        self.close_calls += 1


def host_acceptance():
    from mewcode.orchestration import AcceptanceCriterion

    return (
        AcceptanceCriterion(
            criterion_id="host-pass",
            description="deterministic smoke",
            verifier_argv=(sys.executable, "-c", "raise SystemExit(0)"),
        ),
    )


class FakeFactoryAdapter(LegacyAgentFactoryAdapter):
    def __init__(self, **kwargs):
        super().__init__(**kwargs, agent_class=FakeAgent)


@pytest.mark.asyncio
async def test_headless_dag_entry_uses_production_factory_and_closes_runtimes(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    FakeClient.instances.clear()
    monkeypatch.setattr("mewcode.client.create_client", lambda provider: FakeClient())
    FakeRuntimeBuilder.built.clear()
    FakeAgent.instances.clear()
    graph = TaskGraph(
        nodes=(
            TaskNode(
                node_id="explore",
                role=AgentRole.EXPLORER,
                objective="inspect",
                token_budget=10,
                acceptance_criteria=host_acceptance(),
                artifact_contract=ArtifactContract(required_outputs=("inventory",)),
            ),
            TaskNode(
                node_id="verify",
                role=AgentRole.VERIFIER,
                objective="verify independently",
                depends_on=("explore",),
                token_budget=10,
                acceptance_criteria=host_acceptance(),
                artifact_contract=ArtifactContract(required_inputs=("inventory",)),
            ),
        )
    )
    stdout, stderr = io.StringIO(), io.StringIO()
    code = await run_dag_file(
        config(),
        "default",
        None,
        write_graph(tmp_path, graph),
        stdout=stdout,
        stderr=stderr,
        runtime_builder_class=FakeRuntimeBuilder,
        agent_factory_adapter_class=FakeFactoryAdapter,
    )
    payload = json.loads(stdout.getvalue())
    assert code == 0
    assert payload["status"] == RunStatus.SUCCEEDED.value
    assert payload["nodes"]["verify"]["status"] == NodeStatus.SUCCEEDED.value
    assert len(FakeRuntimeBuilder.built) == 2
    assert all(runtime.closed for runtime in FakeRuntimeBuilder.built)
    assert len(FakeClient.instances) == 2
    assert all(client.close_calls == 1 for client in FakeClient.instances)
    by_node = {
        runtime.task.task_id.rsplit("-", 1)[-1]: runtime
        for runtime in FakeRuntimeBuilder.built
    }
    verify_context = by_node["verify"].execution_context
    assert verify_context is by_node["verify"].gateway.execution_context
    assert verify_context.write_set == ()
    assert verify_context.commands == ()
    assert verify_context.network_hosts == ()
    assert verify_context.plan_hash.startswith("dag:")
    assert ":generation-1" in verify_context.plan_hash
    verify_agent = next(
        agent
        for agent in FakeAgent.instances
        if agent.kwargs["execution_gateway"] is by_node["verify"].gateway
    )
    assert verify_agent.kwargs["hook_engine"] is None
    assert stderr.getvalue() == ""


@pytest.mark.asyncio
async def test_dag_final_json_contains_redacted_hook_results(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("mewcode.client.create_client", lambda provider: FakeClient())
    FakeRuntimeBuilder.built.clear()

    class ReportingHook:
        def __init__(self) -> None:
            self.notifications = [
                SimpleNamespace(
                    hook_id="audit-hook",
                    event="turn_end",
                    status=None,
                    success=False,
                    elapsed_ms=4,
                    error_code="hook.fixture",
                    truncated=False,
                    output="Authorization: Bearer hook-secret-sentinel",
                )
            ]

        async def shutdown(self, timeout=None) -> None:
            return None

        def drain_notifications(self):
            values, self.notifications = self.notifications, []
            return values

    graph = TaskGraph(
        nodes=(
            TaskNode(
                node_id="inspect",
                role=AgentRole.EXPLORER,
                objective="inspect",
                token_budget=10,
                acceptance_criteria=host_acceptance(),
            ),
        )
    )
    stdout = io.StringIO()
    code = await run_dag_file(
        config(),
        "default",
        ReportingHook(),
        write_graph(tmp_path, graph),
        stdout=stdout,
        stderr=io.StringIO(),
        runtime_builder_class=FakeRuntimeBuilder,
        agent_factory_adapter_class=FakeFactoryAdapter,
    )

    payload = json.loads(stdout.getvalue())
    assert code == 0
    assert payload["hook_events"][0]["error_code"] == "hook.fixture"
    assert "hook-secret-sentinel" not in stdout.getvalue()


@pytest.mark.asyncio
async def test_headless_dag_binds_subtree_write_scope_and_shared_checker(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    FakeRuntimeBuilder.built.clear()
    FakeAgent.instances.clear()
    captured_agents: list[FakeAgent] = []

    class CapturingFactory(FakeFactoryAdapter):
        async def __call__(self, role, envelope):
            agent = await super().__call__(role, envelope)
            captured_agents.append(agent)
            return agent

    graph = TaskGraph(
        nodes=(
            TaskNode(
                node_id="implement",
                role=AgentRole.IMPLEMENTER,
                objective="edit only the typed subtree",
                predicted_write_set=("src/**",),
                token_budget=10,
                acceptance_criteria=host_acceptance(),
            ),
        )
    )
    code = await run_dag_file(
        config(),
        "default",
        object(),
        write_graph(tmp_path, graph),
        stdout=io.StringIO(),
        stderr=io.StringIO(),
        runtime_builder_class=FakeRuntimeBuilder,
        agent_factory_adapter_class=CapturingFactory,
    )
    assert code == 0
    runtime = FakeRuntimeBuilder.built[0]
    context = runtime.execution_context
    assert context.write_set == ("src/**",)
    assert context.commands is None
    assert context.network_hosts is None
    assert runtime.gateway.execution_context is context
    # RuntimeBuilder and Agent receive the exact same node-local policy object.
    assert (
        captured_agents[0].kwargs["permission_checker"]
        is FakeRuntimeBuilder.built[0].task_runtime_checker
    )
    assert captured_agents[0].kwargs["hook_engine"] is not None
    # Shell is deliberately absent until TaskGraph grows an explicit command
    # manifest; otherwise `Bash` could write outside the typed write-set.
    assert captured_agents[0].registry.get("Bash") is None
    assert captured_agents[0].registry.get("WriteFile") is not None

    from mewcode.execution.descriptor import ToolDescriptor
    from mewcode.tools.write_file import WriteFile

    write_descriptor = ToolDescriptor.from_tool(WriteFile())
    assert (
        context.constraint_error(
            write_descriptor,
            {"file_path": "src/nested/new.py", "content": "x"},
        )
        is None
    )
    error = context.constraint_error(
        write_descriptor,
        {"file_path": "tests/outside.py", "content": "x"},
    )
    assert error is not None
    assert error[0] == "manifest.write_set_violation"


@pytest.mark.asyncio
async def test_headless_dag_wires_host_cas_and_workspace_tracker(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    captured: dict[str, object] = {}
    real_adapter = LegacyAgentDAGAdapter

    class CapturingAdapter(real_adapter):
        def __init__(self, factory, **kwargs):
            captured.update(kwargs)
            super().__init__(factory, **kwargs)

    monkeypatch.setattr(orchestration_cli, "LegacyAgentDAGAdapter", CapturingAdapter)
    graph = TaskGraph(
        nodes=(
            TaskNode(
                node_id="implement",
                role=AgentRole.IMPLEMENTER,
                objective="edit declared scope",
                predicted_write_set=("src/**",),
                token_budget=10,
                acceptance_criteria=host_acceptance(),
            ),
        )
    )

    code = await run_dag_file(
        config(),
        "default",
        None,
        write_graph(tmp_path, graph),
        stdout=io.StringIO(),
        stderr=io.StringIO(),
        runtime_builder_class=FakeRuntimeBuilder,
        agent_factory_adapter_class=FakeFactoryAdapter,
    )

    from mewcode.orchestration import ArtifactStore, WorkspaceChangeTracker

    assert code == 0
    assert isinstance(captured["artifact_store"], ArtifactStore)
    tracker = captured["workspace_tracker"]
    assert isinstance(tracker, WorkspaceChangeTracker)
    assert tracker.workspace == tmp_path.resolve()
    assert callable(captured["task_runtime_resolver"])


@pytest.mark.asyncio
async def test_headless_dag_failure_returns_nonzero_and_json_report(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)

    class FailingFactory(FakeFactoryAdapter):
        async def __call__(self, role, envelope):
            agent = await super().__call__(role, envelope)

            async def fail(task, conversation=None, event_callback=None):
                raise RuntimeError("model failure")

            agent.run_to_completion = fail
            return agent

    graph = TaskGraph(
        nodes=(TaskNode(node_id="explore", role=AgentRole.EXPLORER, objective="fail", acceptance_criteria=host_acceptance()),)
    )
    stdout, stderr = io.StringIO(), io.StringIO()
    code = await run_dag_file(
        config(),
        "default",
        None,
        write_graph(tmp_path, graph),
        stdout=stdout,
        stderr=stderr,
        runtime_builder_class=FakeRuntimeBuilder,
        agent_factory_adapter_class=FailingFactory,
    )
    payload = json.loads(stdout.getvalue())
    assert code == 1
    assert payload["status"] == RunStatus.FAILED.value
    assert payload["nodes"]["explore"]["status"] == NodeStatus.FAILED.value


@pytest.mark.asyncio
async def test_headless_dag_can_emit_separate_versioned_progress_jsonl(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    graph = TaskGraph(
        nodes=(
            TaskNode(
                node_id="explore",
                role=AgentRole.EXPLORER,
                objective="emit progress",
                token_budget=10,
                acceptance_criteria=host_acceptance(),
            ),
        )
    )
    stdout, stderr, progress = io.StringIO(), io.StringIO(), io.StringIO()

    code = await run_dag_file(
        config(),
        "default",
        None,
        write_graph(tmp_path, graph),
        stdout=stdout,
        stderr=stderr,
        progress_jsonl=progress,
        runtime_builder_class=FakeRuntimeBuilder,
        agent_factory_adapter_class=FakeFactoryAdapter,
    )

    assert code == 0
    # The final report remains one standalone JSON document; progress uses a
    # caller-selected stream so machine consumers never need to split formats.
    assert json.loads(stdout.getvalue())["status"] == "succeeded"
    events = [json.loads(line) for line in progress.getvalue().splitlines()]
    assert [event["sequence"] for event in events] == list(
        range(1, len(events) + 1)
    )
    assert {event["type"] for event in events} >= {
        "node_started",
        "node_completed",
        "budget",
    }
    assert all(event["schema_version"] == "1.0" for event in events)
    assert stderr.getvalue() == ""


@pytest.mark.asyncio
async def test_headless_dag_bootstrap_failure_returns_nonzero(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    class BrokenRuntimeBuilder:
        def __init__(self, work_dir, **kwargs):
            raise RuntimeError("runtime unavailable")

    graph = TaskGraph(
        nodes=(TaskNode(node_id="explore", role=AgentRole.EXPLORER, objective="x", acceptance_criteria=host_acceptance()),)
    )
    stdout, stderr = io.StringIO(), io.StringIO()
    code = await run_dag_file(
        config(),
        "default",
        None,
        write_graph(tmp_path, graph),
        stdout=stdout,
        stderr=stderr,
        runtime_builder_class=BrokenRuntimeBuilder,
        agent_factory_adapter_class=FakeFactoryAdapter,
    )
    assert code == 1
    payload = json.loads(stdout.getvalue())
    assert payload["nodes"]["explore"]["status"] == NodeStatus.FAILED.value
    assert "runtime unavailable" in payload["nodes"]["explore"]["failure_reason"]


@pytest.mark.asyncio
async def test_headless_dag_strict_schema_error_returns_config_exit(tmp_path):
    graph_path = tmp_path / "bad.json"
    graph_path.write_text(
        json.dumps(
            {
                "nodes": [
                    {
                        "node_id": "x",
                        "role": "explorer",
                        "objective": "x",
                        "unknown": True,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    stdout, stderr = io.StringIO(), io.StringIO()
    code = await run_dag_file(
        config(), "default", None, graph_path, stdout=stdout, stderr=stderr
    )
    assert code == 2
    assert stdout.getvalue() == ""
    assert "Invalid TaskGraph" in stderr.getvalue()


def test_load_task_graph_rejects_verifier_write_set(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text(
        json.dumps(
            {
                "nodes": [
                    {
                        "node_id": "verify",
                        "role": "verifier",
                        "objective": "verify",
                        "predicted_write_set": ["src/**"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="read-only|empty write-set"):
        load_task_graph(path)


@pytest.mark.asyncio
async def test_headless_dag_rejects_empty_acceptance_contract(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    graph = TaskGraph(
        nodes=(TaskNode(node_id="empty", role=AgentRole.EXPLORER, objective="x"),)
    )
    stdout, stderr = io.StringIO(), io.StringIO()
    code = await run_dag_file(
        config(),
        "default",
        None,
        write_graph(tmp_path, graph),
        stdout=stdout,
        stderr=stderr,
        runtime_builder_class=FakeRuntimeBuilder,
        agent_factory_adapter_class=FakeFactoryAdapter,
    )
    assert code == 2
    assert stdout.getvalue() == ""
    assert "require at least one blocking deterministic" in stderr.getvalue()
