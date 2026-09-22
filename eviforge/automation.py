"""Stable machine-readable execution results and append-only event output."""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any, Literal, TextIO

from pydantic import BaseModel, ConfigDict, Field

RunStatus = Literal["success", "failed", "blocked", "approval_required", "ambiguous", "cancelled"]
EXIT_CODES = {"success": 0, "failed": 1, "config_error": 2, "blocked": 3,
              "approval_required": 3, "ambiguous": 4, "cancelled": 130}


class RunError(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str
    message: str


class RunResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["1.0"] = "1.0"
    run_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    session_id: str = ""
    trace_id: str = ""
    status: RunStatus = "success"
    exit_code: int = 0
    output: str = ""
    errors: list[RunError] = Field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    elapsed_seconds: float = 0.0
    events_path: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def failure(cls, status: RunStatus, code: str, message: str, **kwargs: Any) -> RunResult:
        return cls(status=status, exit_code=EXIT_CODES[status], errors=[RunError(code=code, message=message)], **kwargs)


class RunEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["1.0"] = "1.0"
    run_id: str
    sequence: int
    timestamp: float
    type: str
    data: dict[str, Any] = Field(default_factory=dict)


class EventJournal:
    def __init__(self, path: Path, run_id: str, stream: TextIO | None = None):
        self.path = path
        self.run_id = run_id
        self.stream = stream
        self.sequence = 0
        path.parent.mkdir(parents=True, exist_ok=True)
        # Run IDs are not reused; an existing transcript must never be truncated.
        self._file = path.open("x", encoding="utf-8")

    def emit(self, event: dict[str, Any]) -> None:
        self.sequence += 1
        content = dict(event)
        kind = str(content.pop("type", "event"))
        record = RunEvent(run_id=self.run_id, sequence=self.sequence,
                          timestamp=time.time(), type=kind, data=content)
        line = record.model_dump_json()
        self._file.write(line + "\n")
        self._file.flush()
        if self.stream is not None:
            self.stream.write(line + "\n")
            self.stream.flush()

    def close(self) -> None:
        self._file.close()


def write_result(result: RunResult, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(result.model_dump_json(indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def write_schemas(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for name, model in (("run-result-v1.schema.json", RunResult), ("run-event-v1.schema.json", RunEvent)):
        (directory / name).write_text(json.dumps(model.model_json_schema(), indent=2) + "\n", encoding="utf-8")
