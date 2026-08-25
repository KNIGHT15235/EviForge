from __future__ import annotations

from datetime import timedelta

from mewcode.evolution import (
    EvolutionRegistry,
    EvolutionState,
    RetrievalContext,
    SkillManifest,
)
from tests.evolution.conftest import NOW


def activate(registry, candidate_factory, policy, validation_factory, *, tokens=None):
    registry.register_candidate(candidate_factory())
    manifest = registry.create_skill(
        "candidate-1",
        skill_id="async-cancel",
        name="Async cancellation cleanup",
        description="Preserve cancellation while cleaning resources.",
        promotion_policy=policy,
        estimated_tokens=tokens,
    )
    for index in range(1, 4):
        registry.add_validation(
            manifest.skill_id, manifest.version, validation_factory(index)
        )
    registry.promote(manifest.skill_id, manifest.version, EvolutionState.CANARY)
    return registry.promote(
        manifest.skill_id, manifest.version, EvolutionState.ACTIVE
    )


def test_active_retrieval_respects_signature_scope_expiry_and_budget(
    tmp_path, candidate_factory, policy, validation_factory
):
    with EvolutionRegistry(tmp_path / "registry.db") as registry:
        active = activate(registry, candidate_factory, policy, validation_factory)
        context = RetrievalContext(
            project_fingerprint="project-a", language="python", at=NOW
        )
        assert not registry.retrieve_active(
            task_signature=active.task_signatures[0],
            context=context,
            token_budget=active.estimated_tokens - 1,
        )
        selected = registry.retrieve_active(
            task_signature=active.task_signatures[0],
            context=context,
            token_budget=active.estimated_tokens,
        )
        assert len(selected) == 1
        assert selected[0].token_cost == active.estimated_tokens
        assert "manifestHash" in selected[0].markdown
        assert not registry.retrieve_active(
            task_signature=active.task_signatures[0],
            context=RetrievalContext(
                project_fingerprint="another-project", language="python", at=NOW
            ),
            token_budget=1000,
        )
        assert not registry.retrieve_active(
            task_signature="unrelated task",
            context=context,
            token_budget=1000,
        )


def test_active_skill_is_filtered_after_expiry(
    tmp_path, candidate_factory, policy, validation_factory
):
    expires_at = NOW + timedelta(days=30)
    with EvolutionRegistry(tmp_path / "registry.db") as registry:
        active = activate(
            registry,
            lambda candidate_id="candidate-1": candidate_factory(
                candidate_id, expires_at=expires_at
            ),
            policy,
            validation_factory,
        )
        assert registry.retrieve_active(
            task_signature=active.task_signatures[0],
            context=RetrievalContext(
                project_fingerprint="project-a",
                language="python",
                at=expires_at - timedelta(seconds=1),
            ),
            token_budget=5000,
        )
        assert not registry.retrieve_active(
            task_signature=active.task_signatures[0],
            context=RetrievalContext(
                project_fingerprint="project-a",
                language="python",
                at=expires_at,
            ),
            token_budget=5000,
        )


def test_expired_candidate_is_never_promotable(tmp_path, candidate_factory, policy):
    candidate = candidate_factory(expires_at=NOW + timedelta(minutes=30))
    with EvolutionRegistry(tmp_path / "registry.db") as registry:
        result = registry.register_candidate(candidate)
        # Registration happens after the fixture's historical timestamp.
        assert not result.eligible_for_promotion
        assert "expired_at_registration" in result.blocked_reasons


def test_export_manifest_is_canonical_and_markdown_is_regenerable(
    tmp_path, candidate_factory, policy, validation_factory
):
    with EvolutionRegistry(tmp_path / "registry.db") as registry:
        active = activate(registry, candidate_factory, policy, validation_factory)
        manifest_path, markdown_path = registry.export_skill(
            active.skill_id, active.version, tmp_path / "export"
        )
        loaded = SkillManifest.model_validate_json(
            manifest_path.read_text(encoding="utf-8")
        )
        first_markdown = markdown_path.read_text(encoding="utf-8")
        markdown_path.write_text("tampered", encoding="utf-8")
        registry.export_skill(active.skill_id, active.version, tmp_path / "export")
        assert loaded.manifest_hash == active.manifest_hash
        assert markdown_path.read_text(encoding="utf-8") == first_markdown


def test_versions_audit_and_atomic_rollback(
    tmp_path, candidate_factory, policy, validation_factory
):
    with EvolutionRegistry(tmp_path / "registry.db") as registry:
        v1 = activate(registry, candidate_factory, policy, validation_factory)
        candidate_v2 = candidate_factory(
            "candidate-2",
            task_signature="fix different asyncio cancellation leak",
            failure_signature="CancelledError swallowed in service",
            decision="Use a cancellation-safe context manager.",
            procedure=("Enter context.", "Re-raise cancellation."),
        )
        registry.register_candidate(candidate_v2)
        v2 = registry.create_skill(
            "candidate-2",
            skill_id="async-cancel",
            name="Async cancellation cleanup",
            description="Second version with cancellation-safe context.",
            promotion_policy=policy,
        )
        for index in range(4, 7):
            group_index = index - 3
            registry.add_validation(
                v2.skill_id,
                v2.version,
                validation_factory(
                    index,
                    group=f"repo-group-{chr(96 + group_index)}",
                    repository=f"repository-v2-{group_index}",
                ),
            )
        registry.promote(v2.skill_id, v2.version, EvolutionState.CANARY)
        v2_active = registry.promote(
            v2.skill_id, v2.version, EvolutionState.ACTIVE
        )
        assert registry.get_manifest(v1.skill_id, v1.version).rollout_state is EvolutionState.DEPRECATED
        bad, restored = registry.rollback(
            v2_active.skill_id,
            v2_active.version,
            restore_version=v1.version,
            reason="canary cohort regression",
        )
        assert bad.rollout_state is EvolutionState.ROLLED_BACK
        assert restored is not None
        assert restored.rollout_state is EvolutionState.ACTIVE
        selected = registry.retrieve_active(
            task_signature=v1.task_signatures[0],
            context=RetrievalContext(
                project_fingerprint="project-a", language="python", at=NOW
            ),
            token_budget=5000,
        )
        assert [item.manifest.version for item in selected] == [v1.version]
        audit_types = [event.event_type for event in registry.audit_events()]
        assert "skill_superseded" in audit_types
        assert "skill_rolled_back" in audit_types
        assert [
            revision.rollout_state
            for revision in registry.revision_history(v1.skill_id, v1.version)
        ] == [
            EvolutionState.QUARANTINE,
            EvolutionState.QUARANTINE,
            EvolutionState.QUARANTINE,
            EvolutionState.QUARANTINE,
            EvolutionState.CANARY,
            EvolutionState.ACTIVE,
            EvolutionState.DEPRECATED,
            EvolutionState.ACTIVE,
        ]
