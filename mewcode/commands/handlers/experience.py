from __future__ import annotations

import uuid
from collections import Counter
from datetime import datetime, timezone

from mewcode.commands.registry import Command, CommandContext, CommandType
from mewcode.evolution import (
    EvolutionState,
    ExperienceWorkflow,
    FeedbackOutcome,
    PromotionPolicy,
    ValidationRecord,
)


_USAGE = (
    "/experience [status | list [candidates|skills|feedback] | "
    "review <candidate> | review skill <id> <version> | "
    "create <candidate> <skill> <group1,group2,...> <name> | "
    "validate <skill> <version> <group> <repository> <run-id> "
    "<pass|fail> <effect> <confidence> <evidence-ref> [harm-count] | "
    "promote <skill> <version> <canary|active> confirm | "
    "rollback <skill> <version> confirm <reason> | "
    "feedback <skill> <version> <help|harm>]"
)


def _workflow(ctx: CommandContext) -> ExperienceWorkflow:
    adapter = ctx.config.get("evolution_adapter") if isinstance(ctx.config, dict) else None
    if adapter is None:
        raise RuntimeError("Experience control plane is unavailable")
    return ExperienceWorkflow(adapter)


def _csv(values: tuple[str, ...]) -> str:
    return ", ".join(values) if values else "-"


def _status_text(workflow: ExperienceWorkflow) -> str:
    status = workflow.status(candidate_limit=None)
    candidate_states = Counter(
        record.disposition.value for record in status.candidates
    )
    rollout_states = Counter(manifest.rollout_state.value for manifest in status.manifests)
    feedback_count = len(workflow.list_feedback())
    return "\n".join(
        (
            "Experience 治理状态",
            "─────────────",
            f"候选: {len(status.candidates)} "
            f"(eligible={sum(item.eligible_for_promotion for item in status.candidates)})",
            "候选处置: "
            + (", ".join(f"{key}={value}" for key, value in sorted(candidate_states.items())) or "-"),
            f"Skill 版本: {len(status.manifests)}",
            "发布状态: "
            + (", ".join(f"{key}={value}" for key, value in sorted(rollout_states.items())) or "-"),
            f"反馈记录: {feedback_count}",
            "安全约束: candidate/新版本默认 quarantine；通过 replay/验证门禁后才可 promote",
        )
    )


def _candidate_review_text(workflow: ExperienceWorkflow, candidate_id: str) -> str:
    review = workflow.review_candidate(candidate_id)
    record = review.record
    candidate = record.candidate
    lines = [
        f"Experience Candidate {candidate.candidate_id} [UNTRUSTED REVIEW DATA]",
        "─────────────",
        f"状态: {candidate.status.value}",
        f"处置: {record.disposition.value}; eligible={record.eligible_for_promotion}",
        f"记录时间: {record.recorded_at.isoformat()}",
        f"任务签名: {candidate.task_signature}",
        f"失败签名: {candidate.failure_signature}",
        f"根因族: {candidate.root_cause_family}",
        f"决策: {candidate.decision}",
        "步骤: " + " | ".join(candidate.procedure),
        f"Evidence: {_csv(candidate.evidence_refs)}",
        f"来源 Trace: {_csv(candidate.source_trace_ids)}",
        f"来源 Commit: {candidate.source_commit}",
        f"来源代码哈希: {candidate.source_code_hash}",
        f"项目指纹: {candidate.project_fingerprint}",
        f"安全标记: {_csv(candidate.security_flags)}",
        f"阻断原因: {_csv(record.blocked_reasons)}",
        f"冲突项: {_csv(record.conflicts_with)}",
    ]
    if review.projected_versions:
        lines.append(
            "投影版本: "
            + ", ".join(
                f"{item.skill_id}@{item.version}({item.rollout_state.value})"
                for item in review.projected_versions
            )
        )
    else:
        lines.append("投影版本: -")
    lines.append("审计轨迹:")
    lines.extend(
        f"  #{event.sequence} {event.event_type} actor={event.actor} "
        f"at={event.occurred_at.isoformat()}"
        for event in review.audit_events
    )
    if not review.audit_events:
        lines.append("  -")
    return "\n".join(lines)


