"""Production adapter between typed tool execution and the durable journal."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from mewcode.execution.context import ExecutionAssessment, ExecutionContext
from mewcode.execution.descriptor import ToolDescriptor
from mewcode.recovery.models import ActionState, AttemptLease, EffectKind
from mewcode.recovery.store import RecoveryStore


@dataclass(frozen=True, slots=True)
class JournalHandle:
    action_id: str
    lease: AttemptLease
    ambiguous_on_interrupt: bool


class RecoveryExecutionCoordinator:
    """Journal non-read effects around the actual tool commit boundary.

    Generic legacy tools do not expose a two-phase file operation, so local
    writes are recorded as opaque effects.  Command/network/external effects
    are additionally treated as ambiguous when control is lost after STARTED.
    """

    def __init__(self, store: RecoveryStore, *, ticket_ttl_seconds: float = 120.0) -> None:
        self.store = store
        self.ticket_ttl_seconds = ticket_ttl_seconds

    def begin(
        self,
        assessment: ExecutionAssessment,
        context: ExecutionContext,
        *,
        authorization_source: str,
    ) -> JournalHandle | None:
        descriptor = assessment.descriptor
        if descriptor is None or not _has_side_effect(descriptor):
            return None
        action_id = "act_" + hashlib.sha256(
            f"{context.task_id}\x1f{assessment.invocation_id}".encode("utf-8")
        ).hexdigest()[:32]
        ambiguous = descriptor.category == "command" or descriptor.side_effect in {
            "network",
            "external_write",
            "unknown",
        }
        action = self.store.prepare_action(
            task_id=context.task_id,
            action_id=action_id,
            action_type=f"tool:{assessment.tool_name}",
            idempotency_key=f"invocation:{assessment.invocation_id}",
            normalized_args_hash=assessment.arguments_hash,
            cwd=context.cwd,
            plan_hash=context.plan_hash,
            expected_pre_state_hash=context.expected_pre_state_hash,
            # Opaque legacy tools expose no postcondition that a restart can
            # prove.  Mark every side effect EXTERNAL for crash scanning (no
            # automatic replay); a caught ordinary local-tool error is still
            # deterministically closed as FAILED by ``interrupt`` below.
            effect_kind=EffectKind.EXTERNAL,
            postcondition={
                "tool_name": assessment.tool_name,
                "arguments_hash": assessment.arguments_hash,
            },
        )
        if action.state is not ActionState.PREPARED:
            raise RuntimeError(
                f"invocation {assessment.invocation_id} already journaled as {action.state.value}"
            )
        ticket = self.store.issue_for_action(
            action.action_id,
            approver=authorization_source,
            ttl_seconds=self.ticket_ttl_seconds,
            max_uses=1,
        )
        token = f"gateway:{assessment.invocation_id}"
        self.store.reserve_for_action(
            ticket.ticket_id,
            action.action_id,
            reservation_token=token,
        )
        self.store.authorize_action(action.action_id, ticket.ticket_id, reservation_token=token)
        lease = self.store.start_action(action.action_id, reservation_token=token)
        return JournalHandle(action.action_id, lease, ambiguous)

    def succeed(self, handle: JournalHandle | None) -> None:
        if handle is not None:
            self.store.finish_action(
                handle.lease,
                succeeded=True,
                reason="tool_reported_success",
            )

    def fail(self, handle: JournalHandle | None, *, error: str) -> None:
        if handle is not None:
            self.store.finish_action(
                handle.lease,
                succeeded=False,
                error=error,
                reason="tool_reported_error",
            )

    def interrupt(self, handle: JournalHandle | None, *, error: str) -> None:
        if handle is None:
            return
        if handle.ambiguous_on_interrupt:
            self.store.mark_uncertain(
                handle.lease,
                reason="tool_interrupted_after_started",
                error=error,
            )
        else:
            self.store.finish_action(
                handle.lease,
                succeeded=False,
                error=error,
                reason="tool_raised_before_success",
            )


def _has_side_effect(descriptor: ToolDescriptor) -> bool:
    return descriptor.category != "read" or descriptor.side_effect not in {"none", "local_read"}
