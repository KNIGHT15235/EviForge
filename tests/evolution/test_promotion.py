from __future__ import annotations

import pytest

from mewcode.evolution import (
    EvolutionRegistry,
    EvolutionState,
    PromotionGateError,
    RiskLevel,
)


def make_skill(registry, candidate_factory, policy, *, risk=RiskLevel.LOW):
    registry.register_candidate(candidate_factory())
    return registry.create_skill(
        "candidate-1",
        skill_id="async-cancel",
        name="Async cancellation cleanup",
        description="Preserve cancellation while cleaning resources.",
        promotion_policy=policy,
        risk_level=risk,
    )


def test_one_observation_can_reach_canary_but_never_active(
    tmp_path, candidate_factory, policy, validation_factory
):
    with EvolutionRegistry(tmp_path / "registry.db") as registry:
        manifest = make_skill(registry, candidate_factory, policy)
        registry.add_validation(
            manifest.skill_id, manifest.version, validation_factory(1)
        )
        canary = registry.promote(
            manifest.skill_id, manifest.version, EvolutionState.CANARY
        )
        assert canary.rollout_state is EvolutionState.CANARY
        with pytest.raises(PromotionGateError, match="active evidence"):
            registry.promote(
                manifest.skill_id, manifest.version, EvolutionState.ACTIVE
            )


def test_repeated_same_group_does_not_fake_independence(
    tmp_path, candidate_factory, policy, validation_factory
):
    with EvolutionRegistry(tmp_path / "registry.db") as registry:
        manifest = make_skill(registry, candidate_factory, policy)
        for index in range(1, 4):
            registry.add_validation(
                manifest.skill_id,
                manifest.version,
                validation_factory(
                    index,
                    group="repo-group-a",
                    repository=f"repository-{index}",
                ),
            )
        summary = registry.validation_summary(manifest.skill_id, manifest.version)
        assert summary.passing_groups == ("repo-group-a",)
        assert not summary.meets_active


def test_active_requires_all_preregistered_independent_groups_and_zero_harm(
    tmp_path, candidate_factory, policy, validation_factory
):
    with EvolutionRegistry(tmp_path / "registry.db") as registry:
        manifest = make_skill(registry, candidate_factory, policy)
        for index in range(1, 4):
            registry.add_validation(
                manifest.skill_id, manifest.version, validation_factory(index)
            )
        registry.promote(manifest.skill_id, manifest.version, EvolutionState.CANARY)
        active = registry.promote(
            manifest.skill_id, manifest.version, EvolutionState.ACTIVE
        )
        assert active.rollout_state is EvolutionState.ACTIVE
        assert active.confidence_lower_bound == pytest.approx(0.8)

    with EvolutionRegistry(tmp_path / "harm.db") as registry:
        manifest = make_skill(registry, candidate_factory, policy)
        registry.add_validation(
            manifest.skill_id,
            manifest.version,
            validation_factory(1, harm_count=1),
        )
        for index in (2, 3):
            registry.add_validation(
                manifest.skill_id, manifest.version, validation_factory(index)
            )
        summary = registry.validation_summary(manifest.skill_id, manifest.version)
        assert summary.total_harm == 1
        assert not summary.meets_canary
        assert not summary.meets_active


def test_different_group_labels_from_same_repository_are_not_independent(
    tmp_path, candidate_factory, policy, validation_factory
):
    with EvolutionRegistry(tmp_path / "registry.db") as registry:
        manifest = make_skill(registry, candidate_factory, policy)
        for index in range(1, 4):
            registry.add_validation(
                manifest.skill_id,
                manifest.version,
                validation_factory(index, repository="same-repository"),
            )
        summary = registry.validation_summary(manifest.skill_id, manifest.version)
        assert summary.passing_groups == policy.validation_groups
        assert summary.distinct_repositories == 1
        assert not summary.meets_active


def test_high_risk_requires_manual_approval(
    tmp_path, candidate_factory, policy, validation_factory
):
    with EvolutionRegistry(tmp_path / "registry.db") as registry:
        manifest = make_skill(
            registry, candidate_factory, policy, risk=RiskLevel.HIGH
        )
        registry.add_validation(
            manifest.skill_id, manifest.version, validation_factory(1)
        )
        with pytest.raises(PromotionGateError, match="manual approval"):
            registry.promote(
                manifest.skill_id, manifest.version, EvolutionState.CANARY
            )
        promoted = registry.promote(
            manifest.skill_id,
            manifest.version,
            EvolutionState.CANARY,
            manual_approval=True,
        )
        assert promoted.rollout_state is EvolutionState.CANARY