def _skill_review_text(
    workflow: ExperienceWorkflow, skill_id: str, version: int
) -> str:
    review = workflow.review_skill(skill_id, version)
    manifest = review.manifest
    summary = review.validation_summary
    lines = [
        f"Experience Skill {manifest.skill_id}@{manifest.version}",
        "─────────────",
        f"状态: {manifest.rollout_state.value}; 风险: {manifest.risk_level.value}",
        f"候选来源: {_csv(manifest.candidate_ids)}",
        f"Evidence: {_csv(manifest.evidence_refs)}",
        f"来源 Trace: {_csv(manifest.source_trace_ids)}",
        f"来源 Commit: {_csv(manifest.source_commits)}",
        f"来源代码哈希: {_csv(manifest.source_code_hashes)}",
        f"内容哈希: {manifest.content_hash}",
        f"Manifest 哈希: {manifest.manifest_hash}",
        f"Policy: {manifest.promotion_policy.policy_id}@"
        f"{manifest.promotion_policy.policy_version} "
        f"({manifest.promotion_policy.policy_hash})",
        f"修订数: {len(review.revisions)}",
        f"验证结论: canary={summary.meets_canary}, active={summary.meets_active}, "
        f"passing={len(summary.passing_groups)}/{len(summary.registered_groups)}, "
        f"harm={summary.total_harm}",
        f"门禁原因: {_csv(summary.reasons)}",
        "Replay/验证记录:",
    ]
    lines.extend(
        f"  {item.validation_id}: group={item.validation_group}, "
        f"repo={item.repository_fingerprint}, run={item.run_id}, "
        f"passed={item.passed}, harm={item.harm_count}, "
        f"evidence={_csv(item.evidence_refs)}"
        for item in review.validations
    )
    if not review.validations:
        lines.append("  -")
    lines.append("反馈记录:")
    lines.extend(
        f"  {item.feedback_id}: task={item.task_id}, outcome={item.outcome.value}, "
        f"evidence={_csv(item.evidence_refs)}"
        for item in review.feedback
    )
    if not review.feedback:
        lines.append("  -")
    lines.append("审计轨迹:")
    lines.extend(
        f"  #{event.sequence} {event.event_type} "
        f"{event.from_state.value if event.from_state else '-'}→"
        f"{event.to_state.value if event.to_state else '-'} actor={event.actor}"
        for event in review.audit_events
    )
    if not review.audit_events:
        lines.append("  -")
    return "\n".join(lines)


def _current_evidence(ctx: CommandContext) -> str:
    return (
        getattr(ctx.agent, "_last_evidence_bundle_ref", "")
        or getattr(getattr(ctx.agent, "_last_completion_block", None), "bundle_ref", "")
    )


def _current_task_id(ctx: CommandContext) -> str:
    runtime = getattr(ctx.agent, "task_runtime", None)
    if runtime is not None:
        return runtime.task_id
    return f"session-{ctx.session.session_id}"


