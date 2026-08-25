"""Headless CLI composition for executing a reviewed typed TaskGraph."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from pydantic import ValidationError

from mewcode.execution import ExecutionContext
from mewcode.orchestration.artifact_store import ArtifactStore
from mewcode.orchestration.events import (
    ProgressCallback,
    combine_progress_callbacks,
    jsonl_progress_callback,
)
from mewcode.orchestration.graph import TaskGraph
from mewcode.orchestration.legacy_adapter import (
    LegacyAgentDAGAdapter,
    LegacyAgentFactoryAdapter,
)
from mewcode.orchestration.models import AgentRole, RunStatus
from mewcode.orchestration.models import scope_contains
from mewcode.orchestration.persistence import (
    DAGPersistenceError,
    DAGResumePlan,
    DAGRunStore,
    build_resume_plan,
    capability_profile_for,
    dag_recovery_preflight,
    new_dag_run_id,
)
from mewcode.orchestration.validation import (
    DiagnosticSeverity,
    resolve_schedule_budget,
    validate_task_graph,
)
from mewcode.orchestration.workspace import WorkspaceChangeTracker


def _argument_paths(descriptor: Any, arguments: Mapping[str, Any]) -> tuple[str, ...]:
    paths: list[str] = []
    for field in descriptor.path_fields:
        value = arguments.get(field)
        if isinstance(value, (str, os.PathLike)):
            paths.append(os.fspath(value))
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            paths.extend(
                os.fspath(item)
                for item in value
                if isinstance(item, (str, os.PathLike))
            )
    return tuple(paths)


def _relative_workspace_path(root: Path, raw: str) -> str | None:
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        relative = candidate.resolve(strict=False).relative_to(root).as_posix()
        return os.path.normcase(relative).replace("\\", "/")
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class DAGExecutionContext(ExecutionContext):
    """ExecutionContext adapter that honors typed ``/**`` write scopes.

    The base gateway manifest deliberately accepts exact paths.  A TaskGraph
    additionally supports explicit subtree scopes for conflict scheduling, so
    this adapter widens only those named subtrees and delegates every other
    command/network constraint to the production ExecutionContext.
    """

    def constraint_error(
        self, descriptor: Any, arguments: Mapping[str, Any]
    ) -> tuple[str, str] | None:
        # Invoke the actual base from this class' MRO.  Besides being clearer
        # for embedders that reload modules in tests/plugins, this avoids
        # coupling an existing subclass to a subsequently re-imported class
        # object with the same qualified name.
        error = super(DAGExecutionContext, self).constraint_error(
            descriptor, arguments
        )
        if not error or error[0] != "manifest.write_set_violation":
            return error
        scopes = tuple(self.write_set or ())
        if not any(scope.endswith("/**") for scope in scopes):
            return error
        root = Path(self.workspace_root or self.cwd).expanduser().resolve(strict=False)
        targets = _argument_paths(descriptor, arguments)
        if not targets:
            return error
        relative_targets = tuple(_relative_workspace_path(root, raw) for raw in targets)
        if any(target is None for target in relative_targets):
            return error
        if not all(
            any(scope_contains(scope, target) for scope in scopes)
            for target in relative_targets
            if target is not None
        ):
            return error
        # Re-run the remaining host-owned constraints with only the already
        # validated write constraint disabled.
        remainder = ExecutionContext(
            task_id=self.task_id,
            cwd=self.cwd,
            plan_hash=self.plan_hash,
            expected_pre_state_hash=self.expected_pre_state_hash,
            workspace_root=self.workspace_root,
            write_set=None,
            commands=self.commands,
            network_hosts=self.network_hosts,
        )
        return remainder.constraint_error(descriptor, arguments)


class DAGConfigError(ValueError):
    pass


def load_task_graph(path: str | os.PathLike[str]) -> TaskGraph:
    source = Path(path).expanduser().resolve(strict=True)
    try:
        payload = source.read_text(encoding="utf-8")
        return TaskGraph.model_validate_json(payload)
    except (OSError, ValidationError, ValueError) as exc:
        raise DAGConfigError(f"Invalid TaskGraph {source}: {exc}") from exc


async def run_dag_file(
    config: Any,
    permission_mode: Any,
    hook_engine: Any,
    graph_path: str | os.PathLike[str],
    *,
    max_concurrency: int = 4,
    total_tokens: int | None = None,
    wall_time_seconds: float | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    progress_callback: ProgressCallback | None = None,
    progress_jsonl: TextIO | None = None,
    control_root: str | os.PathLike[str] | None = None,
    run_id: str | None = None,
    allow_recovery: bool = False,
    runtime_builder_class: type[Any] | None = None,
    agent_factory_adapter_class: type[Any] | None = None,
) -> int:
    """Run a reviewed graph and convert every failure into a process code."""

    errors = stderr or sys.stderr
    run_identifier = run_id or new_dag_run_id()
    try:
        return await _run_dag_file_impl(
            config,
            permission_mode,
            hook_engine,
            graph_path,
            max_concurrency=max_concurrency,
            total_tokens=total_tokens,
            wall_time_seconds=wall_time_seconds,
            stdout=stdout,
            stderr=stderr,
            progress_callback=progress_callback,
            progress_jsonl=progress_jsonl,
            control_root=control_root,
            run_id=run_identifier,
            allow_recovery=allow_recovery,
            runtime_builder_class=runtime_builder_class,
            agent_factory_adapter_class=agent_factory_adapter_class,
        )
    except DAGPersistenceError as exc:
        print(f"DAG persistence error: {exc}", file=errors)
        return 2
    except Exception as exc:
        DAGRunStore(os.getcwd(), control_root=control_root).mark_interrupted(
            run_identifier, f"{type(exc).__name__}: {exc}"
        )
        print(f"DAG execution error: {type(exc).__name__}: {exc}", file=errors)
        return 1


async def resume_dag_file(
    config: Any,
    permission_mode: Any,
    hook_engine: Any,
    graph_path: str | os.PathLike[str],
    run_id: str,
    *,
    max_concurrency: int | None = None,
    total_tokens: int | None = None,
    wall_time_seconds: float | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    progress_callback: ProgressCallback | None = None,
    progress_jsonl: TextIO | None = None,
    control_root: str | os.PathLike[str] | None = None,
    allow_recovery: bool = False,
    runtime_builder_class: type[Any] | None = None,
    agent_factory_adapter_class: type[Any] | None = None,
) -> int:
    """Resume only nodes proven safe by host-owned evidence and boundaries."""

    output = stdout or sys.stdout
    errors = stderr or sys.stderr
    try:
        graph = load_task_graph(graph_path)
        store = DAGRunStore(os.getcwd(), control_root=control_root)
        record = store.load(run_id)
        provider = config.providers[0]
        with store.resume_guard(run_id):
            resume_plan = build_resume_plan(
                run_id,
                graph,
                workspace=os.getcwd(),
                permission_mode=permission_mode,
                provider=provider,
                control_root=control_root,
            )
            return await _run_dag_file_impl(
                config,
                permission_mode,
                hook_engine,
                graph_path,
                max_concurrency=(
                    record.max_concurrency if max_concurrency is None else max_concurrency
                ),
                total_tokens=(
                    record.budget.total_tokens if total_tokens is None else total_tokens
                ),
                wall_time_seconds=(
                    record.budget.wall_time_seconds
                    if wall_time_seconds is None
                    else wall_time_seconds
                ),
                stdout=stdout,
                stderr=stderr,
                progress_callback=progress_callback,
                progress_jsonl=progress_jsonl,
                control_root=control_root,
                run_id=run_id,
                allow_recovery=allow_recovery,
                resume_plan=resume_plan,
                runtime_builder_class=runtime_builder_class,
                agent_factory_adapter_class=agent_factory_adapter_class,
            )
    except (DAGPersistenceError, DAGConfigError) as exc:
        print(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "kind": "dag_resume",
                    "status": "blocked",
                    "error_code": "resume_blocked",
                    "run_id": run_id,
                    "reason": str(exc),
                    "automatic_replay": False,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=output,
        )
        return 3
    except Exception as exc:
        DAGRunStore(os.getcwd(), control_root=control_root).mark_interrupted(
            run_id, f"{type(exc).__name__}: {exc}"
        )
        print(f"DAG resume error: {type(exc).__name__}: {exc}", file=errors)
        return 1


async def _run_dag_file_impl(
    config: Any,
    permission_mode: Any,
    hook_engine: Any,
    graph_path: str | os.PathLike[str],
    *,
    max_concurrency: int = 4,
    total_tokens: int | None = None,
    wall_time_seconds: float | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    progress_callback: ProgressCallback | None = None,
    progress_jsonl: TextIO | None = None,
    control_root: str | os.PathLike[str] | None = None,
    run_id: str | None = None,
    allow_recovery: bool = False,
    resume_plan: DAGResumePlan | None = None,
    runtime_builder_class: type[Any] | None = None,
    agent_factory_adapter_class: type[Any] | None = None,
) -> int:
    """Compose the existing runtime and Agent factory without launching TUI."""

    from mewcode.agents.loader import AgentLoader
    from mewcode.client import aclose_client, create_client, resolve_context_window
    from mewcode.memory.instructions import load_instructions
    from mewcode.permissions import (
        DangerousCommandDetector,
        PathSandbox,
        PermissionChecker,
        RuleEngine,
    )
    from mewcode.runtime import RuntimeBuilder, resolve_control_root
    from mewcode.tools import create_default_registry
    from mewcode.tools.impl.tool_search import ToolSearchTool

    output = stdout or sys.stdout
    errors = stderr or sys.stderr
    try:
        graph = load_task_graph(graph_path)
    except (DAGConfigError, OSError) as exc:
        print(f"DAG config error: {exc}", file=errors)
        return 2
    try:
        budget = resolve_schedule_budget(
            graph,
            total_tokens=total_tokens,
            wall_time_seconds=wall_time_seconds,
        )
    except ValidationError as exc:
        print(f"DAG config error: invalid schedule budget: {exc}", file=errors)
        return 2
    validation = validate_task_graph(
        graph,
        budget=budget,
        max_concurrency=max_concurrency,
        source=str(Path(graph_path).expanduser().resolve(strict=False)),
    )
    blocking_diagnostics = [
        diagnostic
        for diagnostic in validation.diagnostics
        if diagnostic.severity is DiagnosticSeverity.ERROR
    ]
    if blocking_diagnostics:
        print(
            "DAG config error: "
            + "; ".join(
                f"{diagnostic.path} [{diagnostic.code}] {diagnostic.message}"
                + (
                    f": {diagnostic.node_id}"
                    if diagnostic.node_id is not None
                    else ""
                )
                for diagnostic in blocking_diagnostics
            ),
            file=errors,
        )
        return 2
    work_dir = os.getcwd()
    provider = config.providers[0]
    if not allow_recovery:
        recovery_block = dag_recovery_preflight(
            workspace=work_dir, control_root=control_root
        )
        if recovery_block is not None:
            print(
                json.dumps(
                    recovery_block.machine_readable(),
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                file=output,
            )
            return 3
    run_identifier = run_id or new_dag_run_id()
    run_store = DAGRunStore(work_dir, control_root=control_root)
    if resume_plan is None:
        run_store.create(
            run_id=run_identifier,
            graph=graph,
            capability_profile=capability_profile_for(
                graph, permission_mode, provider
            ),
            budget=budget,
            max_concurrency=max_concurrency,
        )
    else:
        if resume_plan.record.run_id != run_identifier:
            raise DAGPersistenceError("resume plan run id mismatch")
        run_store.begin_resume(resume_plan)
    # All durable graph/capability/recovery checks above are offline. Provider
    # discovery is intentionally after the preflight boundary.
    await resolve_context_window(provider)
    home = Path.home()
    builder_type = runtime_builder_class or RuntimeBuilder
    instructions = load_instructions(work_dir)
    loader = AgentLoader(work_dir, enable_verification=True)
    loader.load_all()
    components: list[Any] = []
    permission_checkers: dict[str, Any] = {}
    task_runtimes: dict[str, Any] = {}
    resolved_control_root = resolve_control_root(control_root)
    artifact_store = ArtifactStore(run_store.artifact_root)
    workspace_tracker = WorkspaceChangeTracker(work_dir, artifact_store)

    def permission_factory(role: AgentRole, envelope: Any) -> Any:
        if envelope.dispatch_id not in permission_checkers:
            permission_checkers[envelope.dispatch_id] = PermissionChecker(
                detector=DangerousCommandDetector(),
                sandbox=PathSandbox(work_dir),
                rule_engine=RuleEngine(
                    user_rules_path=home / ".mewcode" / "permissions.yaml",
                    project_rules_path=Path(work_dir) / ".mewcode" / "permissions.yaml",
                    local_rules_path=Path(work_dir) / ".mewcode" / "permissions.local.yaml",
                ),
                mode=permission_mode,
            )
        return permission_checkers[envelope.dispatch_id]

    def registry_factory(role: AgentRole, envelope: Any) -> Any:
        registry = create_default_registry()
        registry.register(ToolSearchTool(registry, protocol=provider.protocol))
        return registry

    def runtime_factory(role: AgentRole, envelope: Any) -> Any:
        checker = permission_factory(role, envelope)
        component = builder_type(
            work_dir,
            control_root=resolved_control_root,
            permission_checker=checker,
            protection_mode="policy_only",
        ).build(task_id=f"{envelope.run_id}-{envelope.node.node_id}")
        read_only = envelope.capability.read_only
        scoped_context = DAGExecutionContext(
            task_id=component.task.task_id,
            cwd=work_dir,
            plan_hash=(
                f"dag:{envelope.run_id}:{envelope.lease.lease_id}:"
                f"generation-{envelope.lease.generation}"
            ),
            workspace_root=work_dir,
            write_set=(
                ()
                if read_only
                else tuple(envelope.capability.allowed_write_set)
            ),
            commands=() if read_only else None,
            network_hosts=() if read_only else None,
        )
        component.execution_context = scoped_context
        component.gateway.execution_context = scoped_context
        components.append(component)
        task_runtimes[envelope.dispatch_id] = component.task
        return component

    def agent_kwargs_factory(
        role: AgentRole, envelope: Any, component: Any | None
    ) -> Mapping[str, Any]:
        if component is None:
            return {}
        values: dict[str, Any] = {
            "execution_context": component.execution_context,
        }
        if hasattr(component, "evolution"):
            values["evolution_adapter"] = component.evolution
        return values

    factory_type = agent_factory_adapter_class or LegacyAgentFactoryAdapter
    factory = factory_type(
        client_factory=lambda role, envelope: create_client(provider),
        registry_factory=registry_factory,
        protocol=provider.protocol,
        work_dir=work_dir,
        agent_loader=loader,
        permission_checker_factory=permission_factory,
        runtime_factory=runtime_factory,
        context_window=provider.get_context_window(),
        base_instructions=instructions,
        hook_engine=hook_engine,
        agent_kwargs_factory=agent_kwargs_factory,
    )
    adapter = LegacyAgentDAGAdapter(
        factory,
        # This CLI creates one fresh client per node.  Ownership is explicit;
        # custom adapter users keep their injected clients unless they pass a
        # closer themselves.
        agent_closer=lambda agent: aclose_client(
            getattr(agent, "_dag_owned_client", None)
        ),
        artifact_store=artifact_store,
        workspace_tracker=workspace_tracker,
        task_runtime_resolver=lambda envelope: task_runtimes.get(envelope.dispatch_id),
    )
    combined_progress = combine_progress_callbacks(
        (
            run_store.record_progress,
            progress_callback,
            jsonl_progress_callback(progress_jsonl)
            if progress_jsonl is not None
            else None,
        )
    )
    try:
        report = await adapter.run(
            graph,
            budget=budget,
            max_concurrency=max_concurrency,
            run_id=run_identifier,
            progress_callback=combined_progress,
            initial_reports=(
                None if resume_plan is None else resume_plan.initial_reports
            ),
            initial_artifacts=(
                None if resume_plan is None else resume_plan.initial_artifacts
            ),
            initial_accepted_changes=(
                ()
                if resume_plan is None
                else resume_plan.initial_accepted_changes
            ),
            lease_generations=(
                None if resume_plan is None else resume_plan.lease_generations
            ),
        )
        run_store.finalize(report)
        payload = report.machine_readable()
        if hook_engine is not None and hasattr(hook_engine, "shutdown"):
            try:
                await hook_engine.shutdown(timeout=1.0)
            except TypeError:
                await hook_engine.shutdown()
            from mewcode.runtime.data_manager import redact_text

            hook_events = []
            if hasattr(hook_engine, "drain_notifications"):
                for notification in hook_engine.drain_notifications():
                    hook_events.append(
                        {
                            "hook_id": notification.hook_id,
                            "event": notification.event,
                            "status": (
                                notification.status.value
                                if notification.status is not None
                                else (
                                    "succeeded"
                                    if notification.success
                                    else "failed"
                                )
                            ),
                            "elapsed_ms": notification.elapsed_ms,
                            "error_code": notification.error_code,
                            "truncated": notification.truncated,
                            "output": redact_text(notification.output),
                        }
                    )
            payload["hook_events"] = hook_events
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True), file=output)
        return 0 if report.status is RunStatus.SUCCEEDED else 1
    finally:
        for component in reversed(components):
            try:
                component.close()
            except Exception as exc:
                print(f"DAG runtime close warning: {exc}", file=errors)
        if hook_engine is not None and hasattr(hook_engine, "shutdown"):
            await hook_engine.shutdown()


__all__ = [
    "DAGConfigError",
    "DAGExecutionContext",
    "load_task_graph",
    "resume_dag_file",
    "run_dag_file",
]
