from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


RUN_RESULT_SCHEMA_VERSION = 1
RUN_RESULT_RECORD_TYPE = "run_result"


def _normalise_event(event: dict[str, Any]) -> dict[str, Any]:
    """Return an event body that is valid in both embedded and JSONL form."""

    normalised = dict(event)
    event_type = normalised.get("type")
    if not isinstance(event_type, str) or not event_type.strip():
        normalised["type"] = "event"
    elif event_type == RUN_RESULT_RECORD_TYPE:
        # ``run_result`` is reserved for the final JSONL record.  Keeping the
        # namespace disjoint makes stream consumers able to stop deterministically.
        normalised["type"] = "event"
        normalised.setdefault("event_type", RUN_RESULT_RECORD_TYPE)
    normalised.pop("schema_version", None)
    return normalised


@dataclass(slots=True)
class RunResult:
    status: str
    result: str = ""
    exit_code: int = 0
    provider: str = ""
    model: str = ""
    task_id: str = ""
    trace_id: str = ""
    verdict: str = ""
    evidence_bundle_ref: str = ""
    usage: dict[str, int] = field(default_factory=dict)
    tools: tuple[str, ...] = ()
    evidence: dict[str, object] = field(default_factory=dict)
    capabilities: tuple[str, ...] = ()
    background: dict[str, object] = field(default_factory=dict)
    recovery: list[dict[str, object]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    error: dict[str, Any] = field(default_factory=dict)
    schema_version: int = RUN_RESULT_SCHEMA_VERSION

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "status": self.status,
            "exit_code": self.exit_code,
            "provider": self.provider,
            "model": self.model,
            "task_id": self.task_id,
            "trace_id": self.trace_id,
            "verdict": self.verdict or None,
            "evidence_bundle_ref": self.evidence_bundle_ref or None,
            "result": self.result,
            "usage": dict(self.usage),
            "tools": list(self.tools),
            "evidence": dict(self.evidence),
            "capabilities": list(self.capabilities),
            "background": self.background,
            "recovery": self.recovery,
            "events": [_normalise_event(event) for event in self.events],
            "error": self.error or None,
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), ensure_ascii=False, sort_keys=True)

    def to_jsonl(self) -> str:
        lines = []
        for event in self.events:
            record = _normalise_event(event)
            record["schema_version"] = self.schema_version
            lines.append(json.dumps(record, ensure_ascii=False, sort_keys=True))
        final_record = self.as_dict()
        final_record["type"] = RUN_RESULT_RECORD_TYPE
        lines.append(
            json.dumps(final_record, ensure_ascii=False, sort_keys=True)
        )
        return "\n".join(lines)


__all__ = [
    "RUN_RESULT_RECORD_TYPE",
    "RUN_RESULT_SCHEMA_VERSION",
    "RunResult",
]
