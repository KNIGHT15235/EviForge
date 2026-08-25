"""Async, budget-aware scheduler for typed multi-agent task DAGs."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone

from .events import (
    DAGBudgetSnapshot,
    DAGProgressEvent,
    DAGProgressEventType,
    ProgressCallback,
)
from .graph import TaskGraph
from .models import (
    AgentRole,
    ArtifactRef,
    ChangeEnvelope,
    ContextPolicy,
    ExecutionLease,
    NodeExecutionResult,
    NodeReport,
    NodeStatus,
    RoleCapability,
    RunStatus,
    ScheduleBudget,
    ScheduleReport,
    SchedulerMetrics,
    TaskEnvelope,
    TaskNode,
    scope_contains,
    write_scopes_conflict,
)


NodeExecutor = Callable[[TaskEnvelope], Awaitable[NodeExecutionResult]]
NodeAbortHook = Callable[
    [TaskEnvelope, NodeStatus, str], Awaitable[str | None] | str | None
]


class LeaseRegistry:
    """Issues monotonically increasing leases and rejects stale commits."""

    def __init__(
        self,
        run_id: str,
        *,
        initial_generations: Mapping[str, int] | None = None,
    ) -> None:
        self.run_id = run_id
        self._generations: dict[str, int] = {
            node_id: max(0, int(generation))
            for node_id, generation in (initial_generations or {}).items()
        }
        self._active: dict[str, ExecutionLease] = {}

    def issue(self, node: TaskNode) -> ExecutionLease:
        generation = self._generations.get(node.node_id, 0) + 1
        self._generations[node.node_id] = generation
        lease = ExecutionLease(
            run_id=self.run_id,
            node_id=node.node_id,
            generation=generation,
            predicted_write_set=node.predicted_write_set,
        )
        self._active[node.node_id] = lease
        return lease

    def validate(self, change: ChangeEnvelope) -> tuple[bool, str | None]:
        lease = self._active.get(change.node_id)
        if lease is None:
            return False, "no active lease for change"
        if change.lease_generation != lease.generation:
            return False, "stale lease generation"
        if change.lease_id != lease.lease_id:
            return False, "lease id does not match active generation"
        if any(
            not any(scope_contains(scope, written) for scope in lease.predicted_write_set)
            for written in change.write_set
        ):
            return False, "change exceeds predicted write-set"
        return True, None

    def revoke(self, lease: ExecutionLease) -> None:
        active = self._active.get(lease.node_id)
        if active is not None and active.lease_id == lease.lease_id:
            del self._active[lease.node_id]

    def generation(self, node_id: str) -> int:
        return self._generations.get(node_id, 0)


@dataclass(slots=True)
class _ActiveNode:
    node: TaskNode
    envelope: TaskEnvelope
    started: float
    reserved_tokens: int


@dataclass(slots=True)
class _Completion:
    result: NodeExecutionResult | None
    status: NodeStatus
    reason: str | None = None
    task_state: str | None = None


class DAGScheduler:
    """Run ready nodes concurrently while respecting capabilities and budgets."""

    def __init__(
        self,
        graph: TaskGraph,
        executor: NodeExecutor,
        *,
        budget: ScheduleBudget,
        max_concurrency: int = 4,
        run_id: str | None = None,
        abort_hook: NodeAbortHook | None = None,
        progress_callback: ProgressCallback | None = None,
        initial_reports: Mapping[str, NodeReport] | None = None,
        initial_artifacts: Mapping[str, tuple[ArtifactRef, ...]] | None = None,
        initial_accepted_changes: tuple[ChangeEnvelope, ...] = (),
        lease_generations: Mapping[str, int] | None = None,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be at least one")
        self.graph = graph
        self.executor = executor
        self.budget = budget
        self.max_concurrency = max_concurrency
        self.run_id = run_id or f"dag_{uuid.uuid4().hex}"
        self.abort_hook = abort_hook
        self.progress_callback = progress_callback
        known_nodes = set(graph.by_id)
        unknown_reports = set(initial_reports or {}) - known_nodes
        unknown_artifacts = set(initial_artifacts or {}) - known_nodes
        if unknown_reports or unknown_artifacts:
            raise ValueError("resume state contains nodes outside the TaskGraph")
        for node_id, report in (initial_reports or {}).items():
            if report.node_id != node_id or report.status is not NodeStatus.SUCCEEDED:
                raise ValueError("only matching successful reports may be resumed")
        self.initial_reports = dict(initial_reports or {})
        self.initial_artifacts = {
            node_id: tuple(values)
            for node_id, values in (initial_artifacts or {}).items()
        }
        self.initial_accepted_changes = tuple(initial_accepted_changes)
        self.leases = LeaseRegistry(
            self.run_id, initial_generations=lease_generations
        )
        self._cancel_requested = False
        self._running = False
        self._live_tasks: set[asyncio.Task[_Completion]] = set()
        self._abort_task_states: dict[str, str | None] = {}
        self._abort_lock = asyncio.Lock()

    def cancel(self) -> None:
        """Request cooperative cancellation of running and pending nodes."""

        self._cancel_requested = True
        # ``asyncio.wait`` is otherwise only awakened by executor completion or
        # the global deadline.  Cancelling the live tasks makes a cooperative
        # cancellation request observable immediately.
        for task in tuple(self._live_tasks):
            task.cancel()

    async def _invoke(self, envelope: TaskEnvelope) -> _Completion:
        try:
            result = await asyncio.wait_for(
                self.executor(envelope), timeout=envelope.allocated_time_seconds
            )
            if not isinstance(result, NodeExecutionResult):
                result = NodeExecutionResult.model_validate(result)
            return _Completion(result=result, status=NodeStatus.SUCCEEDED)
        except TimeoutError:
            reason = "node execution timed out"
            task_state = await self._abort(envelope, NodeStatus.TIMED_OUT, reason)
            return _Completion(None, NodeStatus.TIMED_OUT, reason, task_state)
        except asyncio.CancelledError:
            await self._abort(
                envelope, NodeStatus.CANCELLED, "node task was cancelled"
            )
            raise
        except Exception as exc:  # executor failures become typed node failures
            reason = f"{type(exc).__name__}: {exc}"
            task_state = await self._abort(envelope, NodeStatus.FAILED, reason)
            return _Completion(None, NodeStatus.FAILED, reason, task_state)

    async def _abort(
        self, envelope: TaskEnvelope, status: NodeStatus, reason: str
    ) -> str | None:
        if self.abort_hook is None:
            return None
        async with self._abort_lock:
            if envelope.dispatch_id in self._abort_task_states:
                return self._abort_task_states[envelope.dispatch_id]
            value = self.abort_hook(envelope, status, reason)
            if hasattr(value, "__await__"):
                value = await value
            self._abort_task_states[envelope.dispatch_id] = value
            return value

    def _build_envelope(
        self,
        node: TaskNode,
        *,
        artifacts: dict[str, tuple[ArtifactRef, ...]],
        allocated_tokens: int,
        allocated_time: float,
    ) -> TaskEnvelope:
        dependency_artifacts = tuple(
            artifact
            for dependency in node.depends_on
            for artifact in artifacts.get(dependency, ())
        )
        available_names = {artifact.name for artifact in dependency_artifacts}
        missing_inputs = set(node.artifact_contract.required_inputs) - available_names
        if missing_inputs:
            raise ValueError(
                "missing required dependency artifacts: " + ", ".join(sorted(missing_inputs))
            )
        lease = self.leases.issue(node)
        read_only = node.role.read_only
        return TaskEnvelope(
            run_id=self.run_id,
            node=node.model_copy(deep=True),
            lease=lease,
            capability=RoleCapability(
                role=node.role,
                read_only=read_only,
                allowed_write_set=() if read_only else node.predicted_write_set,
            ),
            context_policy=(
                ContextPolicy.FRESH_ISOLATED
                if node.role is AgentRole.VERIFIER
                else ContextPolicy.DEPENDENCY_ARTIFACTS_ONLY
            ),
            dependency_artifacts=tuple(
                artifact.model_copy(deep=True) for artifact in dependency_artifacts
            ),
            allocated_token_budget=allocated_tokens,
            allocated_time_seconds=allocated_time,
        )

    def _validate_result(
        self,
        node: TaskNode,
        envelope: TaskEnvelope,
        result: NodeExecutionResult,
    ) -> tuple[NodeStatus, str | None, ChangeEnvelope | None]:
        if result.tokens_used > envelope.allocated_token_budget:
            return (
                NodeStatus.BUDGET_EXCEEDED,
                "node exceeded its allocated token budget",
                None,
            )
        if not result.success:
            return NodeStatus.FAILED, result.error, None

        output_names = {artifact.name for artifact in result.artifacts}
        missing_outputs = set(node.artifact_contract.required_outputs) - output_names
        if missing_outputs:
            return (
                NodeStatus.FAILED,
                "missing required output artifacts: " + ", ".join(sorted(missing_outputs)),
                None,
            )
        if node.role.read_only and result.change is not None:
            return (
                NodeStatus.POLICY_VIOLATION,
                f"{node.role.value} role attempted a workspace mutation",
                None,
            )
        if result.change is None:
            return NodeStatus.SUCCEEDED, None, None
        valid, reason = self.leases.validate(result.change)
        if not valid:
            return NodeStatus.STALE_CHANGE_REJECTED, reason, None
        return NodeStatus.SUCCEEDED, None, result.change

    async def run(self) -> ScheduleReport:
        if self._running:
            raise RuntimeError("a scheduler instance can only run once")
        self._running = True
        started_wall = datetime.now(timezone.utc)
        started = time.monotonic()
        deadline = started + self.budget.wall_time_seconds
        by_id = self.graph.by_id
        criticality = self.graph.critical_path_weights()
        reports: dict[str, NodeReport] = {
            node.node_id: self.initial_reports.get(
                node.node_id,
                NodeReport(node_id=node.node_id, status=NodeStatus.PENDING),
            ).model_copy(deep=True)
            for node in self.graph.nodes
        }
        artifacts: dict[str, tuple[ArtifactRef, ...]] = {
            node_id: tuple(item.model_copy(deep=True) for item in values)
            for node_id, values in self.initial_artifacts.items()
        }
        accepted_changes: list[ChangeEnvelope] = [
            item.model_copy(deep=True) for item in self.initial_accepted_changes
        ]
        active: dict[asyncio.Task[_Completion], _ActiveNode] = {}
        conflict_pairs: set[tuple[str, str]] = set()
        total_tokens_used = sum(
            report.tokens_used for report in self.initial_reports.values()
        )
        budget_overrun = 0
        reserved_tokens = 0
        peak_parallelism = 0
        progress_sequence = 0

        def terminal_failure(status: NodeStatus) -> bool:
            return status.terminal and not status.successful

        async def emit_progress(
            event_type: DAGProgressEventType,
            *,
            node_id: str | None = None,
            status: NodeStatus | None = None,
            message: str | None = None,
        ) -> None:
            nonlocal progress_sequence
            if self.progress_callback is None:
                return
            progress_sequence += 1
            event = DAGProgressEvent(
                sequence=progress_sequence,
                type=event_type,
                run_id=self.run_id,
                node_id=node_id,
                status=status,
                message=message,
                completed_nodes=sum(
                    report.status.terminal for report in reports.values()
                ),
                total_nodes=len(reports),
                budget=DAGBudgetSnapshot(
                    total_tokens=self.budget.total_tokens,
                    actual_tokens_used=total_tokens_used,
                    reserved_tokens=reserved_tokens,
                    available_tokens=max(
                        0,
                        self.budget.total_tokens
                        - total_tokens_used
                        - reserved_tokens,
                    ),
                    overrun_tokens=budget_overrun,
                ),
            )
            result = self.progress_callback(event)
            if hasattr(result, "__await__"):
                await result

        async def mark_pending(status: NodeStatus, reason: str) -> None:
            for node_id, report in list(reports.items()):
                if report.status is NodeStatus.PENDING:
                    reports[node_id] = report.model_copy(
                        update={"status": status, "failure_reason": reason}
                    )
                    await emit_progress(
                        DAGProgressEventType.NODE_FAILED,
                        node_id=node_id,
                        status=status,
                        message=reason,
                    )

        try:
            await emit_progress(
                DAGProgressEventType.BUDGET,
                message=(
                    "initial budget; tokens are reserved before dispatch and actual "
                    "usage is reconciled after node completion"
                ),
            )
            while any(not report.status.terminal for report in reports.values()):
                now = time.monotonic()
                if self._cancel_requested:
                    for task in active:
                        task.cancel()
                    if active:
                        await asyncio.gather(*active, return_exceptions=True)
                    for task, current in active.items():
                        self.leases.revoke(current.envelope.lease)
                        task_state = self._abort_task_states.get(
                            current.envelope.dispatch_id
                        )
                        reports[current.node.node_id] = NodeReport(
                            node_id=current.node.node_id,
                            status=NodeStatus.CANCELLED,
                            generation=current.envelope.lease.generation,
                            runtime_seconds=max(0.0, now - current.started),
                            task_state=task_state,
                            failure_reason="scheduler cancellation requested",
                        )
                        await emit_progress(
                            DAGProgressEventType.NODE_FAILED,
                            node_id=current.node.node_id,
                            status=NodeStatus.CANCELLED,
                            message="scheduler cancellation requested",
                        )
                    active.clear()
                    reserved_tokens = 0
                    await emit_progress(
                        DAGProgressEventType.BUDGET,
                        message="all active reservations released after cancellation",
                    )
                    await mark_pending(
                        NodeStatus.CANCELLED, "scheduler cancellation requested"
                    )
                    break
                if now >= deadline:
                    for task in active:
                        task.cancel()
                    if active:
                        await asyncio.gather(*active, return_exceptions=True)
                    for task, current in active.items():
                        self.leases.revoke(current.envelope.lease)
                        task_state = self._abort_task_states.get(
                            current.envelope.dispatch_id
                        )
                        reports[current.node.node_id] = NodeReport(
                            node_id=current.node.node_id,
                            status=NodeStatus.TIMED_OUT,
                            generation=current.envelope.lease.generation,
                            runtime_seconds=max(0.0, now - current.started),
                            task_state=task_state,
                            failure_reason="global wall-time budget exhausted",
                        )
                        await emit_progress(
                            DAGProgressEventType.NODE_FAILED,
                            node_id=current.node.node_id,
                            status=NodeStatus.TIMED_OUT,
                            message="global wall-time budget exhausted",
                        )
                    active.clear()
                    reserved_tokens = 0
                    await emit_progress(
                        DAGProgressEventType.BUDGET,
                        message="all active reservations released after wall-time expiry",
                    )
                    await mark_pending(
                        NodeStatus.CANCELLED, "global wall-time budget exhausted"
                    )
                    break

                # Propagate dependency failures before considering readiness.
                changed = True
                while changed:
                    changed = False
                    for node in self.graph.nodes:
                        if reports[node.node_id].status is not NodeStatus.PENDING:
                            continue
                        failed_dependencies = [
                            dependency
                            for dependency in node.depends_on
                            if terminal_failure(reports[dependency].status)
                        ]
                        if failed_dependencies:
                            reports[node.node_id] = NodeReport(
                                node_id=node.node_id,
                                status=NodeStatus.SKIPPED_DEPENDENCY_FAILED,
                                failure_reason=(
                                    "failed dependencies: " + ", ".join(failed_dependencies)
                                ),
                            )
                            await emit_progress(
                                DAGProgressEventType.NODE_FAILED,
                                node_id=node.node_id,
                                status=NodeStatus.SKIPPED_DEPENDENCY_FAILED,
                                message=(
                                    "failed dependencies: "
                                    + ", ".join(failed_dependencies)
                                ),
                            )
                            changed = True

                ready = [
                    node
                    for node in self.graph.nodes
                    if reports[node.node_id].status is NodeStatus.PENDING
                    and all(reports[dependency].status.successful for dependency in node.depends_on)
                ]
                ready.sort(key=lambda node: (-criticality[node.node_id], node.node_id))

                dispatched = False
                for node in ready:
                    if len(active) >= self.max_concurrency:
                        break
                    conflicting = [
                        current.node.node_id
                        for current in active.values()
                        if write_scopes_conflict(
                            node.predicted_write_set, current.node.predicted_write_set
                        )
                    ]
                    if conflicting:
                        for other in conflicting:
                            conflict_pairs.add(tuple(sorted((node.node_id, other))))
                        continue
                    available_tokens = (
                        self.budget.total_tokens - total_tokens_used - reserved_tokens
                    )
                    if node.token_budget > available_tokens:
                        # An active reservation may return unused budget; defer until it does.
                        if active and node.token_budget <= (
                            self.budget.total_tokens - total_tokens_used
                        ):
                            continue
                        reports[node.node_id] = NodeReport(
                            node_id=node.node_id,
                            status=NodeStatus.BUDGET_EXCEEDED,
                            failure_reason=(
                                f"requires {node.token_budget} tokens, "
                                f"only {max(0, available_tokens)} remain"
                            ),
                        )
                        await emit_progress(
                            DAGProgressEventType.BUDGET,
                            node_id=node.node_id,
                            status=NodeStatus.BUDGET_EXCEEDED,
                            message=(
                                f"dispatch denied: requires reservation of "
                                f"{node.token_budget} tokens, only "
                                f"{max(0, available_tokens)} available"
                            ),
                        )
                        await emit_progress(
                            DAGProgressEventType.NODE_FAILED,
                            node_id=node.node_id,
                            status=NodeStatus.BUDGET_EXCEEDED,
                            message=reports[node.node_id].failure_reason,
                        )
                        continue
                    remaining_time = deadline - time.monotonic()
                    if remaining_time <= 0:
                        break
                    allocated_time = min(node.timeout_seconds, remaining_time)
                    try:
                        envelope = self._build_envelope(
                            node,
                            artifacts=artifacts,
                            allocated_tokens=node.token_budget,
                            allocated_time=allocated_time,
                        )
                    except ValueError as exc:
                        reports[node.node_id] = NodeReport(
                            node_id=node.node_id,
                            status=NodeStatus.FAILED,
                            failure_reason=str(exc),
                        )
                        await emit_progress(
                            DAGProgressEventType.NODE_FAILED,
                            node_id=node.node_id,
                            status=NodeStatus.FAILED,
                            message=str(exc),
                        )
                        continue
                    task = asyncio.create_task(self._invoke(envelope))
                    active[task] = _ActiveNode(
                        node=node,
                        envelope=envelope,
                        started=time.monotonic(),
                        reserved_tokens=node.token_budget,
                    )
                    self._live_tasks.add(task)
                    reserved_tokens += node.token_budget
                    reports[node.node_id] = NodeReport(
                        node_id=node.node_id,
                        status=NodeStatus.RUNNING,
                        generation=envelope.lease.generation,
                    )
                    peak_parallelism = max(peak_parallelism, len(active))
                    await emit_progress(
                        DAGProgressEventType.NODE_STARTED,
                        node_id=node.node_id,
                        status=NodeStatus.RUNNING,
                        message=(
                            f"reserved {node.token_budget} tokens; actual usage is "
                            "unknown until an executor usage event is reconciled"
                        ),
                    )
                    await emit_progress(
                        DAGProgressEventType.BUDGET,
                        node_id=node.node_id,
                        status=NodeStatus.RUNNING,
                        message="node reservation acquired",
                    )
                    dispatched = True

                if not active:
                    # The propagation/budget updates above may be all the progress needed.
                    if any(not report.status.terminal for report in reports.values()):
                        if not dispatched:
                            # A ready node may just have become terminal because it
                            # could not obtain budget or satisfy its artifact
                            # contract.  Give the next loop a chance to propagate
                            # that typed failure through its descendants before
                            # declaring an actual scheduler deadlock.
                            if any(
                                reports[node.node_id].status is NodeStatus.PENDING
                                and any(
                                    terminal_failure(reports[dependency].status)
                                    for dependency in node.depends_on
                                )
                                for node in self.graph.nodes
                            ):
                                continue
                            await mark_pending(
                                NodeStatus.FAILED,
                                "scheduler could not make progress with ready nodes",
                            )
                    continue

                done, _ = await asyncio.wait(
                    active,
                    timeout=max(0.0, deadline - time.monotonic()),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if not done:
                    continue
                for task in done:
                    current = active.pop(task)
                    self._live_tasks.discard(task)
                    reserved_tokens -= current.reserved_tokens
                    runtime = max(0.0, time.monotonic() - current.started)
                    try:
                        completion = task.result()
                    except asyncio.CancelledError:
                        completion = _Completion(
                            None, NodeStatus.CANCELLED, "node task was cancelled"
                        )
                    result = completion.result
                    status = completion.status
                    reason = completion.reason
                    accepted: ChangeEnvelope | None = None
                    tokens_used = 0
                    result_artifacts: tuple[ArtifactRef, ...] = ()
                    if result is not None:
                        tokens_used = result.tokens_used
                        total_tokens_used += tokens_used
                        budget_overrun += max(
                            0, tokens_used - current.envelope.allocated_token_budget
                        )
                        status, reason, accepted = self._validate_result(
                            current.node, current.envelope, result
                        )
                        if status is NodeStatus.SUCCEEDED:
                            result_artifacts = result.artifacts
                            artifacts[current.node.node_id] = result.artifacts
                            if accepted is not None:
                                accepted_changes.append(accepted)
                    self.leases.revoke(current.envelope.lease)
                    reports[current.node.node_id] = NodeReport(
                        node_id=current.node.node_id,
                        status=status,
                        generation=current.envelope.lease.generation,
                        runtime_seconds=runtime,
                        tokens_used=tokens_used,
                        artifact_refs=result_artifacts,
                        acceptance_receipts=(
                            result.acceptance_receipts if result is not None else ()
                        ),
                        task_state=(
                            result.task_state
                            if result is not None
                            else completion.task_state
                        ),
                        failure_reason=reason,
                    )
                    await emit_progress(
                        (
                            DAGProgressEventType.NODE_COMPLETED
                            if status is NodeStatus.SUCCEEDED
                            else DAGProgressEventType.NODE_FAILED
                        ),
                        node_id=current.node.node_id,
                        status=status,
                        message=reason,
                    )
                    await emit_progress(
                        DAGProgressEventType.BUDGET,
                        node_id=current.node.node_id,
                        status=status,
                        message=(
                            "node usage reconciled after completion"
                            if tokens_used <= current.envelope.allocated_token_budget
                            else (
                                "node usage reconciled with unavoidable in-flight "
                                "overshoot of "
                                f"{tokens_used - current.envelope.allocated_token_budget} "
                                "tokens"
                            )
                        ),
                    )
        finally:
            for task in active:
                task.cancel()
            if active:
                await asyncio.gather(*active, return_exceptions=True)
            self._live_tasks.clear()
            self._running = False

        finished = time.monotonic()
        statuses = {report.status for report in reports.values()}
        if statuses == {NodeStatus.SUCCEEDED}:
            run_status = RunStatus.SUCCEEDED
        elif NodeStatus.CANCELLED in statuses or self._cancel_requested:
            run_status = RunStatus.CANCELLED
        elif NodeStatus.BUDGET_EXCEEDED in statuses:
            run_status = RunStatus.BUDGET_EXCEEDED
        elif NodeStatus.SUCCEEDED in statuses:
            run_status = RunStatus.PARTIAL
        else:
            run_status = RunStatus.FAILED
        runtimes = {
            node_id: report.runtime_seconds for node_id, report in reports.items()
        }
        metrics = SchedulerMetrics(
            makespan_seconds=max(0.0, finished - started),
            node_runtime_seconds=runtimes,
            peak_parallelism=peak_parallelism,
            max_concurrency=self.max_concurrency,
            peak_normalized_utilization=peak_parallelism / self.max_concurrency,
            prevented_conflicts=len(conflict_pairs),
            total_tokens_used=total_tokens_used,
            total_token_budget=self.budget.total_tokens,
            budget_overrun_tokens=budget_overrun,
            critical_path_seconds=self.graph.critical_path_seconds,
        )
        return ScheduleReport(
            run_id=self.run_id,
            status=run_status,
            started_at=started_wall,
            finished_at=datetime.now(timezone.utc),
            nodes=reports,
            accepted_changes=tuple(accepted_changes),
            metrics=metrics,
        )
