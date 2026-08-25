from __future__ import annotations

from mewcode.commands.registry import Command, CommandContext, CommandType
from mewcode.recovery import ActionState, AttemptLease, EffectKind


def _components(ctx: CommandContext):
    if not isinstance(ctx.config, dict):
        return None
    return ctx.config.get("runtime_components")


async def handle_recovery(ctx: CommandContext) -> None:
    components = _components(ctx)
    if components is None:
        ctx.ui.add_system_message("Recovery runtime is unavailable.")
        return
    parts = ctx.args.strip().split()
    action = parts[0].casefold() if parts else "status"
    store = components.recovery
    if action in {"status", "list"}:
        report = store.scan_recovery()
        if not report.items:
            ctx.ui.add_system_message("Recovery: no unresolved interrupted actions.")
            return
        lines = [f"Recovery: {len(report.items)} unresolved action(s)"]
        for item in report.items:
            lines.append(
                f"- {item.action.action_id} [{item.action.state.value}] "
                f"{item.recommendation}: {item.reason}"
            )
        lines.append("Use /recovery inspect <id>, /recovery ack <id> confirm <note>, or /recovery retry <id> confirm.")
        ctx.ui.add_system_message("\n".join(lines))
        return
    if len(parts) < 2:
        ctx.ui.add_system_message(
            "Usage: /recovery status|inspect <id>|ack <id> confirm <note>|retry <id> confirm"
        )
        return
    action_id = parts[1]
    record = store.get_action(action_id)
    if record is None:
        ctx.ui.add_system_message(f"Recovery action not found: {action_id}")
        return
    if action == "inspect":
        journal = store.list_journal(action_id=action_id)
        lines = [
            f"Action: {record.action_id}",
            f"Task: {record.task_id}",
            f"State/effect: {record.state.value} / {record.effect_kind.value}",
            f"Error: {record.error or '(none)'}",
            "Journal:",
        ]
        lines.extend(
            f"- #{entry.sequence} {entry.to_state.value}: {entry.reason}"
            for entry in journal
        )
        ctx.ui.add_system_message("\n".join(lines))
        return
    if action == "ack":
        if len(parts) < 4 or parts[2].casefold() != "confirm":
            ctx.ui.add_system_message("Usage: /recovery ack <id> confirm <review note>")
            return
        if record.state is not ActionState.UNCERTAIN:
            ctx.ui.add_system_message("Only an UNCERTAIN action can be acknowledged.")
            return
        note = " ".join(parts[3:]).strip()
        store.create_checkpoint(
            task_id=record.task_id,
            action_id=record.action_id,
            label="human_acknowledged_uncertain",
            payload={"note": note, "automatic_retry": False},
        )
        ctx.ui.add_system_message(
            f"Acknowledged {action_id}; no external action was replayed."
        )
        return
    if action == "retry":
        if len(parts) != 3 or parts[2].casefold() != "confirm":
            ctx.ui.add_system_message("Usage: /recovery retry <id> confirm")
            return
        if record.state is ActionState.UNCERTAIN:
            ctx.ui.add_system_message("UNCERTAIN actions are never retried automatically.")
            return
        if record.state is not ActionState.STARTED or record.effect_kind is not EffectKind.FILE_REPLACE:
            ctx.ui.add_system_message(
                "Only a hash-verified staged FILE_REPLACE action can be retried here."
            )
            return
        lease = AttemptLease(
            record.action_id,
            record.attempt_id or "",
            record.fencing_generation,
        )
        fenced = store.supersede_attempt(
            lease, reason="operator_confirmed_safe_file_retry"
        )
        completed = store.execute_file_replace(fenced)
        ctx.ui.add_system_message(
            f"Recovery retry {completed.action_id}: {completed.state.value}"
        )
        return
    ctx.ui.add_system_message(f"Unknown recovery action: {action}")


RECOVERY_COMMAND = Command(
    name="recovery",
    aliases=["recover"],
    description="查看并处理可恢复或不确定的中断操作",
    usage="/recovery status|inspect <id>|ack <id> confirm <note>|retry <id> confirm",
    type=CommandType.LOCAL,
    handler=handle_recovery,
)


__all__ = ["RECOVERY_COMMAND", "handle_recovery"]
