"""Stable progress events for DAG embedders and JSONL consumers."""

from __future__ import annotations

import inspect
import json
from collections.abc import Awaitable, Callable, Iterable
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, TextIO

from pydantic import BaseModel, ConfigDict, Field

from .models import NodeStatus


class DAGProgressEventType(StrEnum):
    NODE_STARTED = "node_started"
    NODE_COMPLETED = "node_completed"
    NODE_FAILED = "node_failed"
    BUDGET = "budget"


class DAGBudgetSnapshot(BaseModel):
    """Budget state visible to the scheduler at one event boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    total_tokens: int = Field(ge=1)
    actual_tokens_used: int = Field(ge=0)
    reserved_tokens: int = Field(ge=0)
    available_tokens: int = Field(ge=0)
    overrun_tokens: int = Field(ge=0)
    enforcement: str = "reservation_and_completion_reconciliation"
    in_flight_usage_known: bool = False


class DAGProgressEvent(BaseModel):
    """One versioned scheduler transition suitable for JSONL output."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "1.0"
    sequence: int = Field(ge=1)
    type: DAGProgressEventType
    run_id: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    node_id: str | None = None
    status: NodeStatus | None = None
    message: str | None = None
    completed_nodes: int = Field(ge=0)
    total_nodes: int = Field(ge=1)
    budget: DAGBudgetSnapshot

    def machine_readable(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


ProgressCallback = Callable[
    [DAGProgressEvent], Awaitable[None] | None
]


def jsonl_progress_callback(stream: TextIO) -> ProgressCallback:
    """Return a callback that writes exactly one JSON object per event."""

    def write(event: DAGProgressEvent) -> None:
        print(
            json.dumps(
                event.machine_readable(), ensure_ascii=False, sort_keys=True
            ),
            file=stream,
            flush=True,
        )

    return write


def combine_progress_callbacks(
    callbacks: Iterable[ProgressCallback | None],
) -> ProgressCallback | None:
    """Combine sync/async callbacks while preserving their declared order."""

    active = tuple(callback for callback in callbacks if callback is not None)
    if not active:
        return None

    async def combined(event: DAGProgressEvent) -> None:
        for callback in active:
            result = callback(event)
            if inspect.isawaitable(result):
                await result

    return combined


__all__ = [
    "DAGBudgetSnapshot",
    "DAGProgressEvent",
    "DAGProgressEventType",
    "ProgressCallback",
    "combine_progress_callbacks",
    "jsonl_progress_callback",
]