async def handle_experience(ctx: CommandContext) -> None:
    workflow = _workflow(ctx)
    parts = ctx.args.split()
    action = parts[0].casefold() if parts else "status"

    if action in {"status", "stats"}:
        ctx.ui.add_system_message(_status_text(workflow))
        return

    if action == "list":
        target = parts[1].casefold() if len(parts) > 1 else "candidates"
        if target == "candidates":
            records = workflow.list_candidates(limit=100)
            lines = ["Experience candidates (newest first)"]
            lines.extend(
                f"- {item.candidate.candidate_id}: "
                f"{item.candidate.status.value}/{item.disposition.value}, "
                f"eligible={item.eligible_for_promotion}, "
                f"trace={_csv(item.candidate.source_trace_ids)}, "
                f"evidence={_csv(item.candidate.evidence_refs)}"
                for item in records
            )
        elif target == "skills":
            manifests = workflow.list_skills()
            lines = ["Experience Skill versions"]
            lines.extend(
                f"- {item.skill_id}@{item.version}: {item.rollout_state.value}, "
                f"candidate={_csv(item.candidate_ids)}, manifest={item.manifest_hash}"
                for item in manifests
            )
        elif target == "feedback":
            feedback = workflow.list_feedback()
            lines = ["Experience feedback (append-only)"]
            lines.extend(
                f"- {item.feedback_id}: {item.skill_id}@{item.version}, "
                f"task={item.task_id}, {item.outcome.value}, "
                f"evidence={_csv(item.evidence_refs)}"
                for item in feedback
            )
        else:
            ctx.ui.add_system_message("用法: /experience list [candidates|skills|feedback]")
            return
        if len(lines) == 1:
            lines.append("- empty")
        ctx.ui.add_system_message("\n".join(lines))
        return

    if action in {"review", "inspect"}:
        if len(parts) == 2:
            ctx.ui.add_system_message(_candidate_review_text(workflow, parts[1]))
            return
        if len(parts) == 3 and parts[1].casefold() == "candidate":
            ctx.ui.add_system_message(_candidate_review_text(workflow, parts[2]))
            return
        if len(parts) == 4 and parts[1].casefold() == "skill":
            ctx.ui.add_system_message(
                _skill_review_text(workflow, parts[2], int(parts[3]))
            )
            return
        ctx.ui.add_system_message(
            "用法: /experience review [candidate] <candidate-id> | "
            "/experience review skill <skill-id> <version>"
        )
        return

    if action == "create" and len(parts) >= 5:
        candidate_id, skill_id, group_text = parts[1:4]
        name = " ".join(parts[4:])
        groups = tuple(item.strip() for item in group_text.split(",") if item.strip())
        if len(groups) < 2:
            raise ValueError("create requires at least two independent replay groups")
        policy = PromotionPolicy(
            policy_id="experience-cli-v1",
            validation_groups=groups,
            canary_minimum_groups=1,
            active_minimum_independent_groups=len(groups),
            minimum_group_pass_rate=1.0,
            minimum_effect_lower_bound=0.0,
        )
        manifest = workflow.create(
            candidate_id,
            skill_id=skill_id,
            name=name,
            description=f"Evidence-governed experience: {name}",
            promotion_policy=policy,
        )
        ctx.ui.add_system_message(
            f"Created {manifest.skill_id}@{manifest.version} in "
            f"{manifest.rollout_state.value}; validate before promote. "
            f"manifest={manifest.manifest_hash}"
        )
        return

    if action == "validate" and len(parts) in {10, 11}:
        skill_id = parts[1]
        version = int(parts[2])
        verdict = parts[6].casefold()
        if verdict not in {"pass", "fail"}:
            raise ValueError("validation verdict must be pass or fail")
        record = ValidationRecord(
            validation_id=f"validation-{uuid.uuid4().hex}",
            validation_group=parts[3],
            repository_fingerprint=parts[4],
            run_id=parts[5],
            passed=verdict == "pass",
            harm_count=int(parts[10]) if len(parts) == 11 else 0,
            effect_lower_bound=float(parts[7]),
            confidence_lower_bound=float(parts[8]),
            evidence_refs=(parts[9],),
            observed_at=datetime.now(timezone.utc),
        )
        summary = workflow.validate(skill_id, version, record)
        ctx.ui.add_system_message(
            f"Recorded replay {record.validation_id} for {skill_id}@{version}; "
            f"canary={summary.meets_canary}, active={summary.meets_active}; "
            f"evidence={parts[9]}"
        )
        return

    if action == "promote" and len(parts) == 5:
        if parts[4].casefold() != "confirm":
            raise ValueError("promotion requires an explicit trailing confirm")
        target = EvolutionState(parts[3].casefold())
        manifest = workflow.promote(
            parts[1],
            int(parts[2]),
            target,
            manual_approval=True,
        )
        ctx.ui.add_system_message(
            f"Promoted {manifest.skill_id}@{manifest.version} → "
            f"{manifest.rollout_state.value}; manifest={manifest.manifest_hash}"
        )
        return

    if action == "rollback" and len(parts) >= 5:
        if parts[3].casefold() != "confirm":
            raise ValueError("rollback requires confirm before the reason")
        reason = " ".join(parts[4:])
        rolled_back, restored = workflow.rollback(
            parts[1], int(parts[2]), reason=reason
        )
        restored_text = (
            f"; restored={restored.skill_id}@{restored.version}" if restored else ""
        )
        ctx.ui.add_system_message(
            f"Rolled back {rolled_back.skill_id}@{rolled_back.version}; "
            f"reason={reason}{restored_text}"
        )
        return

    if action == "feedback" and len(parts) == 4:
        outcome = FeedbackOutcome(parts[3].casefold())
        if outcome is FeedbackOutcome.HIT:
            raise ValueError("hit is recorded automatically during retrieval")
        evidence_ref = _current_evidence(ctx)
        if not evidence_ref or evidence_ref == "unavailable":
            raise ValueError("help/harm feedback requires a durable Evidence bundle")
        result = workflow.feedback(
            skill_id=parts[1],
            version=int(parts[2]),
            task_id=_current_task_id(ctx),
            outcome=outcome,
            evidence_refs=(evidence_ref,),
            actor="user:interactive",
        )
        suffix = " and rolled back automatically" if result is not None else ""
        ctx.ui.add_system_message(
            f"Recorded {outcome.value} for {parts[1]}@{parts[2]} "
            f"with {evidence_ref}{suffix}"
        )
        return

    ctx.ui.add_system_message(f"用法: {_USAGE}")


EXPERIENCE_COMMAND = Command(
    name="experience",
    aliases=["xp"],
    description="审查、验证、发布和回滚可追溯经验",
    usage=_USAGE,
    type=CommandType.LOCAL,
    handler=handle_experience,
)
