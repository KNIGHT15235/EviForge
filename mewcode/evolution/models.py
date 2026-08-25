"""Versioned domain models for evidence-governed skill evolution.

The SQLite registry stores these models as its canonical representation.  Human
readable ``SKILL.md`` files are deliberately generated projections, never an
input to promotion decisions.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import datetime, timezone
from enum import Enum, StrEnum
from typing import Any, Literal, Mapping

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PositiveInt,
    ValidationInfo,
    field_validator,
    model_validator,
)


SCHEMA_VERSION = 1
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{7,64}$")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def normalize_signature(value: str) -> str:
    """Return a stable key without pretending to perform semantic matching."""

    normalized = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(normalized.split())


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    return value


def canonical_json(value: Mapping[str, Any] | BaseModel) -> str:
    payload = _jsonable(value)
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def sha256_json(value: Mapping[str, Any] | BaseModel) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


class StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_default=True,
    )


class EvolutionState(StrEnum):
    QUARANTINE = "quarantine"
    CANARY = "canary"
    ACTIVE = "active"
    DEPRECATED = "deprecated"
    ROLLED_BACK = "rolled_back"


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ScopeKind(StrEnum):
    PROJECT = "project"
    LANGUAGE = "language"
    GLOBAL = "global"


class RegistrationDisposition(StrEnum):
    ADDED = "added"
    DUPLICATE = "duplicate"
    CONFLICT = "conflict"
    BLOCKED = "blocked"


class FeedbackOutcome(StrEnum):
    """Observed effect of injecting one active skill into a later task."""

    HIT = "hit"
    HELP = "help"
    HARM = "harm"


class SkillScope(StrictModel):
    """A deny-by-default applicability boundary for evolved knowledge."""

    schema_version: Literal[1] = SCHEMA_VERSION
    kind: ScopeKind = ScopeKind.PROJECT
    project_fingerprints: tuple[str, ...] = ()
    languages: tuple[str, ...] = ()
    repository_families: tuple[str, ...] = ()

    @field_validator(
        "project_fingerprints", "languages", "repository_families", mode="after"
    )
    @classmethod
    def _unique_normalized_values(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(normalize_signature(value) for value in values)
        if any(not value for value in normalized):
            raise ValueError("scope values must not be blank")
        if len(normalized) != len(set(normalized)):
            raise ValueError("scope values must be unique")
        return normalized

    @model_validator(mode="after")
    def _validate_required_boundary(self) -> SkillScope:
        if self.kind is ScopeKind.PROJECT and not self.project_fingerprints:
            raise ValueError("project scope requires project_fingerprints")
        if self.kind is ScopeKind.LANGUAGE and not self.languages:
            raise ValueError("language scope requires languages")
        return self

    def matches(self, context: RetrievalContext) -> bool:
        project = normalize_signature(context.project_fingerprint)
        language = normalize_signature(context.language) if context.language else None
        family = (
            normalize_signature(context.repository_family)
            if context.repository_family
            else None
        )
        if self.kind is ScopeKind.PROJECT and project not in self.project_fingerprints:
            return False
        if self.kind is ScopeKind.LANGUAGE and language not in self.languages:
            return False
        if self.languages and language not in self.languages:
            return False
        if self.repository_families and family not in self.repository_families:
            return False
        return True

    @property
    def specificity(self) -> int:
        return {
            ScopeKind.PROJECT: 3,
            ScopeKind.LANGUAGE: 2,
            ScopeKind.GLOBAL: 1,
        }[self.kind]


class RetrievalContext(StrictModel):
    schema_version: Literal[1] = SCHEMA_VERSION
    project_fingerprint: str = Field(min_length=1, max_length=256)
    language: str | None = Field(default=None, min_length=1, max_length=64)
    repository_family: str | None = Field(default=None, min_length=1, max_length=128)
    at: datetime = Field(default_factory=utc_now)

    @field_validator("at")
    @classmethod
    def _aware_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("at must be timezone-aware")
        return value


class ExperienceCandidate(StrictModel):
    """Untrusted experience extracted from a trace.

    Construction is strict, but validity is not equivalent to safety.  The
    sanitizer and registry still quarantine instruction injection, secrets,
    conflicts, and stale candidates.
    """

    schema_version: Literal[1] = SCHEMA_VERSION
    candidate_id: str = Field(pattern=_IDENTIFIER_RE.pattern)
    task_signature: str = Field(min_length=3, max_length=512)
    failure_signature: str = Field(min_length=3, max_length=512)
    root_cause_family: str = Field(min_length=2, max_length=128)
    symptom: str = Field(default="", max_length=2_000)
    context_constraints: tuple[str, ...] = ()
    decision: str = Field(min_length=3, max_length=8_000)
    procedure: tuple[str, ...] = Field(min_length=1, max_length=64)
    failed_attempts: tuple[str, ...] = Field(default=(), max_length=64)
    evidence_refs: tuple[str, ...] = Field(min_length=1, max_length=128)
    source_trace_ids: tuple[str, ...] = Field(min_length=1, max_length=64)
    source_commit: str = Field(pattern=_COMMIT_RE.pattern)
    source_code_hash: str = Field(pattern=_HEX_RE.pattern)
    project_fingerprint: str = Field(min_length=3, max_length=256)
    scope: SkillScope
    confidence_prior: float = Field(ge=0.0, le=1.0)
    expires_at: datetime | None = None
    security_flags: tuple[str, ...] = ()
    status: EvolutionState = EvolutionState.QUARANTINE
    created_at: datetime = Field(default_factory=utc_now)

    @field_validator(
        "procedure",
        "failed_attempts",
        "evidence_refs",
        "source_trace_ids",
        "context_constraints",
        "security_flags",
    )
    @classmethod
    def _nonblank_unique(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value.strip() for value in values):
            raise ValueError("list values must not be blank")
        if len(values) != len(set(values)):
            raise ValueError("list values must be unique")
        return values

    @field_validator("created_at", "expires_at")
    @classmethod
    def _aware_datetimes(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def _quarantine_and_scope(self) -> ExperienceCandidate:
        if self.status is not EvolutionState.QUARANTINE:
            raise ValueError("new experience candidates must start in quarantine")
        if (
            self.scope.kind is ScopeKind.PROJECT
            and normalize_signature(self.project_fingerprint)
            not in self.scope.project_fingerprints
        ):
            raise ValueError("project_fingerprint must be included in project scope")
        if self.expires_at is not None and self.expires_at <= self.created_at:
            raise ValueError("expires_at must be later than created_at")
        return self

    @property
    def signature_key(self) -> str:
        return sha256_json(
            {
                "task": normalize_signature(self.task_signature),
                "failure": normalize_signature(self.failure_signature),
            }
        )

    @property
    def material_hash(self) -> str:
        return sha256_json(
            {
                "decision": normalize_signature(self.decision),
                "procedure": [normalize_signature(item) for item in self.procedure],
                "scope": self.scope.model_dump(mode="json"),
            }
        )

    def is_expired(self, *, at: datetime | None = None) -> bool:
        when = at or utc_now()
        return self.expires_at is not None and self.expires_at <= when


class PromotionPolicy(StrictModel):
    """Pre-registered policy snapshot bound to one immutable skill version."""

    schema_version: Literal[1] = SCHEMA_VERSION
    policy_id: str = Field(pattern=_IDENTIFIER_RE.pattern)
    policy_version: PositiveInt = 1
    validation_groups: tuple[str, ...] = Field(min_length=2, max_length=256)
    canary_minimum_groups: PositiveInt = 1
    active_minimum_independent_groups: int = Field(default=3, ge=2)
    minimum_group_pass_rate: float = Field(default=1.0, ge=0.0, le=1.0)
    minimum_effect_lower_bound: float = 0.0
    max_total_harm: Literal[0] = 0
    require_distinct_repositories: Literal[True] = True

    @field_validator("validation_groups")
    @classmethod
    def _unique_groups(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(normalize_signature(value) for value in values)
        if any(not value for value in normalized):
            raise ValueError("validation groups must not be blank")
        if len(normalized) != len(set(normalized)):
            raise ValueError("validation groups must be unique")
        return normalized

    @model_validator(mode="after")
    def _thresholds_fit_preregistered_groups(self) -> PromotionPolicy:
        group_count = len(self.validation_groups)
        if self.canary_minimum_groups > group_count:
            raise ValueError("canary threshold exceeds registered validation groups")
        if self.active_minimum_independent_groups > group_count:
            raise ValueError("active threshold exceeds registered validation groups")
        return self

    @property
    def policy_hash(self) -> str:
        return sha256_json(self)


class ValidationRecord(StrictModel):
    schema_version: Literal[1] = SCHEMA_VERSION
    validation_id: str = Field(pattern=_IDENTIFIER_RE.pattern)
    validation_group: str = Field(min_length=1, max_length=128)
    repository_fingerprint: str = Field(min_length=3, max_length=256)
    run_id: str = Field(pattern=_IDENTIFIER_RE.pattern)
    passed: bool
    harm_count: int = Field(default=0, ge=0)
    effect_lower_bound: float
    confidence_lower_bound: float = Field(ge=0.0, le=1.0)
    evidence_refs: tuple[str, ...] = Field(min_length=1, max_length=64)
    observed_at: datetime = Field(default_factory=utc_now)

    @field_validator("validation_group")
    @classmethod
    def _normalized_group(cls, value: str) -> str:
        return normalize_signature(value)

    @field_validator("evidence_refs")
    @classmethod
    def _evidence_unique(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value.strip() for value in values):
            raise ValueError("evidence refs must not be blank")
        if len(values) != len(set(values)):
            raise ValueError("evidence refs must be unique")
        return values

    @field_validator("observed_at")
    @classmethod
    def _aware_observed_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("observed_at must be timezone-aware")
        return value


class ValidationSummary(StrictModel):
    schema_version: Literal[1] = SCHEMA_VERSION
    registered_groups: tuple[str, ...]
    observed_groups: tuple[str, ...]
    counted_groups: tuple[str, ...]
    passing_groups: tuple[str, ...]
    independent_group_count: int = Field(default=0, ge=0)
    distinct_repositories: int = Field(default=0, ge=0)
    pass_rate: float = Field(ge=0.0, le=1.0)
    total_harm: int = Field(ge=0)
    minimum_effect_lower_bound: float | None
    confidence_lower_bound: float
    meets_canary: bool
    meets_active: bool
    reasons: tuple[str, ...] = ()


class SkillManifest(StrictModel):
    """Canonical governance and content manifest stored by the registry."""

    schema_version: Literal[1] = SCHEMA_VERSION
    skill_id: str = Field(pattern=_IDENTIFIER_RE.pattern)
    version: PositiveInt
    name: str = Field(min_length=1, max_length=128)
    description: str = Field(min_length=3, max_length=2_000)
    candidate_ids: tuple[str, ...] = Field(min_length=1)
    task_signatures: tuple[str, ...] = Field(min_length=1)
    failure_signatures: tuple[str, ...] = Field(min_length=1)
    decision: str = Field(min_length=3, max_length=8_000)
    procedure: tuple[str, ...] = Field(min_length=1, max_length=64)
    failed_attempts: tuple[str, ...] = Field(default=(), max_length=64)
    evidence_refs: tuple[str, ...] = Field(min_length=1)
    source_trace_ids: tuple[str, ...] = Field(min_length=1)
    source_commits: tuple[str, ...] = Field(min_length=1)
    source_code_hashes: tuple[str, ...] = Field(min_length=1)
    scope: SkillScope
    risk_level: RiskLevel
    confidence_prior: float = Field(ge=0.0, le=1.0)
    confidence_lower_bound: float = Field(ge=0.0, le=1.0)
    expires_at: datetime | None
    rollout_state: EvolutionState
    promotion_policy: PromotionPolicy
    validation_ids: tuple[str, ...] = ()
    supersedes_version: PositiveInt | None = None
    estimated_tokens: PositiveInt
    content_hash: str = Field(pattern=_HEX_RE.pattern)
    manifest_hash: str = Field(pattern=_HEX_RE.pattern)
    created_at: datetime
    updated_at: datetime

    @field_validator("created_at", "updated_at", "expires_at")
    @classmethod
    def _aware_manifest_dates(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("manifest timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def _verify_hashes_and_state(self, info: ValidationInfo) -> SkillManifest:
        if isinstance(info.context, dict) and info.context.get("building_manifest"):
            return self
        data = self.model_dump(mode="json")
        if self.content_hash != _manifest_content_hash(data):
            raise ValueError("content_hash does not match canonical skill content")
        if self.manifest_hash != _manifest_hash(data):
            raise ValueError("manifest_hash does not match canonical manifest")
        return self

    @classmethod
    def build(cls, **values: Any) -> SkillManifest:
        # Pydantic's normalized JSON representation (enums, tuples and nested
        # defaults included) is what the validator hashes.  Validate once with
        # placeholders, then compute against that canonical representation.
        raw = dict(values)
        raw.setdefault("schema_version", SCHEMA_VERSION)
        raw["content_hash"] = "0" * 64
        raw["manifest_hash"] = "0" * 64
        provisional = cls.model_validate(raw, context={"building_manifest": True})
        data = provisional.model_dump(mode="json")
        data["content_hash"] = _manifest_content_hash(data)
        data["manifest_hash"] = _manifest_hash(data)
        return cls.model_validate(data)

    def evolve(self, **changes: Any) -> SkillManifest:
        data = self.model_dump(mode="json")
        data.update(_jsonable(changes))
        data.pop("content_hash", None)
        data.pop("manifest_hash", None)
        return type(self).build(**data)

    def to_canonical_json(self) -> str:
        return canonical_json(self)

    def is_expired(self, *, at: datetime | None = None) -> bool:
        when = at or utc_now()
        return self.expires_at is not None and self.expires_at <= when


_CONTENT_FIELDS = (
    "schema_version",
    "skill_id",
    "version",
    "name",
    "description",
    "candidate_ids",
    "task_signatures",
    "failure_signatures",
    "decision",
    "procedure",
    "failed_attempts",
    "evidence_refs",
    "source_trace_ids",
    "source_commits",
    "source_code_hashes",
    "scope",
    "risk_level",
    "expires_at",
    "promotion_policy",
    "supersedes_version",
)


def _manifest_content_hash(data: Mapping[str, Any]) -> str:
    return sha256_json({key: data.get(key) for key in _CONTENT_FIELDS})


def _manifest_hash(data: Mapping[str, Any]) -> str:
    return sha256_json({key: value for key, value in data.items() if key != "manifest_hash"})


class CandidateRecord(StrictModel):
    schema_version: Literal[1] = SCHEMA_VERSION
    candidate: ExperienceCandidate
    disposition: RegistrationDisposition
    eligible_for_promotion: bool
    blocked_reasons: tuple[str, ...] = ()
    duplicate_of: str | None = None
    conflicts_with: tuple[str, ...] = ()
    recorded_at: datetime = Field(default_factory=utc_now)

    @field_validator("recorded_at")
    @classmethod
    def _aware_recorded_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("recorded_at must be timezone-aware")
        return value


class AuditEvent(StrictModel):
    schema_version: Literal[1] = SCHEMA_VERSION
    sequence: PositiveInt
    event_id: str
    event_type: str
    candidate_id: str | None = None
    skill_id: str | None = None
    version: PositiveInt | None = None
    from_state: EvolutionState | None = None
    to_state: EvolutionState | None = None
    actor: str
    payload: dict[str, Any] = Field(default_factory=dict)
    occurred_at: datetime


class RetrievedSkill(StrictModel):
    schema_version: Literal[1] = SCHEMA_VERSION
    manifest: SkillManifest
    markdown: str
    token_cost: PositiveInt
    rank_score: float


class SkillFeedback(StrictModel):
    """Append-only, evidence-bound post-deployment feedback.

    A retrieval hit is an observation and therefore needs no external evidence.
    Claims that a skill helped or harmed a task must name at least one durable
    evidence reference; arbitrary model prose is deliberately not accepted.
    """

    schema_version: Literal[1] = SCHEMA_VERSION
    feedback_id: str = Field(pattern=_IDENTIFIER_RE.pattern)
    skill_id: str = Field(pattern=_IDENTIFIER_RE.pattern)
    version: PositiveInt
    task_id: str = Field(pattern=_IDENTIFIER_RE.pattern)
    outcome: FeedbackOutcome
    evidence_refs: tuple[str, ...] = ()
    actor: str = Field(min_length=1, max_length=128)
    occurred_at: datetime = Field(default_factory=utc_now)

    @field_validator("evidence_refs")
    @classmethod
    def _feedback_evidence_unique(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value.strip() for value in values):
            raise ValueError("feedback evidence refs must not be blank")
        if len(values) != len(set(values)):
            raise ValueError("feedback evidence refs must be unique")
        return values

    @field_validator("occurred_at")
    @classmethod
    def _feedback_time_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("occurred_at must be timezone-aware")
        return value

    @model_validator(mode="after")
    def _effects_require_evidence(self) -> SkillFeedback:
        if self.outcome is not FeedbackOutcome.HIT and not self.evidence_refs:
            raise ValueError("help/harm feedback requires durable evidence_refs")
        return self
