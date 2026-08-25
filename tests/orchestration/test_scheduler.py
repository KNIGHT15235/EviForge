from __future__ import annotations

import asyncio

import pytest

from mewcode.orchestration import (
    AgentRole,
    ArtifactContract,
    ArtifactRef,
    ChangeEnvelope,
    ContextPolicy,
    DAGScheduler,
    NodeExecutionResult,
    NodeStatus,
    RunStatus,
    ScheduleBudget,
    TaskEnvelope,
    TaskGraph,
    TaskNode,
)


def node(node_id: str, **changes: object) -> TaskNode:
    values: dict[str, object] = {
        "node_id": node_id,
        "role": AgentRole.EXPLORER,
        "objective": f"run {node_id}",
        "token_budget": 10,
        "timeout_seconds": 1,
        "estimated_duration_seconds": 1,
    }
    values.update(changes)
    return TaskNode(**values)


def scheduler(
    nodes: tuple[TaskNode, ...],
    executor,
    *,
    tokens: int = 100,
    seconds: float = 2,
    concurrency: int = 4,
    progress=None,
) -> DAGScheduler:
    return DAGScheduler(
        TaskGraph(nodes=nodes),
        executor,
        budget=ScheduleBudget(total_tokens=tokens, wall_time_seconds=seconds),
        max_concurrency=concurrency,
        run_id="test-run",
        progress_callback=progress,
    )


@pytest.mark.asyncio
async def test_independent_ready_nodes_run_in_parallel_and_report_metrics() -> None:
    both_running = asyncio.Event()
    active = 0
    peak = 0

    async def execute(envelope: TaskEnvelope) -> NodeExecutionResult:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        if active == 2:
            both_running.set()
        await asyncio.wait_for(both_running.wait(), timeout=.2)
        active -= 1
        return NodeExecutionResult(tokens_used=2)

    run = scheduler((node("a"), node("b")), execute, concurrency=2)
    report = await run.run()

    assert report.status is RunStatus.SUCCEEDED
    assert peak == 2
    assert report.metrics.peak_parallelism == 2
    assert report.metrics.peak_normalized_utilization == 1.0
    assert report.metrics.total_tokens_used == 4
    assert set(report.metrics.node_runtime_seconds) == {"a", "b"}
    assert report.machine_readable()["schema_version"] == "1.0"


@pytest.mark.asyncio
async def test_dependencies_run_after_artifacts_are_available() -> None:
    order: list[str] = []

    async def execute(envelope: TaskEnvelope) -> NodeExecutionResult:
        order.append(envelope.node.node_id)
        if envelope.node.node_id == "discover":
            return NodeExecutionResult(
                tokens_used=1,
                artifacts=(ArtifactRef(name="inventory", uri="mem://inventory"),),
            )
        assert [artifact.name for artifact in envelope.dependency_artifacts] == [
            "inventory"
        ]
        return NodeExecutionResult(tokens_used=1)

    report = await scheduler(
        (
            node(
                "discover",
                artifact_contract=ArtifactContract(required_outputs=("inventory",)),
            ),
            node(
                "implement",
                role=AgentRole.IMPLEMENTER,
                depends_on=("discover",),
                artifact_contract=ArtifactContract(required_inputs=("inventory",)),
                predicted_write_set=("src/**",),
            ),
        ),
        execute,
    ).run()

    assert report.status is RunStatus.SUCCEEDED
    assert order == ["discover", "implement"]


@pytest.mark.asyncio
async def test_overlapping_write_sets_are_serialized_and_counted() -> None:
    active = 0
    peak = 0

    async def execute(envelope: TaskEnvelope) -> NodeExecutionResult:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(.015)
        active -= 1
        return NodeExecutionResult(
            tokens_used=1,
            change=ChangeEnvelope.from_lease(
                envelope.lease, write_set=("src/shared.py",)
            ),
        )

    writable = {
        "role": AgentRole.IMPLEMENTER,
        "predicted_write_set": ("src/**",),
    }
    report = await scheduler(
        (node("a", **writable), node("b", **writable)), execute, concurrency=2
    ).run()

    assert report.status is RunStatus.SUCCEEDED
    assert peak == 1
    assert report.metrics.prevented_conflicts == 1
    assert report.metrics.peak_parallelism == 1
    assert len(report.accepted_changes) == 2


