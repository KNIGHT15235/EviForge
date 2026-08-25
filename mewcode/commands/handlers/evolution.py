from __future__ import annotations

from mewcode.commands.registry import Command, CommandContext, CommandType
from mewcode.evolution import EvolutionState, FeedbackOutcome


def _adapter(ctx: CommandContext):
    adapter = ctx.config.get("evolution_adapter") if isinstance(ctx.config, dict) else None
    if adapter is None:
        raise RuntimeError("Evolution control plane is unavailable")
    return adapter


async def handle_evolution(ctx: CommandContext) -> None:
    adapter = _adapter(ctx)
    parts = ctx.args.split()
    action = parts[0].casefold() if parts else "status"

    if action in {"status", "list"}:
        status = adapter.status(candidate_limit=50)
        lines = [
            "Trace-to-Skill 状态（仓库内 SKILL.md 仅为投影）",
            f"候选经验: {len(status.candidates)}",
            f"Skill 版本: {len(status.manifests)}",
        ]
        for item in status.candidates[:10]:
            lines.append(
                f"- candidate {item.candidate.candidate_id}: "
                f"{item.disposition.value}, eligible={item.eligible_for_promotion}"
            )
        for manifest in status.manifests[:10]:
            lines.append(
                f"- skill {manifest.skill_id}@{manifest.version}: "
                f"{manifest.rollout_state.value}"
            )
        ctx.ui.add_system_message("\n".join(lines))
        return

    if action == "promote" and len(parts) in {3, 4}:
        skill_id, version_text = parts[1:3]
        target = parts[3] if len(parts) == 4 else EvolutionState.CANARY.value
        manifest = adapter.promote(
            skill_id,
            int(version_text),
            target,
            manual_approval=True,
        )
        ctx.ui.add_system_message(
            f"Skill {manifest.skill_id}@{manifest.version} → {manifest.rollout_state.value}"
        )
        return

    if action == "rollback" and len(parts) >= 4:
        skill_id, version_text = parts[1:3]
        reason = " ".join(parts[3:])
        rolled_back, restored = adapter.rollback(
            skill_id,
            int(version_text),
            reason=reason,
        )
        restored_text = (
            f"; restored={restored.skill_id}@{restored.version}" if restored else ""
        )
        ctx.ui.add_system_message(
            f"Rolled back {rolled_back.skill_id}@{rolled_back.version}{restored_text}"
        )
        return

    if action == "feedback" and len(parts) >= 4:
        skill_id, version_text, outcome_text = parts[1:4]
        outcome = FeedbackOutcome(outcome_text.casefold())
        if outcome is FeedbackOutcome.HIT:
            ctx.ui.add_system_message(
                "hit feedback is recorded automatically when a Skill is injected"
            )
            return
        evidence_ref = (
            getattr(ctx.agent, "_last_evidence_bundle_ref", "")
            or getattr(getattr(ctx.agent, "_last_completion_block", None), "bundle_ref", "")
        )
        if not evidence_ref or evidence_ref == "unavailable":
            raise ValueError("help/harm feedback requires a durable Evidence bundle")
        task_id = (
            ctx.agent.task_runtime.task_id
            if getattr(ctx.agent, "task_runtime", None) is not None
            else f"session-{ctx.session.session_id}"
        )
        adapter.record_feedback(
            skill_id=skill_id,
            version=int(version_text),
            task_id=task_id,
            outcome=outcome,
            evidence_refs=(evidence_ref,),
            actor="user:interactive",
        )
        ctx.ui.add_system_message(
            f"Recorded evidence-bound {outcome.value} for {skill_id}@{version_text}"
        )
        return

    ctx.ui.add_system_message(
        "用法: /evolution [status|promote <skill> <version> [canary|active]|"
        "rollback <skill> <version> <reason>|feedback <skill> <version> <help|harm>]"
    )


EVOLUTION_COMMAND = Command(
    name="evolution",
    aliases=["evo"],
    description="查看、晋升或回滚证据门控 Skill",
    usage="/evolution status",
    type=CommandType.LOCAL,
    handler=handle_evolution,
)
