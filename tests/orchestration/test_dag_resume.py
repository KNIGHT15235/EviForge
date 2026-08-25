from __future__ import annotations

import hashlib
import io
import json
import sys
from types import SimpleNamespace

import pytest

import mewcode.orchestration.cli as orchestration_cli
from mewcode.config import ProviderConfig
from mewcode.orchestration import (
    AcceptanceCriterion,
    AcceptanceReceipt,
    AcceptanceStatus,
    AgentRole,
    ArtifactStore,
    DAGBudgetSnapshot,
    DAGProgressEvent,
    DAGProgressEventType,
    DAGResumeAction,
    DAGRunStore,
    DAGScheduler,
    NodeExecutionResult,
    NodeStatus,
    ScheduleBudget,
    TaskGraph,
    TaskNode,
    dag_run_status,
    resume_dag_file,
    run_dag_file,
)
from mewcode.recovery import EffectKind, RecoveryStore
from mewcode.runtime import ControlPlanePaths, workspace_id_for


def _config():
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


def _criterion() -> AcceptanceCriterion:
    return AcceptanceCriterion(
        criterion_id="host-pass",
        description="deterministic smoke",
        verifier_argv=(sys.executable, "-c", "raise SystemExit(0)"),
    )


def _receipt(node: TaskNode) -> tuple[AcceptanceReceipt, ...]:
    criterion = node.acceptance_criteria[0]
    digest = hashlib.sha256(
        json.dumps(
            list(criterion.verifier_argv),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return (
        AcceptanceReceipt(
            criterion_id=criterion.criterion_id,
            blocking=True,
            status=AcceptanceStatus.PASS,
            verifier_argv_sha256=digest,
            verifier_cwd=criterion.verifier_cwd,
            exit_code=0,
        ),
    )


def _write_graph(tmp_path, graph: TaskGraph):
    path = tmp_path / "graph.json"
    path.write_text(graph.model_dump_json(indent=2), encoding="utf-8")
    return path


class _UnusedFactory:
    def __init__(self, **kwargs):
        self.work_dir = kwargs["work_dir"]


class _SchedulerAdapter:
    fail_nodes: set[str] = set()
    calls: list[str] = []

    def __init__(self, factory, **kwargs):
        self.artifacts: ArtifactStore = kwargs["artifact_store"]

    async def run(
        self,
        graph,
        *,
        budget,
        max_concurrency=4,
        run_id=None,
        progress_callback=None,
        initial_reports=None,
        initial_artifacts=None,
        initial_accepted_changes=(),
        lease_generations=None,
    ):
        async def execute(envelope):
            node = envelope.node
            self.calls.append(node.node_id)
            if node.node_id in self.fail_nodes:
                return NodeExecutionResult(
                    success=False,
                    tokens_used=2,
                    error="injected failure",
                )
            reference = self.artifacts.put_text(f"{node.node_id}.result", "ok")
            return NodeExecutionResult(
                tokens_used=2,
                artifacts=(reference,),
                acceptance_receipts=_receipt(node),
            )

        return await DAGScheduler(
            graph,
            execute,
            budget=budget,
            max_concurrency=max_concurrency,
            run_id=run_id,
            progress_callback=progress_callback,
            initial_reports=initial_reports,
            initial_artifacts=initial_artifacts,
            initial_accepted_changes=initial_accepted_changes,
            lease_generations=lease_generations,
        ).run()


@pytest.fixture
def offline_dag(monkeypatch):
    async def resolved(provider):
        return provider.context_window

    monkeypatch.setattr("mewcode.client.resolve_context_window", resolved)
    monkeypatch.setattr(orchestration_cli, "LegacyAgentDAGAdapter", _SchedulerAdapter)
    _SchedulerAdapter.fail_nodes = set()
    _SchedulerAdapter.calls = []


@pytest.mark.asyncio
async def test_resume_skips_verified_success_and_retries_read_only_failure(
    tmp_path, monkeypatch, offline_dag
):
    monkeypatch.chdir(tmp_path)
    control = tmp_path / "control"
    graph = TaskGraph(
        nodes=(
            TaskNode(
                node_id="explore",
                role=AgentRole.EXPLORER,
                objective="inventory",
                token_budget=10,
                acceptance_criteria=(_criterion(),),
            ),
            TaskNode(
                node_id="verify",
                role=AgentRole.VERIFIER,
                objective="verify",
                depends_on=("explore",),
                token_budget=10,
                acceptance_criteria=(_criterion(),),
            ),
        )
    )
    graph_path = _write_graph(tmp_path, graph)
    _SchedulerAdapter.fail_nodes = {"verify"}
    first_out = io.StringIO()
    code = await run_dag_file(
        _config(),
        "default",
        None,
        graph_path,
        control_root=control,
        stdout=first_out,
        stderr=io.StringIO(),
        agent_factory_adapter_class=_UnusedFactory,
    )
    first = json.loads(first_out.getvalue())
    assert code == 1
    assert _SchedulerAdapter.calls == ["explore", "verify"]
    run_id = first["run_id"]
    persisted = DAGRunStore(tmp_path, control_root=control).load(run_id)
    assert persisted.graph_hash
    assert persisted.metrics is not None
    assert persisted.metrics.total_tokens_used == 4
    assert persisted.nodes["explore"].report is not None
    assert persisted.nodes["explore"].report.artifact_refs
    before = dag_run_status(run_id, workspace=tmp_path, control_root=control)
    assert before.nodes["explore"].resume_action is DAGResumeAction.SKIP_VERIFIED
    assert before.nodes["verify"].resume_action is DAGResumeAction.RETRY_SAFE
    assert before.total_tokens_used == 4

    _SchedulerAdapter.fail_nodes = set()
    _SchedulerAdapter.calls = []
    resumed_out = io.StringIO()
    code = await resume_dag_file(
        _config(),
        "default",
        None,
        graph_path,
        run_id,
        control_root=control,
        stdout=resumed_out,
        stderr=io.StringIO(),
        agent_factory_adapter_class=_UnusedFactory,
    )
    resumed = json.loads(resumed_out.getvalue())
    assert code == 0, resumed
    assert _SchedulerAdapter.calls == ["verify"]
    assert resumed["nodes"]["explore"]["tokens_used"] == 2
    assert resumed["nodes"]["verify"]["generation"] == 2
    assert resumed["metrics"]["total_tokens_used"] == 4
    after = dag_run_status(run_id, workspace=tmp_path, control_root=control)
    assert after.attempt == 2
    assert all(
        node.resume_action is DAGResumeAction.SKIP_VERIFIED
        for node in after.nodes.values()
    )


@pytest.mark.asyncio
async def test_resume_rejects_changed_graph_before_executor(
    tmp_path, monkeypatch, offline_dag
):
    monkeypatch.chdir(tmp_path)
    control = tmp_path / "control"
    original = TaskGraph(
        nodes=(
            TaskNode(
                node_id="explore",
                role=AgentRole.EXPLORER,
                objective="original",
                acceptance_criteria=(_criterion(),),
            ),
        )
    )
    path = _write_graph(tmp_path, original)
    output = io.StringIO()
    assert (
        await run_dag_file(
            _config(),
            "default",
            None,
            path,
            control_root=control,
            stdout=output,
            stderr=io.StringIO(),
            agent_factory_adapter_class=_UnusedFactory,
        )
        == 0
    )
    run_id = json.loads(output.getvalue())["run_id"]
    changed = original.model_copy(
        update={
            "nodes": (
                original.nodes[0].model_copy(update={"objective": "changed"}),
            )
        }
    )
    _write_graph(tmp_path, changed)
    _SchedulerAdapter.calls = []
    blocked_out = io.StringIO()
    code = await resume_dag_file(
        _config(),
        "default",
        None,
        path,
        run_id,
        control_root=control,
        stdout=blocked_out,
        stderr=io.StringIO(),
        agent_factory_adapter_class=_UnusedFactory,
    )
    blocked = json.loads(blocked_out.getvalue())
    assert code == 3
    assert blocked["status"] == "blocked"
    assert "graph_hash_mismatch" in blocked["reason"]
    assert _SchedulerAdapter.calls == []


@pytest.mark.asyncio
async def test_resume_rejects_capability_change_and_invalid_write_evidence(
    tmp_path, monkeypatch, offline_dag
):
    monkeypatch.chdir(tmp_path)
    control = tmp_path / "control"
    graph = TaskGraph(
        nodes=(
            TaskNode(
                node_id="implement",
                role=AgentRole.IMPLEMENTER,
                objective="produce reviewed output",
                predicted_write_set=("src/**",),
                acceptance_criteria=(_criterion(),),
            ),
        )
    )
    path = _write_graph(tmp_path, graph)
    output = io.StringIO()
    assert (
        await run_dag_file(
            _config(),
            "default",
            None,
            path,
            control_root=control,
            stdout=output,
            stderr=io.StringIO(),
            agent_factory_adapter_class=_UnusedFactory,
        )
        == 0
    )
    run_id = json.loads(output.getvalue())["run_id"]

    _SchedulerAdapter.calls = []
    capability_out = io.StringIO()
    code = await resume_dag_file(
        _config(),
        "acceptEdits",
        None,
        path,
        run_id,
        control_root=control,
        stdout=capability_out,
        stderr=io.StringIO(),
        agent_factory_adapter_class=_UnusedFactory,
    )
    assert code == 3
    assert "capability_boundary_mismatch" in json.loads(
        capability_out.getvalue()
    )["reason"]
    assert _SchedulerAdapter.calls == []

    store = DAGRunStore(tmp_path, control_root=control)
    record = store.load(run_id)
    artifact = record.nodes["implement"].report.artifact_refs[0]  # type: ignore[union-attr]
    digest = artifact.uri.rsplit("/", 1)[-1]
    (store.artifact_root / "blobs" / "sha256" / digest[:2] / digest).unlink()
    status = dag_run_status(run_id, workspace=tmp_path, control_root=control)
    assert not status.resumable
    assert status.nodes["implement"].resume_action is DAGResumeAction.BLOCKED
    assert status.nodes["implement"].evidence_valid is False


@pytest.mark.asyncio
async def test_writable_failure_is_never_automatically_replayed(
    tmp_path, monkeypatch, offline_dag
):
    monkeypatch.chdir(tmp_path)
    control = tmp_path / "control"
    graph = TaskGraph(
        nodes=(
            TaskNode(
                node_id="implement",
                role=AgentRole.IMPLEMENTER,
                objective="may have partially written",
                predicted_write_set=("src/**",),
                acceptance_criteria=(_criterion(),),
            ),
        )
    )
    path = _write_graph(tmp_path, graph)
    _SchedulerAdapter.fail_nodes = {"implement"}
    output = io.StringIO()
    assert (
        await run_dag_file(
            _config(),
            "default",
            None,
            path,
            control_root=control,
            stdout=output,
            stderr=io.StringIO(),
            agent_factory_adapter_class=_UnusedFactory,
        )
        == 1
    )
    run_id = json.loads(output.getvalue())["run_id"]
    status = dag_run_status(run_id, workspace=tmp_path, control_root=control)
    assert not status.resumable
    assert status.nodes["implement"].resume_action is DAGResumeAction.BLOCKED
    _SchedulerAdapter.calls = []
    blocked_out = io.StringIO()
    code = await resume_dag_file(
        _config(),
        "default",
        None,
        path,
        run_id,
        control_root=control,
        stdout=blocked_out,
        stderr=io.StringIO(),
        agent_factory_adapter_class=_UnusedFactory,
    )
    assert code == 3
    assert json.loads(blocked_out.getvalue())["automatic_replay"] is False
    assert _SchedulerAdapter.calls == []


def test_inflight_node_is_reported_as_uncertain_and_blocked(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    control = tmp_path / "control"
    graph = TaskGraph(
        nodes=(
            TaskNode(
                node_id="implement",
                role=AgentRole.IMPLEMENTER,
                objective="in flight",
                predicted_write_set=("src/**",),
                acceptance_criteria=(_criterion(),),
            ),
        )
    )
    store = DAGRunStore(tmp_path, control_root=control)
    record = store.create(
        run_id="dag_inflight",
        graph=graph,
        capability_profile={"test": True},
        budget=ScheduleBudget(total_tokens=100, wall_time_seconds=60),
        max_concurrency=1,
    )
    # The store is intentionally driven through the same persisted progress
    # event used by the scheduler; no terminal report exists after a crash.
    event = DAGProgressEvent(
        sequence=1,
        type=DAGProgressEventType.NODE_STARTED,
        run_id=record.run_id,
        node_id="implement",
        status=NodeStatus.RUNNING,
        completed_nodes=0,
        total_nodes=1,
        budget=DAGBudgetSnapshot(
            total_tokens=100,
            actual_tokens_used=0,
            reserved_tokens=10,
            available_tokens=90,
            overrun_tokens=0,
        ),
    )
    store.record_progress(event)
    status = dag_run_status(record.run_id, workspace=tmp_path, control_root=control)
    assert status.nodes["implement"].effective_status == "uncertain"
    assert status.nodes["implement"].resume_action is DAGResumeAction.BLOCKED


@pytest.mark.asyncio
async def test_recovery_preflight_blocks_before_provider_and_can_be_explicitly_overridden(
    tmp_path, monkeypatch, offline_dag
):
    monkeypatch.chdir(tmp_path)
    control = tmp_path / "control"
    paths = ControlPlanePaths.build(
        control_root=control, workspace_id=workspace_id_for(tmp_path)
    )
    with RecoveryStore(database_path=paths.database) as recovery:
        recovery.prepare_action(
            task_id="prior-task",
            action_type="external_call",
            idempotency_key="prior-action",
            normalized_args_hash="args-hash",
            cwd=tmp_path,
            plan_hash="plan-hash",
            expected_pre_state_hash="pre-hash",
            effect_kind=EffectKind.EXTERNAL,
        )
    graph = TaskGraph(
        nodes=(
            TaskNode(
                node_id="explore",
                role=AgentRole.EXPLORER,
                objective="must not start by default",
                acceptance_criteria=(_criterion(),),
            ),
        )
    )
    path = _write_graph(tmp_path, graph)
    calls = 0

    async def should_not_resolve(provider):
        nonlocal calls
        calls += 1
        return provider.context_window

    monkeypatch.setattr("mewcode.client.resolve_context_window", should_not_resolve)
    blocked_out = io.StringIO()
    code = await run_dag_file(
        _config(),
        "default",
        None,
        path,
        control_root=control,
        stdout=blocked_out,
        stderr=io.StringIO(),
        agent_factory_adapter_class=_UnusedFactory,
    )
    blocked = json.loads(blocked_out.getvalue())
    assert code == 3
    assert blocked["status"] == "recovery_blocked"
    assert blocked["automatic_replay"] is False
    assert blocked["items"][0]["action_id"]
    assert calls == 0
    assert _SchedulerAdapter.calls == []

    allowed_out = io.StringIO()
    code = await run_dag_file(
        _config(),
        "default",
        None,
        path,
        control_root=control,
        allow_recovery=True,
        stdout=allowed_out,
        stderr=io.StringIO(),
        agent_factory_adapter_class=_UnusedFactory,
    )
    assert code == 0
    assert calls == 1
