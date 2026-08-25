from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from mewcode.evolution import (
    ExperienceCandidate,
    PromotionPolicy,
    ScopeKind,
    SkillScope,
    ValidationRecord,
)


NOW = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def candidate_factory():
    def build(
        candidate_id: str = "candidate-1",
        *,
        task_signature: str = "fix asyncio cancellation leak",
        failure_signature: str = "CancelledError swallowed in worker",
        decision: str = "Re-raise cancellation after deterministic cleanup.",
        procedure: tuple[str, ...] = (
            "Wrap resource cleanup in finally.",
            "Re-raise asyncio.CancelledError.",
            "Run the cancellation fixture.",
        ),
        project: str = "project-a",
        expires_at=None,
        security_flags: tuple[str, ...] = (),
    ) -> ExperienceCandidate:
        return ExperienceCandidate(
            candidate_id=candidate_id,
            task_signature=task_signature,
            failure_signature=failure_signature,
            root_cause_family="asyncio-cancellation",
            symptom="Worker hangs after cancellation.",
            context_constraints=("Python 3.11+",),
            decision=decision,
            procedure=procedure,
            failed_attempts=("Only cancel the outer task.",),
            evidence_refs=(f"evidence://{candidate_id}/receipt",),
            source_trace_ids=(f"trace-{candidate_id}",),
            source_commit="a1b2c3d",
            source_code_hash="a" * 64,
            project_fingerprint=project,
            scope=SkillScope(
                kind=ScopeKind.PROJECT,
                project_fingerprints=(project,),
                languages=("python",),
            ),
            confidence_prior=0.5,
            expires_at=expires_at,
            security_flags=security_flags,
            created_at=NOW,
        )

    return build

@pytest.fixture
def policy() -> PromotionPolicy:
    return PromotionPolicy(
        policy_id="evolution-v1",
        validation_groups=("repo-group-a", "repo-group-b", "repo-group-c"),
        canary_minimum_groups=1,
        active_minimum_independent_groups=3,
        minimum_group_pass_rate=1.0,
        minimum_effect_lower_bound=0.10,
    )


@pytest.fixture
def validation_factory():
    def build(
        index: int,
        *,
        group: str | None = None,
        repository: str | None = None,
        passed: bool = True,
        harm_count: int = 0,
        effect: float = 0.20,
    ) -> ValidationRecord:
        return ValidationRecord(
            validation_id=f"validation-{index}",
            validation_group=group or f"repo-group-{chr(96 + index)}",
            repository_fingerprint=repository or f"repository-{index}",
            run_id=f"run-{index}",
            passed=passed,
            harm_count=harm_count,
            effect_lower_bound=effect,
            confidence_lower_bound=0.80,
            evidence_refs=(f"evidence://validation/{index}",),
            observed_at=NOW + timedelta(minutes=index),
        )

    return build
