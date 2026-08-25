from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping


class ExecutionStatus(str, Enum):
    SUCCEEDED = "succeeded"
    TOOL_ERROR = "tool_error"
    VALIDATION_ERROR = "validation_error"
    PERMISSION_DENIED = "permission_denied"
    APPROVAL_REQUIRED = "approval_required"
    INTERNAL_ERROR = "internal_error"


class TraceStage(str, Enum):
    RECEIVED = "received"
    VALIDATED = "validated"
    VALIDATION_FAILED = "validation_failed"
    RISK_ASSESSED = "risk_assessed"
    PERMISSION_DECIDED = "permission_decided"
    STARTED = "started"
    COMPLETED = "completed"


@dataclass(frozen=True, slots=True)
class ToolInvocation:
    """Canonical input to the execution layer.

    ``arguments_hash`` is safe to bind to trace and future approval tickets;
    trace events deliberately carry argument keys rather than raw values.
    """

    tool_name: str
    arguments: Mapping[str, Any]
    invocation_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    actor: str = "agent"
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def arguments_hash(self) -> str:
        encoded = json.dumps(
            dict(self.arguments),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class ExecutionTraceEvent:
    invocation_id: str
    tool_name: str
    stage: TraceStage
    timestamp: float = field(default_factory=time.time)
    risk_level: str | None = None
    reason_codes: tuple[str, ...] = ()
    status: ExecutionStatus | None = None
    argument_keys: tuple[str, ...] = ()
    # Hash of the normalized argument object.  Values themselves are never
    # copied into traces, so credentials and file contents stay out of the
    # durable control plane while approvals remain auditable.
    arguments_hash: str = ""


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    invocation_id: str
    tool_name: str
    status: ExecutionStatus
    output: str
    is_error: bool
    executed: bool
    risk_level: str | None
    reason_codes: tuple[str, ...]
    duration_ms: float
    arguments_hash: str
    exception_type: str | None = None

    @property
    def approval_required(self) -> bool:
        return self.status is ExecutionStatus.APPROVAL_REQUIRED
