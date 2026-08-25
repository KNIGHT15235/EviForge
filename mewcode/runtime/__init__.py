"""Durable runtime primitives for EviForge.

The package deliberately has no dependency on the interactive application.  It
can therefore be shared by the TUI, a headless evaluator, and recovery tools.
"""

from mewcode.runtime.fsm import (
    ALLOWED_TRANSITIONS,
    FINAL_STATES,
    ConcurrentTransitionError,
    InvalidTransitionError,
    TaskRun,
    TaskState,
)
from mewcode.runtime.models import TraceEvent, TraceRecord
from mewcode.runtime.kernel import TaskRuntime
from mewcode.runtime.builder import RuntimeBuilder, RuntimeComponents, workspace_id_for
from mewcode.runtime.paths import ControlPlanePaths, resolve_control_root
from mewcode.runtime.data_manager import DataStats, RuntimeDataManager
from mewcode.runtime.store import (
    DuplicateEventError,
    ExportCorruptionError,
    ExportResult,
    RuntimeStore,
    RuntimeStoreError,
)

__all__ = [
    "ALLOWED_TRANSITIONS",
    "FINAL_STATES",
    "ConcurrentTransitionError",
    "ControlPlanePaths",
    "DuplicateEventError",
    "DataStats",
    "ExportCorruptionError",
    "ExportResult",
    "InvalidTransitionError",
    "RuntimeStore",
    "RuntimeStoreError",
    "RuntimeBuilder",
    "RuntimeComponents",
    "RuntimeDataManager",
    "TaskRun",
    "TaskRuntime",
    "TaskState",
    "TraceEvent",
    "TraceRecord",
    "resolve_control_root",
    "workspace_id_for",
]