@pytest.mark.asyncio
async def test_critical_path_has_dispatch_priority() -> None:
    dispatch_order: list[str] = []

    async def execute(envelope: TaskEnvelope) -> NodeExecutionResult:
        dispatch_order.append(envelope.node.node_id)
        return NodeExecutionResult(tokens_used=1)

    nodes = (
        node("minor", estimated_duration_seconds=2),
        node("critical", estimated_duration_seconds=1),
        node("critical-tail", depends_on=("critical",), estimated_duration_seconds=5),
    )
    report = await scheduler(nodes, execute, concurrency=1).run()

    assert report.status is RunStatus.SUCCEEDED
    assert dispatch_order == ["critical", "critical-tail", "minor"]
    assert report.metrics.critical_path_seconds == 6


@pytest.mark.asyncio
async def test_total_token_budget_blocks_node_and_propagates_to_dependent() -> None:
    async def execute(envelope: TaskEnvelope) -> NodeExecutionResult:
        return NodeExecutionResult(tokens_used=10)

    report = await scheduler(
        (
            node("a", token_budget=10, estimated_duration_seconds=2),
            node("b", token_budget=10),
            node("b-tail", depends_on=("b",), token_budget=1),
        ),
        execute,
        tokens=15,
        concurrency=1,
    ).run()

    assert report.status is RunStatus.BUDGET_EXCEEDED
    assert report.nodes["a"].status is NodeStatus.SUCCEEDED
    assert report.nodes["b"].status is NodeStatus.BUDGET_EXCEEDED
    assert report.nodes["b-tail"].status is NodeStatus.SKIPPED_DEPENDENCY_FAILED
    assert report.metrics.total_tokens_used == 10


@pytest.mark.asyncio
async def test_node_token_overrun_is_rejected_and_measured() -> None:
    events = []

    async def execute(envelope: TaskEnvelope) -> NodeExecutionResult:
        return NodeExecutionResult(tokens_used=13)

    report = await scheduler(
        (node("a", token_budget=10),),
        execute,
        tokens=20,
        progress=events.append,
    ).run()

    assert report.status is RunStatus.BUDGET_EXCEEDED
    assert report.nodes["a"].status is NodeStatus.BUDGET_EXCEEDED
    assert report.metrics.budget_overrun_tokens == 3
    assert report.metrics.total_tokens_used == 13
    assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    started = next(event for event in events if event.type == "node_started")
    failed = next(event for event in events if event.type == "node_failed")
    reconciled = events[-1]
    assert started.node_id == "a"
    assert started.budget.reserved_tokens == 10
    assert started.budget.actual_tokens_used == 0
    assert failed.status is NodeStatus.BUDGET_EXCEEDED
    assert reconciled.type == "budget"
    assert reconciled.budget.reserved_tokens == 0
    assert reconciled.budget.actual_tokens_used == 13
    assert reconciled.budget.overrun_tokens == 3
    assert reconciled.budget.in_flight_usage_known is False
    assert "unavoidable in-flight overshoot" in (reconciled.message or "")


@pytest.mark.asyncio
async def test_budget_denial_emits_budget_and_failed_events_without_execution() -> None:
    calls = 0
    events = []

    async def execute(envelope: TaskEnvelope) -> NodeExecutionResult:
        nonlocal calls
        calls += 1
        return NodeExecutionResult(tokens_used=1)

    report = await scheduler(
        (node("too-large", token_budget=11),),
        execute,
        tokens=10,
        progress=events.append,
    ).run()

    assert calls == 0
    assert report.nodes["too-large"].status is NodeStatus.BUDGET_EXCEEDED
    assert any(
        event.type == "budget"
        and event.node_id == "too-large"
        and "dispatch denied" in (event.message or "")
        for event in events
    )
    assert any(
        event.type == "node_failed"
        and event.node_id == "too-large"
        and event.status is NodeStatus.BUDGET_EXCEEDED
        for event in events
    )


