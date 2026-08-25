"""SQLite authority for TaskRun state and append-only execution traces."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from mewcode.runtime.fsm import (
    ConcurrentTransitionError,
    TaskRun,
    TaskState,
    utc_now,
)
from mewcode.runtime.models import TraceEvent, TraceRecord
from mewcode.runtime.paths import ControlPlanePaths


class RuntimeStoreError(RuntimeError):
    pass


class DuplicateEventError(RuntimeStoreError):
    def __init__(self, event_id: str) -> None:
        self.event_id = event_id
        super().__init__(f"TraceEvent already exists: {event_id}")


class ExportCorruptionError(RuntimeStoreError):
    pass


@dataclass(frozen=True, slots=True)
class ExportResult:
    exported_events: int
    paths: tuple[Path, ...]
    last_sequence: int


_SCHEMA = """
CREATE TABLE IF NOT EXISTS runtime_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_runs (
    task_id TEXT PRIMARY KEY,
    trace_id TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version >= 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trace_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    schema_version TEXT NOT NULL,
    trace_id TEXT NOT NULL,
    span_id TEXT,
    parent_span_id TEXT,
    task_id TEXT,
    node_id TEXT,
    agent_id TEXT,
    event_type TEXT NOT NULL,
    wall_time TEXT NOT NULL,
    monotonic_ns INTEGER NOT NULL,
    body_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_trace_events_trace_sequence
    ON trace_events(trace_id, sequence);
CREATE INDEX IF NOT EXISTS idx_trace_events_task_sequence
    ON trace_events(task_id, sequence);
CREATE INDEX IF NOT EXISTS idx_trace_events_type_sequence
    ON trace_events(event_type, sequence);

CREATE TABLE IF NOT EXISTS trace_outbox (
    sequence INTEGER PRIMARY KEY,
    event_id TEXT NOT NULL UNIQUE,
    trace_id TEXT NOT NULL,
    wall_time TEXT NOT NULL,
    body_json TEXT NOT NULL,
    FOREIGN KEY(sequence) REFERENCES trace_events(sequence)
);

CREATE TABLE IF NOT EXISTS export_cursors (
    cursor_name TEXT PRIMARY KEY,
    last_sequence INTEGER NOT NULL CHECK (last_sequence >= 0),
    updated_at TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS trace_events_no_update
BEFORE UPDATE ON trace_events
BEGIN
    SELECT RAISE(ABORT, 'trace_events is append-only');
END;
CREATE TRIGGER IF NOT EXISTS trace_events_no_delete
BEFORE DELETE ON trace_events
BEGIN
    SELECT RAISE(ABORT, 'trace_events is append-only');
END;
CREATE TRIGGER IF NOT EXISTS trace_outbox_no_update
BEFORE UPDATE ON trace_outbox
BEGIN
    SELECT RAISE(ABORT, 'trace_outbox is append-only');
END;
CREATE TRIGGER IF NOT EXISTS trace_outbox_no_delete
BEFORE DELETE ON trace_outbox
BEGIN
    SELECT RAISE(ABORT, 'trace_outbox is append-only');
END;
"""


def _iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.isoformat(timespec="microseconds")


class RuntimeStore:
    """Own a workspace's durable state database.

    A state transition updates the TaskRun projection and appends both a trace
    row and its outbox row in one ``BEGIN IMMEDIATE`` transaction.  JSONL is an
    asynchronous, retry-safe projection and never participates in authority.
    """

    def __init__(
        self,
        *,
        control_root: str | os.PathLike[str] | None = None,
        workspace_id: str = "default",
        synchronous: str = "FULL",
        busy_timeout_ms: int = 5_000,
    ) -> None:
        synchronous = synchronous.upper()
        if synchronous not in {"OFF", "NORMAL", "FULL", "EXTRA"}:
            raise ValueError("synchronous must be OFF, NORMAL, FULL, or EXTRA")
        if busy_timeout_ms < 0:
            raise ValueError("busy_timeout_ms must be non-negative")

        self.paths = ControlPlanePaths.build(
            control_root=control_root, workspace_id=workspace_id
        )
        self.paths.ensure_directories()
        self._lock = threading.RLock()
        self._closed = False
        self._connection = sqlite3.connect(
            self.paths.database,
            isolation_level=None,
            check_same_thread=False,
            timeout=max(busy_timeout_ms / 1000, 0.001),
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._connection.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
        journal_mode = self._connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        if str(journal_mode).lower() != "wal":
            self._connection.close()
            raise RuntimeStoreError(f"SQLite WAL unavailable: {journal_mode}")
        self._connection.execute(f"PRAGMA synchronous={synchronous}")
        self._connection.executescript(_SCHEMA)
        with self._transaction() as connection:
            connection.executemany(
                "INSERT OR REPLACE INTO runtime_meta(key, value) VALUES (?, ?)",
                (
                    ("schema_version", "1"),
                    ("journal_mode", "WAL"),
                    ("synchronous", synchronous),
                ),
            )

    @property
    def db_path(self) -> Path:
        return self.paths.database

    def __enter__(self) -> RuntimeStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._connection.close()
                self._closed = True

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeStoreError("RuntimeStore is closed")

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._require_open()
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield self._connection
            except BaseException:
                self._connection.rollback()
                raise
            else:
                self._connection.commit()

    @staticmethod
    def _task_from_row(row: sqlite3.Row) -> TaskRun:
        return TaskRun(
            task_id=row["task_id"],
            trace_id=row["trace_id"],
            state=TaskState(row["state"]),
            version=row["version"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            metadata=json.loads(row["metadata_json"]),
        )

    @staticmethod
    def _insert_event(connection: sqlite3.Connection, event: TraceEvent) -> int:
        body = event.canonical_json()
        try:
            cursor = connection.execute(
                """
                INSERT INTO trace_events(
                    event_id, schema_version, trace_id, span_id, parent_span_id,
                    task_id, node_id, agent_id, event_type, wall_time,
                    monotonic_ns, body_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.event_id,
                    event.schema_version,
                    event.trace_id,
                    event.span_id,
                    event.parent_span_id,
                    event.task_id,
                    event.node_id,
                    event.agent_id,
                    event.event_type,
                    _iso(event.wall_time),
                    event.monotonic_ns,
                    body,
                ),
            )
        except sqlite3.IntegrityError as exc:
            if "event_id" in str(exc) or "UNIQUE constraint" in str(exc):
                raise DuplicateEventError(event.event_id) from exc
            raise
        sequence = int(cursor.lastrowid)
        connection.execute(
            """
            INSERT INTO trace_outbox(sequence, event_id, trace_id, wall_time, body_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (sequence, event.event_id, event.trace_id, _iso(event.wall_time), body),
        )
        return sequence

    def create_task(
        self,
        *,
        task_id: str | None = None,
        trace_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> TaskRun:
        run = TaskRun.new(task_id=task_id, trace_id=trace_id, metadata=metadata)
        metadata_json = json.dumps(
            run.metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        event = TraceEvent(
            trace_id=run.trace_id,
            task_id=run.task_id,
            event_type="task_state_changed",
            wall_time=run.created_at,
            status=run.state.value,
            payload={
                "previous_state": None,
                "new_state": run.state.value,
                "task_version": run.version,
                "reason": "task_created",
            },
        )
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO task_runs(
                    task_id, trace_id, state, version, created_at, updated_at,
                    metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run.task_id,
                    run.trace_id,
                    run.state.value,
                    run.version,
                    _iso(run.created_at),
                    _iso(run.updated_at),
                    metadata_json,
                ),
            )
            self._insert_event(connection, event)
        return run

    def get_task(self, task_id: str) -> TaskRun | None:
        with self._lock:
            self._require_open()
            row = self._connection.execute(
                "SELECT * FROM task_runs WHERE task_id = ?", (task_id,)
            ).fetchone()
        return None if row is None else self._task_from_row(row)

    def list_tasks(self, *, limit: int | None = None) -> list[TaskRun]:
        if limit is not None and limit < 1:
            raise ValueError("limit must be positive")
        sql = "SELECT * FROM task_runs ORDER BY created_at, task_id"
        params: tuple[Any, ...] = ()
        if limit is not None:
            sql += " LIMIT ?"
            params = (limit,)
        with self._lock:
            self._require_open()
            rows = self._connection.execute(sql, params).fetchall()
        return [self._task_from_row(row) for row in rows]

    def transition_task(
        self,
        task_id: str,
        requested: TaskState | str,
        *,
        expected_version: int | None = None,
        reason: str | None = None,
        actor: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> TaskRun:
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM task_runs WHERE task_id = ?", (task_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown TaskRun: {task_id}")
            current = self._task_from_row(row)
            if expected_version is not None and current.version != expected_version:
                raise ConcurrentTransitionError(
                    task_id, expected_version, current.version
                )
            updated = current.transitioned(requested)
            cursor = connection.execute(
                """
                UPDATE task_runs
                SET state = ?, version = ?, updated_at = ?
                WHERE task_id = ? AND version = ?
                """,
                (
                    updated.state.value,
                    updated.version,
                    _iso(updated.updated_at),
                    task_id,
                    current.version,
                ),
            )
            if cursor.rowcount != 1:
                actual_row = connection.execute(
                    "SELECT version FROM task_runs WHERE task_id = ?", (task_id,)
                ).fetchone()
                actual = -1 if actual_row is None else int(actual_row["version"])
                raise ConcurrentTransitionError(task_id, current.version, actual)

            payload: dict[str, Any] = {
                "previous_state": current.state.value,
                "new_state": updated.state.value,
                "task_version": updated.version,
            }
            if reason is not None:
                payload["reason"] = reason
            if actor is not None:
                payload["actor"] = actor
            if details:
                payload["details"] = dict(details)
            self._insert_event(
                connection,
                TraceEvent(
                    trace_id=updated.trace_id,
                    task_id=updated.task_id,
                    event_type="task_state_changed",
                    wall_time=updated.updated_at,
                    status=updated.state.value,
                    payload=payload,
                ),
            )
        return updated

    def append_event(self, event: TraceEvent) -> TraceRecord:
        """Atomically append a TraceEvent and its immutable outbox entry."""

        with self._transaction() as connection:
            sequence = self._insert_event(connection, event)
        # Validate from canonical form to detach storage from caller-owned dicts.
        detached = TraceEvent.model_validate_json(event.canonical_json())
        return TraceRecord(sequence=sequence, event=detached)

    append_trace_event = append_event

    def list_events(
        self,
        *,
        trace_id: str | None = None,
        task_id: str | None = None,
        after_sequence: int = 0,
        limit: int | None = None,
    ) -> list[TraceRecord]:
        if after_sequence < 0:
            raise ValueError("after_sequence must be non-negative")
        if limit is not None and limit < 1:
            raise ValueError("limit must be positive")
        conditions = ["sequence > ?"]
        params: list[Any] = [after_sequence]
        if trace_id is not None:
            conditions.append("trace_id = ?")
            params.append(trace_id)
        if task_id is not None:
            conditions.append("task_id = ?")
            params.append(task_id)
        sql = (
            "SELECT sequence, body_json FROM trace_events WHERE "
            + " AND ".join(conditions)
            + " ORDER BY sequence"
        )
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        with self._lock:
            self._require_open()
            rows = self._connection.execute(sql, params).fetchall()
        return [
            TraceRecord(
                sequence=row["sequence"],
                event=TraceEvent.model_validate_json(row["body_json"]),
            )
            for row in rows
        ]

    list_trace_events = list_events

    def outbox_size(self) -> int:
        with self._lock:
            self._require_open()
            return int(
                self._connection.execute("SELECT COUNT(*) FROM trace_outbox").fetchone()[0]
            )

    @staticmethod
    def _cursor_name(destination: Path | None) -> str:
        if destination is None:
            return "default-trace-tree"
        absolute = str(destination.expanduser().resolve())
        return "file-" + hashlib.sha256(absolute.encode("utf-8")).hexdigest()

    @staticmethod
    def _existing_event_ids(path: Path) -> set[str]:
        if not path.exists():
            return set()
        event_ids: set[str] = set()
        try:
            with path.open("r", encoding="utf-8") as stream:
                for line_number, line in enumerate(stream, start=1):
                    if not line.strip():
                        continue
                    value = json.loads(line)
                    event_id = value.get("event_id")
                    if not isinstance(event_id, str) or not event_id:
                        raise ValueError("event_id is missing")
                    event_ids.add(event_id)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise ExportCorruptionError(
                f"cannot safely append to malformed JSONL {path}"
            ) from exc
        return event_ids

    @staticmethod
    def _append_jsonl(path: Path, bodies: Sequence[tuple[str, str]]) -> int:
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = RuntimeStore._existing_event_ids(path)
        missing = [(event_id, body) for event_id, body in bodies if event_id not in existing]
        if not missing:
            return 0
        with path.open("ab") as stream:
            for _, body in missing:
                stream.write(body.encode("utf-8"))
                stream.write(b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        return len(missing)

    def export_jsonl(
        self,
        destination: str | os.PathLike[str] | None = None,
        *,
        limit: int | None = None,
    ) -> ExportResult:
        """Append pending outbox events to JSONL and durably advance a cursor.

        With no destination, events are split into ``YYYY-MM/trace-id.jsonl``.
        A destination is useful for a single eval artifact.  If a process dies
        after fsync but before cursor commit, a retry scans event IDs and does
        not duplicate lines.
        """

        if limit is not None and limit < 1:
            raise ValueError("limit must be positive")
        target = None if destination is None else Path(destination).resolve()
        cursor_name = self._cursor_name(target)

        # Keep a SQLite write transaction open while projecting the selected
        # batch.  Besides protecting this connection, BEGIN IMMEDIATE also
        # serializes exporters in other processes that share the same runtime
        # database.  A crash rolls the cursor back; _append_jsonl then removes
        # the ambiguity by recognizing already-fsynced event IDs on retry.
        with self._transaction() as connection:
            cursor_row = connection.execute(
                "SELECT last_sequence FROM export_cursors WHERE cursor_name = ?",
                (cursor_name,),
            ).fetchone()
            last_sequence = 0 if cursor_row is None else int(cursor_row[0])
            sql = (
                "SELECT sequence, event_id, trace_id, wall_time, body_json "
                "FROM trace_outbox WHERE sequence > ? ORDER BY sequence"
            )
            params: list[Any] = [last_sequence]
            if limit is not None:
                sql += " LIMIT ?"
                params.append(limit)
            rows = connection.execute(sql, params).fetchall()

            if not rows:
                return ExportResult(0, (), last_sequence)

            grouped: dict[Path, list[tuple[str, str]]] = {}
            for row in rows:
                path = target or self.paths.trace_jsonl(
                    row["trace_id"], row["wall_time"]
                )
                grouped.setdefault(path, []).append(
                    (row["event_id"], row["body_json"])
                )

            written = 0
            for path, bodies in grouped.items():
                written += self._append_jsonl(path, bodies)

            new_last_sequence = int(rows[-1]["sequence"])
            connection.execute(
                """
                INSERT INTO export_cursors(cursor_name, last_sequence, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(cursor_name) DO UPDATE SET
                    last_sequence = excluded.last_sequence,
                    updated_at = excluded.updated_at
                WHERE export_cursors.last_sequence < excluded.last_sequence
                """,
                (cursor_name, new_last_sequence, _iso(utc_now())),
            )

        return ExportResult(
            exported_events=written,
            paths=tuple(sorted(grouped, key=lambda path: str(path))),
            last_sequence=new_last_sequence,
        )
