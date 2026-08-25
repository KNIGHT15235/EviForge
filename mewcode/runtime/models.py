"""Versioned trace schemas used by the durable runtime store."""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class TraceEvent(BaseModel):
    """One immutable observation in an EviForge execution trace.

    Optional columns intentionally live in the schema instead of an untyped log
    message so evaluators can compare runs without scraping prose.  Event
    payloads are extension points for event-specific facts.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "1.0"
    event_id: str = Field(default_factory=lambda: f"evt_{uuid.uuid4().hex}")
    trace_id: str
    span_id: str | None = None
    parent_span_id: str | None = None
    task_id: str | None = None
    node_id: str | None = None
    agent_id: str | None = None
    event_type: str
    monotonic_ns: int = Field(default_factory=time.monotonic_ns, ge=0)
    wall_time: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    model: str | None = None
    provider: str | None = None
    prompt_hash: str | None = None
    tool_schema_hash: str | None = None
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cache_tokens: int = Field(default=0, ge=0)
    estimated_cost: float = Field(default=0.0, ge=0.0)

    tool_name: str | None = None
    normalized_args_hash: str | None = None
    workspace_id: str | None = None
    git_sha: str | None = None
    diff_hash: str | None = None
    risk_decision: str | None = None
    approval_id: str | None = None
    artifact_refs: tuple[str, ...] = ()
    status: str | None = None
    error_class: str | None = None
    failure_signature: str | None = None
    redaction_metadata: dict[str, Any] = Field(default_factory=dict)
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("schema_version", "event_id", "trace_id", "event_type")
    @classmethod
    def _must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value

    @field_validator("wall_time")
    @classmethod
    def _must_be_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("wall_time must be timezone-aware")
        return value

    def canonical_json(self) -> str:
        """Return stable compact JSON used by SQLite and JSONL exports."""

        # Pydantic performs JSON-mode conversion for datetime and tuples.  The
        # second validation prevents a caller from hiding a non-JSON object in
        # an Any payload.
        import json

        data = self.model_dump(mode="json")
        encoded = json.dumps(
            data, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        json.loads(encoded)
        return encoded


class TraceRecord(BaseModel):
    """A TraceEvent together with its durable, monotonically increasing order."""

    model_config = ConfigDict(frozen=True)

    sequence: int = Field(ge=1)
    event: TraceEvent