@pytest.mark.asyncio
async def test_failure_propagates_but_unrelated_branch_completes() -> None:
    async def execute(envelope: TaskEnvelope) -> NodeExecutionResult:
        if envelope.node.node_id == "bad":
            return NodeExecutionResult(success=False, error="deterministic failure")
        return NodeExecutionResult(tokens_used=1)

    report = await scheduler(
        (
            node("bad"),
            node("blocked", depends_on=("bad",)),
            node("independent"),
        ),
        execute,
    ).run()

    assert report.status is RunStatus.PARTIAL
    assert report.nodes["bad"].status is NodeStatus.FAILED
    assert report.nodes["blocked"].status is NodeStatus.SKIPPED_DEPENDENCY_FAILED
    assert report.nodes["independent"].status is NodeStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_timeout_and_external_cancellation_are_typed() -> None:
    started = asyncio.Event()

    async def execute(envelope: TaskEnvelope) -> NodeExecutionResult:
        started.set()
        await asyncio.sleep(10)
        return NodeExecutionResult()

    timed = await scheduler(
        (node("slow", timeout_seconds=.01),), execute, seconds=1
    ).run()
    assert timed.nodes["slow"].status is NodeStatus.TIMED_OUT

    cancellable = scheduler((node("long"), node("after", depends_on=("long",))), execute)
    task = asyncio.create_task(cancellable.run())
    await asyncio.wait_for(started.wait(), timeout=.2)
    cancellable.cancel()
    cancelled = await asyncio.wait_for(task, timeout=.2)
    assert cancelled.status is RunStatus.CANCELLED
    assert cancelled.nodes["long"].status is NodeStatus.CANCELLED
    assert cancelled.nodes["after"].status is NodeStatus.CANCELLED


@pytest.mark.asyncio
async def test_verifier_receives_fresh_read_only_artifact_only_envelope() -> None:
    captured: list[TaskEnvelope] = []

    async def execute(envelope: TaskEnvelope) -> NodeExecutionResult:
        captured.append(envelope)
        if envelope.node.role is AgentRole.IMPLEMENTER:
            return NodeExecutionResult(
                artifacts=(
                    ArtifactRef(
                        name="candidate", uri="cas://patch", digest="sha256:abc"
                    ),
                )
            )
        assert envelope.node.role is AgentRole.VERIFIER
        assert envelope.context_policy is ContextPolicy.FRESH_ISOLATED
        assert envelope.capability.read_only is True
        assert envelope.capability.allowed_write_set == ()
        assert envelope.dependency_artifacts[0].uri == "cas://patch"
        assert "conversation" not in envelope.model_dump()
        return NodeExecutionResult()

    report = await scheduler(
        (
            node(
                "implement",
                role=AgentRole.IMPLEMENTER,
                predicted_write_set=("src/**",),
                artifact_contract=ArtifactContract(required_outputs=("candidate",)),
            ),
            node(
                "verify",
                role=AgentRole.VERIFIER,
                depends_on=("implement",),
                artifact_contract=ArtifactContract(required_inputs=("candidate",)),
            ),
        ),
        execute,
    ).run()

    assert report.status is RunStatus.SUCCEEDED
    assert len(captured) == 2
    assert captured[0].dispatch_id != captured[1].dispatch_id


@pytest.mark.asyncio
async def test_stale_or_out_of_scope_changes_are_rejected() -> None:
    async def stale(envelope: TaskEnvelope) -> NodeExecutionResult:
        return NodeExecutionResult(
            change=ChangeEnvelope(
                node_id=envelope.node.node_id,
                lease_id=envelope.lease.lease_id,
                lease_generation=envelope.lease.generation + 1,
                write_set=("src/a.py",),
            )
        )

    writable = node(
        "writer", role=AgentRole.IMPLEMENTER, predicted_write_set=("src/**",)
    )
    stale_report = await scheduler((writable,), stale).run()
    assert stale_report.nodes["writer"].status is NodeStatus.STALE_CHANGE_REJECTED
    assert stale_report.accepted_changes == ()

    async def outside(envelope: TaskEnvelope) -> NodeExecutionResult:
        return NodeExecutionResult(
            change=ChangeEnvelope.from_lease(
                envelope.lease, write_set=("tests/not-authorized.py",)
            )
        )

    outside_report = await scheduler((writable,), outside).run()
    assert outside_report.nodes["writer"].status is NodeStatus.STALE_CHANGE_REJECTED
