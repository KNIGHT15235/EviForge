"""Unified operator workflow for governed experience evolution.

This module is deliberately an orchestration facade.  The registry remains the
authority for quarantine, immutable revisions, validation gates and rollback;
the facade merely makes the complete lifecycle available to CLI/TUI adapters
without granting them a bypass around those controls.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .adapter import EvolutionStatus, ProductionEvolutionAdapter
from .models import (
    AuditEvent,
    CandidateRecord,
    EvolutionState,
    FeedbackOutcome,
    PromotionPolicy,
    RiskLevel,
    SkillFeedback,
    SkillManifest,
    ValidationRecord,
    ValidationSummary,
)
from .registry import EvolutionRegistry, PromotionGateError


@dataclass(frozen=True, slots=True)
class CandidateExperienceReview:
    """Traceable review view for one quarantined experience candidate."""

    record: CandidateRecord
    projected_versions: tuple[SkillManifest, ...]
    audit_events: tuple[AuditEvent, ...]


@dataclass(frozen=True, slots=True)
class SkillExperienceReview:
    """Traceable review view for one immutable Skill version."""

    manifest: SkillManifest
    revisions: tuple[SkillManifest, ...]
    validations: tuple[ValidationRecord, ...]
    validation_summary: ValidationSummary
    feedback: tuple[SkillFeedback, ...]
    audit_events: tuple[AuditEvent, ...]


class ExperienceWorkflow:
    """One governed API for candidate review through feedback and rollback."""

    def __init__(self, adapter: ProductionEvolutionAdapter) -> None:
        self.adapter = adapter

    @property
    def registry(self) -> EvolutionRegistry:
        return self.adapter.registry

    def status(self, *, candidate_limit: int | None = 100) -> EvolutionStatus:
        return self.adapter.status(candidate_limit=candidate_limit)

    def list_candidates(self, *, limit: int | None = 100) -> tuple[CandidateRecord, ...]:
        return self.registry.list_candidates(limit=limit)

    def list_skills(self) -> tuple[SkillManifest, ...]:
        return self.registry.list_manifests()

    def list_feedback(
        self,
        *,
        skill_id: str | None = None,
        version: int | None = None,
    ) -> tuple[SkillFeedback, ...]:
        return self.registry.list_feedback(skill_id=skill_id, version=version)

    def review_candidate(self, candidate_id: str) -> CandidateExperienceReview:
        record = self.registry.get_candidate(candidate_id)
        projected = tuple(
            manifest
            for manifest in self.registry.list_manifests()
            if candidate_id in manifest.candidate_ids
        )
        events = tuple(
            event
            for event in self.registry.audit_events()
            if event.candidate_id == candidate_id
            or (
                event.skill_id is not None
                and any(
                    manifest.skill_id == event.skill_id
                    and manifest.version == event.version
                    for manifest in projected
                )
            )
        )
        return CandidateExperienceReview(record, projected, events)

    def review_skill(self, skill_id: str, version: int) -> SkillExperienceReview:
        manifest = self.registry.get_manifest(skill_id, version)
        return SkillExperienceReview(
            manifest=manifest,
            revisions=self.registry.revision_history(skill_id, version),
            validations=self.registry.list_validations(skill_id, version),
            validation_summary=self.registry.validation_summary(skill_id, version),
            feedback=self.registry.list_feedback(skill_id=skill_id, version=version),
            audit_events=tuple(
                event
                for event in self.registry.audit_events()
                if event.skill_id == skill_id and event.version == version
            ),
        )

    def create(
        self,
        candidate_id: str,
        *,
        skill_id: str,
        name: str,
        description: str,
        promotion_policy: PromotionPolicy,
        risk_level: RiskLevel = RiskLevel.LOW,
    ) -> SkillManifest:
        """Project a reviewed candidate into an immutable QUARANTINE version."""

        manifest = self.adapter.create_skill(
            candidate_id,
            skill_id=skill_id,
            name=name,
            description=description,
            promotion_policy=promotion_policy,
            risk_level=risk_level,
        )
        if manifest.rollout_state is not EvolutionState.QUARANTINE:
            raise RuntimeError("new experience versions must remain in quarantine")
        return manifest

    def validate(
        self, skill_id: str, version: int, record: ValidationRecord
    ) -> ValidationSummary:
        """Append one evidence-bound replay/validation result."""

        return self.adapter.validate(skill_id, version, record)

    def promote(
        self,
        skill_id: str,
        version: int,
        target: EvolutionState | str,
        *,
        manual_approval: bool = False,
    ) -> SkillManifest:
        """Promote only after the registered replay/validation gate passes."""

        target_state = EvolutionState(target)
        summary = self.registry.validation_summary(skill_id, version)
        if target_state is EvolutionState.CANARY and not summary.meets_canary:
            raise PromotionGateError(
                "replay/validation gate has not met the canary threshold: "
                + "; ".join(summary.reasons)
            )
        if target_state is EvolutionState.ACTIVE and not summary.meets_active:
            raise PromotionGateError(
                "replay/validation gate has not met the active threshold: "
                + "; ".join(summary.reasons)
            )
        return self.adapter.promote(
            skill_id,
            version,
            target_state,
            manual_approval=manual_approval,
        )

    def rollback(
        self,
        skill_id: str,
        version: int,
        *,
        reason: str,
        restore_version: int | None = None,
    ) -> tuple[SkillManifest, SkillManifest | None]:
        return self.adapter.rollback(
            skill_id,
            version,
            restore_version=restore_version,
            reason=reason,
        )

    def feedback(
        self,
        *,
        skill_id: str,
        version: int,
        task_id: str,
        outcome: FeedbackOutcome | str,
        evidence_refs: Iterable[str] = (),
        actor: str = "experience-workflow",
        feedback_id: str | None = None,
    ) -> tuple[SkillManifest, SkillManifest | None] | None:
        return self.adapter.record_feedback(
            skill_id=skill_id,
            version=version,
            task_id=task_id,
            outcome=outcome,
            evidence_refs=evidence_refs,
            actor=actor,
            feedback_id=feedback_id,
        )
