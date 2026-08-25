from __future__ import annotations

from datetime import datetime

import pytest
from pydantic import ValidationError

from mewcode.evolution import EvolutionState, ExperienceCandidate, SkillManifest


def test_candidate_is_strict_versioned_and_starts_quarantined(candidate_factory):
    candidate = candidate_factory()
    assert candidate.schema_version == 1
    assert candidate.status is EvolutionState.QUARANTINE
    assert candidate.source_code_hash == "a" * 64
    assert candidate.failed_attempts

    data = candidate.model_dump()
    data["unknown"] = True
    with pytest.raises(ValidationError, match="Extra inputs"):
        ExperienceCandidate.model_validate(data)


def test_candidate_rejects_non_quarantine_and_naive_expiry(candidate_factory):
    data = candidate_factory().model_dump()
    data["status"] = "active"
    with pytest.raises(ValidationError, match="must start in quarantine"):
        ExperienceCandidate.model_validate(data)

    data = candidate_factory().model_dump()
    data["expires_at"] = datetime(2027, 1, 1)
    with pytest.raises(ValidationError, match="timezone-aware"):
        ExperienceCandidate.model_validate(data)


def test_manifest_hash_detects_tampering(tmp_path, candidate_factory, policy):
    from mewcode.evolution import EvolutionRegistry

    with EvolutionRegistry(tmp_path / "registry.db") as registry:
        registry.register_candidate(candidate_factory())
        manifest = registry.create_skill(
            "candidate-1",
            skill_id="async-cancel",
            name="Async cancellation cleanup",
            description="Preserve cancellation while cleaning resources.",
            promotion_policy=policy,
        )
    data = manifest.model_dump(mode="json")
    data["decision"] = "Silently swallow cancellation."
    with pytest.raises(ValidationError, match="content_hash"):
        SkillManifest.model_validate(data)
