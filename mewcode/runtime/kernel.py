"""Small integration kernel joining FSM, trace, execution, and evidence.

The lower-level packages intentionally stay usable on their own.  ``TaskRuntime``
is the composition root used by the TUI/headless adapters: it owns one durable
``TaskRun``, turns redacted gateway events into authoritative trace records, and
maps deterministic Evidence Gate verdicts onto legal FSM transitions.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from mewcode.execution import ExecutionTraceEvent

from .fsm import TaskRun, TaskState
from .models import TraceEvent
from .store import RuntimeStore


_GATE_TRANSITIONS: Mapping[str, TaskState] = {
    "PASS": TaskState.COMPLETED,
    "PARTIAL": TaskState.PARTIAL,
    "FAIL": TaskState.REPLANNING,
    "BLOCKED": TaskState.NEEDS_HUMAN,
}


class TaskRuntime:
    """Durable runtime facade for one task.

    State transitions use optimistic versions from :class:`RuntimeStore`; a
    stale UI/worker therefore cannot silently overwrite a newer task state.
    Raw tool arguments are never copied into the trace -- only the canonical
    arguments hash and stable reason codes emitted by ``ExecutionGateway``.
    """

    def __init__(self, store: RuntimeStore, task_id: str) -> None:
        self.store = store
        self.task_id = task_id
        if self.store.get_task(task_id) is None:
            raise KeyError(f"unknown TaskRun: {task_id}")

    @classmethod
    def create(
        cls,
        store: RuntimeStore,
        *,
        task_id: str | None = None,
        trace_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> TaskRuntime:
        run = store.create_task(task_id=task_id, trace_id=trace_id, metadata=metadata)
        return cls(store, run.task_id)

    @property
    def run(self) -> TaskRun:
        current = self.store.get_task(self.task_id)
        if current is None:  # pragma: no cover - protects against DB tampering
            raise KeyError(f"unknown TaskRun: {self.task_id}")
        return current

    def transition(
        self,
        state: TaskState,
        *,
        reason: str,
        actor: str = "runtime",
        details: Mapping[str, Any] | None = None,
    ) -> TaskRun:
        current = self.run
        return self.store.transition_task(
            self.task_id,
            state,
            expected_version=current.version,
            reason=reason,
            actor=actor,
            details=details,
        )

    def prepare_contract(self, contract_id: str) -> TaskRun:
        if not contract_id.strip():
            raise ValueError("contract_id must not be blank")
        return self.transition(
            TaskState.CONTRACT_READY,
            reason="requirement_contract_attached",
            details={"contract_id": contract_id},
        )

    def begin_planning(self) -> TaskRun:
        return self.transition(TaskState.PLANNING, reason="planning_started")

    def begin_execution(self, *, approval_required: bool = False) -> TaskRun:
        if approval_required:
            return self.transition(
                TaskState.AWAITING_APPROVAL,
                reason="risk_policy_requires_approval",
            )
        return self.transition(TaskState.EXECUTING, reason="execution_started")

    def approval_granted(self, approval_id: str) -> TaskRun:
        if not approval_id.strip():
            raise ValueError("approval_id must not be blank")
        return self.transition(
            TaskState.EXECUTING,
            reason="approval_granted",
            details={"approval_id": approval_id},
        )

    def begin_verification(self) -> TaskRun:
        return self.transition(TaskState.VERIFYING, reason="verification_started")

    def apply_gate_verdict(
        self,
        verdict: str,
        *,
        decision_id: str,
        bundle_ref: str | None = None,
        reasons: tuple[str, ...] = (),
    ) -> TaskRun:
        normalized = verdict.upper()
        try:
            target = _GATE_TRANSITIONS[normalized]
        except KeyError as exc:
            raise ValueError(f"unknown Evidence Gate verdict: {verdict!r}") from exc
        details: dict[str, Any] = {
            "gate_verdict": normalized,
            "decision_id": decision_id,
            "reasons": list(reasons),
        }
        if bundle_ref:
            details["bundle_ref"] = bundle_ref
        return self.transition(
            target,
            reason="evidence_gate_decided",
            actor="evidence_gate",
            details=details,
        )

    def queue_evolution(self) -> TaskRun:
        return self.transition(
            TaskState.EVOLUTION_PENDING,
            reason="experience_extraction_queued",
        )

    async def record_execution_event(self, event: ExecutionTraceEvent) -> None:
        """Trace hook accepted directly by :class:`ExecutionGateway`."""

        current = self.run
        self.store.append_event(
            TraceEvent(
                trace_id=current.trace_id,
                span_id=event.invocation_id,
                task_id=current.task_id,
                event_type="tool_execution_stage",
                tool_name=event.tool_name,
                normalized_args_hash=event.arguments_hash or None,
                risk_decision=event.risk_level,
                status=event.status.value if event.status is not None else event.stage.value,
                payload={
                    "stage": event.stage.value,
                    "reason_codes": list(event.reason_codes),
                    "argument_keys": list(event.argument_keys),
                    "arguments_hash": event.arguments_hash,
                },
            )
        )
