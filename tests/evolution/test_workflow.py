from __future__ import annotations

import pytest

from mewcode.evolution import (
    EvolutionRegistry,
    EvolutionState,
    ExperienceWorkflow,
    FeedbackOutcome,
    ProductionEvolutionAdapter,
    PromotionGateError,
    SkillNotFoundError,
)
from mewcode.runtime import RuntimeStore


def test_experience_workflow_is_quarantined_traceable_and_gate_bound(
    tmp_path, candidate_factory, policy, validation_factory
) -> None:
    store = RuntimeStore(control_root=tmp_path / "control", workspace_id="workflow")
    with EvolutionRegistry(tmp_path / "evolution.db") as registry:
        adapter = ProductionEvolutionAdapter(registry, store, workspace=tmp_path)
        workflow = ExperienceWorkflow(adapter)

        candidate = registry.register_candidate(
            candidate_factory("candidate-workflow"), actor="test-extractor"
        )
        assert candidate.candidate.status is EvolutionState.QUARANTINE

        candidate_review = workflow.review_candidate("candidate-workflow")
        assert candidate_review.record.candidate.source_trace_ids == (
            "trace-candidate-workflow",
        )
        assert candidate_review.record.candidate.evidence_refs == (
            "evidence://candidate-workflow/receipt",
        )
        assert candidate_review.audit_events[0].event_type == (
            "experience_candidate_created"
        )

        manifest = workflow.create(
            "candidate-workflow",
            skill_id="async-workflow",
            name="Async workflow",
            description="Preserve cancellation after deterministic cleanup.",
            promotion_policy=policy,
        )
        assert manifest.rollout_state is EvolutionState.QUARANTINE

        with pytest.raises(PromotionGateError, match="replay/validation gate"):
            workflow.promote("async-workflow", 1, EvolutionState.CANARY)

        first_summary = workflow.validate(
            "async-workflow", 1, validation_factory(1)
        )
        assert first_summary.meets_canary
        canary = workflow.promote("async-workflow", 1, EvolutionState.CANARY)
        assert canary.rollout_state is EvolutionState.CANARY

        with pytest.raises(PromotionGateError, match="replay/validation gate"):
            workflow.promote("async-workflow", 1, EvolutionState.ACTIVE)

        workflow.validate("async-workflow", 1, validation_factory(2))
        workflow.validate("async-workflow", 1, validation_factory(3))
        active = workflow.promote("async-workflow", 1, EvolutionState.ACTIVE)
        assert active.rollout_state is EvolutionState.ACTIVE

        skill_review = workflow.review_skill("async-workflow", 1)
        assert len(skill_review.validations) == 3
        assert skill_review.validation_summary.meets_active
        assert skill_review.manifest.candidate_ids == ("candidate-workflow",)
        assert skill_review.manifest.source_trace_ids == (
            "trace-candidate-workflow",
        )
        assert skill_review.manifest.source_code_hashes == ("a" * 64,)
        assert any(
            event.event_type == "skill_published"
            for event in skill_review.audit_events
        )

        rollback = workflow.feedback(
            skill_id="async-workflow",
            version=1,
            task_id="task-observed",
            outcome=FeedbackOutcome.HARM,
            evidence_refs=("evidence://task-observed/failure",),
            feedback_id="feedback-workflow-harm",
        )
        assert rollback is not None
        assert rollback[0].rollout_state is EvolutionState.ROLLED_BACK
        assert workflow.review_skill("async-workflow", 1).feedback[0].evidence_refs == (
            "evidence://task-observed/failure",
        )


def test_public_validation_list_rejects_unknown_version(tmp_path) -> None:
    with EvolutionRegistry(tmp_path / "evolution.db") as registry:
        with pytest.raises(SkillNotFoundError, match="skill not found"):
            registry.list_validations("missing-skill", 1)
