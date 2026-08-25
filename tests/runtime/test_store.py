from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from mewcode.runtime import (
    ConcurrentTransitionError,
    DuplicateEventError,
    InvalidTransitionError,
    RuntimeStore,
    TaskState,
    TraceEvent,
)


def test_create_and_transition_are_durable_and_transactional(tmp_path: Path) -> None:
    with RuntimeStore(control_root=tmp_path, workspace_id="repo") as store:
        created = store.create_task(
            task_id="task-1", trace_id="trace-1", metadata={"request": "fix"}
        )
        planning = store.transition_task(
            created.task_id, TaskState.CONTRACT_READY, expected_version=0
        )
        planning = store.transition_task(
            created.task_id, TaskState.PLANNING, expected_version=planning.version
        )
        assert store.outbox_size() == 3
        database = store.db_path

    with RuntimeStore(control_root=tmp_path, workspace_id="repo") as reopened:
        restored = reopened.get_task("task-1")
        assert restored is not None
        assert restored.state is TaskState.PLANNING
        assert restored.version == 2
        assert restored.metadata == {"request": "fix"}
        records = reopened.list_events(task_id="task-1")
        assert [record.event.status for record in records] == [
            "RECEIVED",
            "CONTRACT_READY",
            "PLANNING",
        ]
        assert reopened.db_path == database


def test_illegal_or_stale_transition_appends_nothing(tmp_path: Path) -> None:
    with RuntimeStore(control_root=tmp_path, workspace_id="repo") as store:
        run = store.create_task(task_id="task-1", trace_id="trace-1")

        with pytest.raises(InvalidTransitionError):
            store.transition_task(run.task_id, TaskState.EXECUTING)
        with pytest.raises(ConcurrentTransitionError):
            store.transition_task(
                run.task_id, TaskState.CONTRACT_READY, expected_version=99
            )

        assert store.get_task(run.task_id) == run
        assert len(store.list_events(task_id=run.task_id)) == 1
        assert store.outbox_size() == 1


def test_arbitrary_event_and_outbox_are_append_only(tmp_path: Path) -> None:
    event = TraceEvent(
        event_id="evt-one", trace_id="trace-1", event_type="checkpoint_written"
    )
    with RuntimeStore(control_root=tmp_path, workspace_id="repo") as store:
        record = store.append_event(event)
        assert record.sequence == 1
        with pytest.raises(DuplicateEventError):
            store.append_event(event)
        assert store.outbox_size() == 1
        database = store.db_path

    connection = sqlite3.connect(database)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("UPDATE trace_events SET event_type = 'changed'")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM trace_outbox")
    finally:
        connection.close()


def test_jsonl_export_is_ordered_idempotent_and_resumes_after_reopen(
    tmp_path: Path,
) -> None:
    export_file = tmp_path / "exports" / "run.jsonl"
    with RuntimeStore(control_root=tmp_path / "control", workspace_id="repo") as store:
        store.create_task(task_id="task-1", trace_id="trace-1")
        store.transition_task("task-1", TaskState.CONTRACT_READY)

        first = store.export_jsonl(export_file)
        second = store.export_jsonl(export_file)

        assert first.exported_events == 2
        assert first.last_sequence == 2
        assert second.exported_events == 0

    with RuntimeStore(control_root=tmp_path / "control", workspace_id="repo") as store:
        store.transition_task("task-1", TaskState.PLANNING)
        third = store.export_jsonl(export_file)
        assert third.exported_events == 1
        assert third.last_sequence == 3

    lines = [json.loads(line) for line in export_file.read_text(encoding="utf-8").splitlines()]
    assert [line["status"] for line in lines] == [
        "RECEIVED",
        "CONTRACT_READY",
        "PLANNING",
    ]
    assert len({line["event_id"] for line in lines}) == 3


def test_default_export_separates_trace_files(tmp_path: Path) -> None:
    with RuntimeStore(control_root=tmp_path, workspace_id="repo") as store:
        store.append_event(
            TraceEvent(trace_id="trace-a", event_type="verification_started")
        )
        store.append_event(
            TraceEvent(trace_id="trace-b", event_type="verification_started")
        )

        result = store.export_jsonl()

        assert result.exported_events == 2
        assert len(result.paths) == 2
        assert all(path.is_file() for path in result.paths)
        assert all(store.paths.traces_dir in path.parents for path in result.paths)
