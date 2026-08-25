"""Composition helpers for building an EviForge task runtime."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

from mewcode.execution import ExecutionContext, ExecutionGateway, RiskEngine
from mewcode.permissions import PermissionChecker
from mewcode.recovery import RecoveryExecutionCoordinator, RecoveryReport, RecoveryStore

from .kernel import TaskRuntime
from .store import RuntimeStore


def workspace_id_for(path: str | os.PathLike[str]) -> str:
    root = Path(path).expanduser().resolve(strict=False)
    normalized = os.path.normcase(str(root))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]


@dataclass(slots=True)
class RuntimeComponents:
    store: RuntimeStore
    recovery: RecoveryStore
    task: TaskRuntime
    gateway: ExecutionGateway
    evolution: object
    execution_context: ExecutionContext
    recovery_coordinator: RecoveryExecutionCoordinator
    startup_recovery: RecoveryReport

    def close(self) -> None:
        errors: list[BaseException] = []
        try:
            # Session shutdown is the explicit lifecycle hook for any staged
            # draft. Invalid/non-PASS drafts fail closed inside the adapter;
            # callers can inspect the logged error without silently losing a
            # valid completed-task candidate.
            self.evolution.close(flush=True)
        except BaseException as exc:
            errors.append(exc)
        for resource in (self.evolution.registry, self.recovery, self.store):
            try:
                resource.close()
            except BaseException as exc:
                errors.append(exc)
        if errors:
            raise ExceptionGroup("runtime close encountered one or more errors", errors)


class RuntimeBuilder:
    """Create the production composition without hiding dependencies in globals."""

    def __init__(
        self,
        work_dir: str | os.PathLike[str],
        *,
        control_root: str | os.PathLike[str] | None = None,
        permission_checker: PermissionChecker | None = None,
        enforce_workspace_boundary: bool = False,
        protection_mode: str = "policy_only",
    ) -> None:
        if protection_mode not in {"policy_only", "os_isolated"}:
            raise ValueError("protection_mode must be policy_only or os_isolated")
        self.work_dir = Path(work_dir).expanduser().resolve(strict=False)
        self.control_root = control_root
        self.permission_checker = permission_checker
        self.enforce_workspace_boundary = enforce_workspace_boundary
        self.protection_mode = protection_mode

    def build(self, *, task_id: str | None = None) -> RuntimeComponents:
        # Delayed import avoids a package cycle: evolution's trace service
        # depends on RuntimeStore, while the production composition owns both.
        from mewcode.evolution import EvolutionRegistry, ProductionEvolutionAdapter

        store = RuntimeStore(
            control_root=self.control_root,
            workspace_id=workspace_id_for(self.work_dir),
        )
        # Runtime state and recovery journal intentionally share the same
        # authoritative SQLite database.  Each package owns disjoint tables;
        # this avoids an impossible cross-file transaction for approval
        # consumption and action STARTED durability.
        recovery = RecoveryStore(database_path=store.db_path)
        startup_recovery = recovery.scan_recovery()
        task = TaskRuntime.create(
            store,
            task_id=task_id,
            metadata={
                "workspace_root": str(self.work_dir),
                "protection_mode": self.protection_mode,
            },
        )
        execution_context = ExecutionContext.unplanned(
            task_id=task.task_id,
            cwd=self.work_dir,
        )
        recovery_coordinator = RecoveryExecutionCoordinator(recovery)
        gateway = ExecutionGateway(
            permission_checker=self.permission_checker,
            risk_engine=RiskEngine(
                workspace_root=self.work_dir,
                control_plane_roots=(store.paths.control_root,),
                enforce_workspace_boundary=self.enforce_workspace_boundary,
            ),
            trace_hook=task.record_execution_event,
            execution_context=execution_context,
            action_journal=recovery_coordinator,
        )
        evolution_registry = EvolutionRegistry(
            store.paths.control_root / "evolution.db"
        )
        evolution = ProductionEvolutionAdapter(
            evolution_registry,
            store,
            workspace=self.work_dir,
        )
        return RuntimeComponents(
            store=store,
            recovery=recovery,
            task=task,
            gateway=gateway,
            evolution=evolution,
            execution_context=execution_context,
            recovery_coordinator=recovery_coordinator,
            startup_recovery=startup_recovery,
        )
