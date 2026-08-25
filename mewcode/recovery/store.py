"""SQLite-backed approval tickets, action journal, and recovery scanner.

The database is authoritative; repository files and human-readable logs are
only projections.  SQLite ``BEGIN IMMEDIATE`` plus compare-and-swap predicates
make ticket reservations and fencing tokens safe across processes.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from mewcode.recovery.canonical import (
    MISSING_FILE_HASH,
    bytes_sha256,
    canonical_json_hash,
    canonical_realpath,
    file_sha256,
)
from mewcode.recovery.models import (
    ActionRecord,
    ActionState,
    ApprovalTicket,
    AttemptLease,
    BindingMismatchError,
    Checkpoint,
    ConcurrentReservationError,
    EffectKind,
    FencingConflictError,
    InvalidActionTransition,
    JournalEntry,
    PreconditionsFailedError,
    RecoveryItem,
    RecoveryReport,
    SimulatedCrash,
    TicketState,
    TicketUnavailableError,
    UncertainActionError,
)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS recovery_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recovery_actions (
    action_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    action_type TEXT NOT NULL,
    state TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    normalized_args_hash TEXT NOT NULL,
    cwd_realpath TEXT NOT NULL,
    plan_hash TEXT NOT NULL,
    expected_pre_state_hash TEXT NOT NULL,
    effect_kind TEXT NOT NULL,
    target_realpath TEXT,
    expected_old_hash TEXT,
    desired_hash TEXT,
    temp_path TEXT,
    postcondition_json TEXT NOT NULL,
    ticket_id TEXT,
    attempt_id TEXT,
    fencing_generation INTEGER NOT NULL DEFAULT 0 CHECK(fencing_generation >= 0),
    version INTEGER NOT NULL DEFAULT 0 CHECK(version >= 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    error TEXT,
    reconciled INTEGER NOT NULL DEFAULT 0 CHECK(reconciled IN (0, 1)),
    UNIQUE(task_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_recovery_actions_state
    ON recovery_actions(state, updated_at);
CREATE INDEX IF NOT EXISTS idx_recovery_actions_task
    ON recovery_actions(task_id, created_at);

CREATE TABLE IF NOT EXISTS recovery_approval_tickets (
    ticket_id TEXT PRIMARY KEY,
    action_id TEXT NOT NULL,
    normalized_args_hash TEXT NOT NULL,
    cwd_realpath TEXT NOT NULL,
    plan_hash TEXT NOT NULL,
    expected_pre_state_hash TEXT NOT NULL,
    approver TEXT NOT NULL,
    issued_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    max_uses INTEGER NOT NULL CHECK(max_uses > 0),
    use_count INTEGER NOT NULL DEFAULT 0 CHECK(use_count >= 0),
    state TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 0 CHECK(version >= 0),
    reservation_token TEXT,
    reserved_at TEXT,
    FOREIGN KEY(action_id) REFERENCES recovery_actions(action_id)
);
CREATE INDEX IF NOT EXISTS idx_recovery_tickets_state_expiry
    ON recovery_approval_tickets(state, expires_at);

CREATE TABLE IF NOT EXISTS recovery_action_journal (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    action_id TEXT NOT NULL,
    from_state TEXT,
    to_state TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    attempt_id TEXT,
    fencing_generation INTEGER NOT NULL,
    reason TEXT NOT NULL,
    details_json TEXT NOT NULL,
    FOREIGN KEY(action_id) REFERENCES recovery_actions(action_id)
);
CREATE INDEX IF NOT EXISTS idx_recovery_journal_action
    ON recovery_action_journal(action_id, sequence);

CREATE TABLE IF NOT EXISTS recovery_checkpoints (
    checkpoint_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    label TEXT NOT NULL,
    created_at TEXT NOT NULL,
    action_id TEXT,
    journal_sequence INTEGER NOT NULL CHECK(journal_sequence >= 0),
    payload_json TEXT NOT NULL,
    FOREIGN KEY(action_id) REFERENCES recovery_actions(action_id)
);
CREATE INDEX IF NOT EXISTS idx_recovery_checkpoints_task
    ON recovery_checkpoints(task_id, created_at);

CREATE TRIGGER IF NOT EXISTS recovery_journal_no_update
BEFORE UPDATE ON recovery_action_journal
BEGIN
    SELECT RAISE(ABORT, 'recovery_action_journal is append-only');
END;
CREATE TRIGGER IF NOT EXISTS recovery_journal_no_delete
BEFORE DELETE ON recovery_action_journal
BEGIN
    SELECT RAISE(ABORT, 'recovery_action_journal is append-only');
END;
"""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return _aware(value).isoformat(timespec="microseconds")


def _from_iso(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)


def _nonblank(value: str, label: str) -> str:
    if not value or not value.strip():
        raise ValueError(f"{label} must not be blank")
    return value


class RecoveryStore:
    """Own the durable action/recovery control plane for one workspace."""

    def __init__(
        self,
        *,
        control_root: str | os.PathLike[str] | None = None,
        workspace_id: str = "default",
        database_path: str | os.PathLike[str] | None = None,
        synchronous: str = "FULL",
        busy_timeout_ms: int = 5_000,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        synchronous = synchronous.upper()
        if synchronous not in {"OFF", "NORMAL", "FULL", "EXTRA"}:
            raise ValueError("invalid SQLite synchronous mode")
        if database_path is not None:
            database = Path(database_path).expanduser().resolve()
        else:
            # Importing the path resolver does not couple schemas or stores; it
            # only keeps EviForge's host-owned control-plane convention shared.
            from mewcode.runtime.paths import ControlPlanePaths

            database = ControlPlanePaths.build(
                control_root=control_root, workspace_id=workspace_id
            ).database
        database.parent.mkdir(parents=True, exist_ok=True)
        self._database = database
        self._clock = clock
        self._lock = threading.RLock()
        self._closed = False
        self._connection = sqlite3.connect(
            database,
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
            raise RuntimeError(f"SQLite WAL unavailable: {journal_mode}")
        self._connection.execute(f"PRAGMA synchronous={synchronous}")
        self._connection.executescript(_SCHEMA)
        with self._transaction() as connection:
            connection.executemany(
                "INSERT OR REPLACE INTO recovery_meta(key, value) VALUES (?, ?)",
                (("schema_version", "1"), ("journal_mode", "WAL")),
            )

    @property
    def db_path(self) -> Path:
        return self._database

    def __enter__(self) -> RecoveryStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._connection.close()
                self._closed = True

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            if self._closed:
                raise RuntimeError("RecoveryStore is closed")
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield self._connection
            except BaseException:
                self._connection.rollback()
                raise
            else:
                self._connection.commit()

    def _now(self) -> datetime:
        return _aware(self._clock())

    @staticmethod
    def _action_from_row(row: sqlite3.Row) -> ActionRecord:
        return ActionRecord(
            action_id=row["action_id"],
            task_id=row["task_id"],
            action_type=row["action_type"],
            state=ActionState(row["state"]),
            idempotency_key=row["idempotency_key"],
            normalized_args_hash=row["normalized_args_hash"],
            cwd_realpath=row["cwd_realpath"],
            plan_hash=row["plan_hash"],
            expected_pre_state_hash=row["expected_pre_state_hash"],
            effect_kind=EffectKind(row["effect_kind"]),
            target_realpath=row["target_realpath"],
            expected_old_hash=row["expected_old_hash"],
            desired_hash=row["desired_hash"],
            temp_path=row["temp_path"],
            postcondition=json.loads(row["postcondition_json"]),
            ticket_id=row["ticket_id"],
            attempt_id=row["attempt_id"],
            fencing_generation=int(row["fencing_generation"]),
            version=int(row["version"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            started_at=_from_iso(row["started_at"]),
            finished_at=_from_iso(row["finished_at"]),
            error=row["error"],
            reconciled=bool(row["reconciled"]),
        )

    @staticmethod
    def _ticket_from_row(row: sqlite3.Row) -> ApprovalTicket:
        return ApprovalTicket(
            ticket_id=row["ticket_id"],
            action_id=row["action_id"],
            normalized_args_hash=row["normalized_args_hash"],
            cwd_realpath=row["cwd_realpath"],
            plan_hash=row["plan_hash"],
            expected_pre_state_hash=row["expected_pre_state_hash"],
            approver=row["approver"],
            issued_at=datetime.fromisoformat(row["issued_at"]),
            expires_at=datetime.fromisoformat(row["expires_at"]),
            max_uses=int(row["max_uses"]),
            use_count=int(row["use_count"]),
            state=TicketState(row["state"]),
            version=int(row["version"]),
            reservation_token=row["reservation_token"],
            reserved_at=_from_iso(row["reserved_at"]),
        )

    @staticmethod
    def _append_journal(
        connection: sqlite3.Connection,
        *,
        action_id: str,
        from_state: ActionState | None,
        to_state: ActionState,
        occurred_at: datetime,
        attempt_id: str | None,
        fencing_generation: int,
        reason: str,
        details: Mapping[str, Any] | None = None,
    ) -> int:
        cursor = connection.execute(
            """
            INSERT INTO recovery_action_journal(
                action_id, from_state, to_state, occurred_at, attempt_id,
                fencing_generation, reason, details_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                action_id,
                None if from_state is None else from_state.value,
                to_state.value,
                _iso(occurred_at),
                attempt_id,
                fencing_generation,
                reason,
                json.dumps(
                    dict(details or {}),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            ),
        )
        return int(cursor.lastrowid)

    def prepare_action(
        self,
        *,
        task_id: str,
        action_type: str,
        idempotency_key: str,
        normalized_args_hash: str,
        cwd: str | os.PathLike[str],
        plan_hash: str,
        expected_pre_state_hash: str,
        effect_kind: EffectKind | str,
        action_id: str | None = None,
        target: str | os.PathLike[str] | None = None,
        expected_old_hash: str | None = None,
        desired_hash: str | None = None,
        temp_path: str | os.PathLike[str] | None = None,
        postcondition: Mapping[str, Any] | None = None,
    ) -> ActionRecord:
        """Persist PREPARED before any irreversible side effect."""

        action_id = action_id or f"act_{uuid.uuid4().hex}"
        values = {
            "task_id": task_id,
            "action_type": action_type,
            "idempotency_key": idempotency_key,
            "normalized_args_hash": normalized_args_hash,
            "plan_hash": plan_hash,
            "expected_pre_state_hash": expected_pre_state_hash,
        }
        for label, value in values.items():
            _nonblank(value, label)
        effect = EffectKind(effect_kind)
        target_realpath = None if target is None else canonical_realpath(target)
        temp_realpath = None if temp_path is None else canonical_realpath(temp_path)
        if effect is EffectKind.FILE_REPLACE:
            if not target_realpath or not expected_old_hash or not desired_hash or not temp_realpath:
                raise ValueError(
                    "file_replace requires target, expected_old_hash, desired_hash, and temp_path"
                )
        now = self._now()
        encoded_postcondition = json.dumps(
            dict(postcondition or {}),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        with self._transaction() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO recovery_actions(
                        action_id, task_id, action_type, state, idempotency_key,
                        normalized_args_hash, cwd_realpath, plan_hash,
                        expected_pre_state_hash, effect_kind, target_realpath,
                        expected_old_hash, desired_hash, temp_path,
                        postcondition_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        action_id,
                        task_id,
                        action_type,
                        ActionState.PREPARED.value,
                        idempotency_key,
                        normalized_args_hash,
                        canonical_realpath(cwd),
                        plan_hash,
                        expected_pre_state_hash,
                        effect.value,
                        target_realpath,
                        expected_old_hash,
                        desired_hash,
                        temp_realpath,
                        encoded_postcondition,
                        _iso(now),
                        _iso(now),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                row = connection.execute(
                    "SELECT * FROM recovery_actions WHERE task_id=? AND idempotency_key=?",
                    (task_id, idempotency_key),
                ).fetchone()
                if row is not None:
                    return self._action_from_row(row)
                raise
            self._append_journal(
                connection,
                action_id=action_id,
                from_state=None,
                to_state=ActionState.PREPARED,
                occurred_at=now,
                attempt_id=None,
                fencing_generation=0,
                reason="action_prepared",
            )
            row = connection.execute(
                "SELECT * FROM recovery_actions WHERE action_id=?", (action_id,)
            ).fetchone()
        return self._action_from_row(row)

    def prepare_file_replace(
        self,
        *,
        task_id: str,
        target: str | os.PathLike[str],
        content: bytes,
        idempotency_key: str,
        args: Mapping[str, Any],
        cwd: str | os.PathLike[str],
        plan_hash: str,
        action_id: str | None = None,
        expected_old_hash: str | None = None,
    ) -> ActionRecord:
        """Durably stage content beside its target, then journal PREPARED."""

        target_path = Path(target).expanduser().resolve(strict=False)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        action_id = action_id or f"act_{uuid.uuid4().hex}"
        temp_path = target_path.with_name(f".{target_path.name}.{action_id}.tmp")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        descriptor = os.open(temp_path, flags, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            try:
                temp_path.unlink()
            except OSError:
                pass
            raise
        desired_hash = bytes_sha256(content)
        old_hash = expected_old_hash or file_sha256(target_path)
        try:
            return self.prepare_action(
                task_id=task_id,
                action_type="file_replace",
                idempotency_key=idempotency_key,
                normalized_args_hash=canonical_json_hash(args),
                cwd=cwd,
                plan_hash=plan_hash,
                expected_pre_state_hash=old_hash,
                effect_kind=EffectKind.FILE_REPLACE,
                action_id=action_id,
                target=target_path,
                expected_old_hash=old_hash,
                desired_hash=desired_hash,
                temp_path=temp_path,
                postcondition={"target_sha256": desired_hash},
            )
        except BaseException:
            try:
                temp_path.unlink()
            except OSError:
                pass
            raise

    def get_action(self, action_id: str) -> ActionRecord | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM recovery_actions WHERE action_id=?", (action_id,)
            ).fetchone()
        return None if row is None else self._action_from_row(row)

    def list_actions(
        self, *, task_id: str | None = None, states: tuple[ActionState, ...] | None = None
    ) -> list[ActionRecord]:
        conditions: list[str] = []
        params: list[Any] = []
        if task_id is not None:
            conditions.append("task_id=?")
            params.append(task_id)
        if states:
            conditions.append("state IN (" + ",".join("?" for _ in states) + ")")
            params.extend(state.value for state in states)
        query = "SELECT * FROM recovery_actions"
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY created_at, action_id"
        with self._lock:
            rows = self._connection.execute(query, params).fetchall()
        return [self._action_from_row(row) for row in rows]

    def issue_ticket(
        self,
        *,
        action_id: str,
        normalized_args_hash: str,
        cwd: str | os.PathLike[str],
        plan_hash: str,
        expected_pre_state_hash: str,
        approver: str,
        ttl_seconds: float,
        max_uses: int = 1,
        ticket_id: str | None = None,
    ) -> ApprovalTicket:
        """Issue a capability bound to the exact approved action context."""

        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        if max_uses < 1:
            raise ValueError("max_uses must be positive")
        _nonblank(approver, "approver")
        ticket_id = ticket_id or f"apr_{uuid.uuid4().hex}"
        now = self._now()
        expires = now + timedelta(seconds=ttl_seconds)
        cwd_realpath = canonical_realpath(cwd)
        with self._transaction() as connection:
            action_row = connection.execute(
                "SELECT * FROM recovery_actions WHERE action_id=?", (action_id,)
            ).fetchone()
            if action_row is None:
                raise KeyError(f"unknown action: {action_id}")
            action = self._action_from_row(action_row)
            self._assert_binding(
                action,
                normalized_args_hash=normalized_args_hash,
                cwd_realpath=cwd_realpath,
                plan_hash=plan_hash,
                expected_pre_state_hash=expected_pre_state_hash,
            )
            connection.execute(
                """
                INSERT INTO recovery_approval_tickets(
                    ticket_id, action_id, normalized_args_hash, cwd_realpath,
                    plan_hash, expected_pre_state_hash, approver, issued_at,
                    expires_at, max_uses, state
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ticket_id,
                    action_id,
                    normalized_args_hash,
                    cwd_realpath,
                    plan_hash,
                    expected_pre_state_hash,
                    approver,
                    _iso(now),
                    _iso(expires),
                    max_uses,
                    TicketState.ISSUED.value,
                ),
            )
            row = connection.execute(
                "SELECT * FROM recovery_approval_tickets WHERE ticket_id=?",
                (ticket_id,),
            ).fetchone()
        return self._ticket_from_row(row)

    def issue_for_action(
        self,
        action_id: str,
        *,
        approver: str,
        ttl_seconds: float,
        max_uses: int = 1,
        ticket_id: str | None = None,
    ) -> ApprovalTicket:
        action = self.get_action(action_id)
        if action is None:
            raise KeyError(f"unknown action: {action_id}")
        return self.issue_ticket(
            action_id=action_id,
            normalized_args_hash=action.normalized_args_hash,
            cwd=action.cwd_realpath,
            plan_hash=action.plan_hash,
            expected_pre_state_hash=action.expected_pre_state_hash,
            approver=approver,
            ttl_seconds=ttl_seconds,
            max_uses=max_uses,
            ticket_id=ticket_id,
        )

    @staticmethod
    def _assert_binding(
        record: ActionRecord | ApprovalTicket,
        *,
        normalized_args_hash: str,
        cwd_realpath: str,
        plan_hash: str,
        expected_pre_state_hash: str,
    ) -> None:
        compared = {
            "normalized_args_hash": normalized_args_hash,
            "cwd_realpath": cwd_realpath,
            "plan_hash": plan_hash,
            "expected_pre_state_hash": expected_pre_state_hash,
        }
        mismatches = [name for name, value in compared.items() if getattr(record, name) != value]
        if mismatches:
            raise BindingMismatchError(
                "approval binding mismatch: " + ", ".join(sorted(mismatches))
            )

    def get_ticket(self, ticket_id: str) -> ApprovalTicket | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM recovery_approval_tickets WHERE ticket_id=?",
                (ticket_id,),
            ).fetchone()
        return None if row is None else self._ticket_from_row(row)

    def reserve_ticket(
        self,
        ticket_id: str,
        *,
        action_id: str,
        normalized_args_hash: str,
        cwd: str | os.PathLike[str],
        plan_hash: str,
        expected_pre_state_hash: str,
        reservation_token: str | None = None,
    ) -> ApprovalTicket:
        """CAS an ISSUED ticket to RESERVED; exactly one contender wins."""

        now = self._now()
        token = reservation_token or f"rsv_{uuid.uuid4().hex}"
        cwd_realpath = canonical_realpath(cwd)
        expired = False
        updated: sqlite3.Row | None = None
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM recovery_approval_tickets WHERE ticket_id=?",
                (ticket_id,),
            ).fetchone()
            if row is None:
                raise TicketUnavailableError(f"unknown ticket: {ticket_id}")
            ticket = self._ticket_from_row(row)
            if ticket.expires_at <= now:
                if ticket.state in {TicketState.ISSUED, TicketState.RESERVED}:
                    connection.execute(
                        """
                        UPDATE recovery_approval_tickets
                        SET state=?, version=version+1, reservation_token=NULL,
                            reserved_at=NULL
                        WHERE ticket_id=? AND version=?
                        """,
                        (TicketState.EXPIRED.value, ticket_id, ticket.version),
                    )
                expired = True
            else:
                if ticket.action_id != action_id:
                    raise BindingMismatchError("approval binding mismatch: action_id")
                self._assert_binding(
                    ticket,
                    normalized_args_hash=normalized_args_hash,
                    cwd_realpath=cwd_realpath,
                    plan_hash=plan_hash,
                    expected_pre_state_hash=expected_pre_state_hash,
                )
                if ticket.state is not TicketState.ISSUED or ticket.use_count >= ticket.max_uses:
                    raise ConcurrentReservationError(f"ticket is {ticket.state.value}")
                cursor = connection.execute(
                    """
                    UPDATE recovery_approval_tickets
                    SET state=?, reservation_token=?, reserved_at=?, version=version+1
                    WHERE ticket_id=? AND state=? AND version=? AND use_count < max_uses
                    """,
                    (
                        TicketState.RESERVED.value,
                        token,
                        _iso(now),
                        ticket_id,
                        TicketState.ISSUED.value,
                        ticket.version,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ConcurrentReservationError("ticket reservation lost CAS race")
                updated = connection.execute(
                    "SELECT * FROM recovery_approval_tickets WHERE ticket_id=?",
                    (ticket_id,),
                ).fetchone()
        if expired:
            raise TicketUnavailableError("ticket expired")
        assert updated is not None
        return self._ticket_from_row(updated)

    def reserve_for_action(
        self,
        ticket_id: str,
        action_id: str,
        *,
        reservation_token: str | None = None,
    ) -> ApprovalTicket:
        action = self.get_action(action_id)
        if action is None:
            raise KeyError(f"unknown action: {action_id}")
        return self.reserve_ticket(
            ticket_id,
            action_id=action.action_id,
            normalized_args_hash=action.normalized_args_hash,
            cwd=action.cwd_realpath,
            plan_hash=action.plan_hash,
            expected_pre_state_hash=action.expected_pre_state_hash,
            reservation_token=reservation_token,
        )

    def authorize_action(
        self, action_id: str, ticket_id: str, *, reservation_token: str
    ) -> ActionRecord:
        """Bind a RESERVED capability to a PREPARED journal record."""

        now = self._now()
        expired = False
        updated: sqlite3.Row | None = None
        with self._transaction() as connection:
            action_row = connection.execute(
                "SELECT * FROM recovery_actions WHERE action_id=?", (action_id,)
            ).fetchone()
            ticket_row = connection.execute(
                "SELECT * FROM recovery_approval_tickets WHERE ticket_id=?",
                (ticket_id,),
            ).fetchone()
            if action_row is None or ticket_row is None:
                raise KeyError("unknown action or ticket")
            action = self._action_from_row(action_row)
            ticket = self._ticket_from_row(ticket_row)
            if action.state is not ActionState.PREPARED:
                raise InvalidActionTransition(f"cannot authorize {action.state.value}")
            if ticket.state is not TicketState.RESERVED or ticket.reservation_token != reservation_token:
                raise TicketUnavailableError("ticket is not held by this reservation")
            if ticket.expires_at <= now:
                connection.execute(
                    """
                    UPDATE recovery_approval_tickets
                    SET state=?, reservation_token=NULL, reserved_at=NULL,
                        version=version+1
                    WHERE ticket_id=?
                    """,
                    (TicketState.EXPIRED.value, ticket_id),
                )
                expired = True
            else:
                if ticket.action_id != action_id:
                    raise BindingMismatchError("approval binding mismatch: action_id")
                self._assert_binding(
                    ticket,
                    normalized_args_hash=action.normalized_args_hash,
                    cwd_realpath=action.cwd_realpath,
                    plan_hash=action.plan_hash,
                    expected_pre_state_hash=action.expected_pre_state_hash,
                )
                cursor = connection.execute(
                    """
                    UPDATE recovery_actions
                    SET state=?, ticket_id=?, version=version+1, updated_at=?
                    WHERE action_id=? AND state=? AND version=?
                    """,
                    (
                        ActionState.AUTHORIZED.value,
                        ticket_id,
                        _iso(now),
                        action_id,
                        ActionState.PREPARED.value,
                        action.version,
                    ),
                )
                if cursor.rowcount != 1:
                    raise InvalidActionTransition("authorization lost CAS race")
                self._append_journal(
                    connection,
                    action_id=action_id,
                    from_state=ActionState.PREPARED,
                    to_state=ActionState.AUTHORIZED,
                    occurred_at=now,
                    attempt_id=None,
                    fencing_generation=action.fencing_generation,
                    reason="approval_reserved",
                    details={"ticket_id": ticket_id, "approver": ticket.approver},
                )
                updated = connection.execute(
                    "SELECT * FROM recovery_actions WHERE action_id=?", (action_id,)
                ).fetchone()
        if expired:
            raise TicketUnavailableError("ticket expired")
        assert updated is not None
        return self._action_from_row(updated)

    def start_action(self, action_id: str, *, reservation_token: str) -> AttemptLease:
        """Atomically consume approval and record STARTED.

        There is no state in which the capability is consumed but STARTED is
        absent: both updates and the journal append share one transaction.
        """

        now = self._now()
        attempt_id = f"att_{uuid.uuid4().hex}"
        expired = False
        generation = 0
        with self._transaction() as connection:
            action_row = connection.execute(
                "SELECT * FROM recovery_actions WHERE action_id=?", (action_id,)
            ).fetchone()
            if action_row is None:
                raise KeyError(f"unknown action: {action_id}")
            action = self._action_from_row(action_row)
            if action.state is not ActionState.AUTHORIZED or not action.ticket_id:
                raise InvalidActionTransition(f"cannot start {action.state.value}")
            ticket_row = connection.execute(
                "SELECT * FROM recovery_approval_tickets WHERE ticket_id=?",
                (action.ticket_id,),
            ).fetchone()
            ticket = self._ticket_from_row(ticket_row)
            if ticket.state is not TicketState.RESERVED or ticket.reservation_token != reservation_token:
                raise TicketUnavailableError("reservation token is not current")
            if ticket.expires_at <= now:
                connection.execute(
                    """
                    UPDATE recovery_approval_tickets
                    SET state=?, reservation_token=NULL, reserved_at=NULL,
                        version=version+1
                    WHERE ticket_id=?
                    """,
                    (TicketState.EXPIRED.value, ticket.ticket_id),
                )
                expired = True
            else:
                generation = action.fencing_generation + 1
                ticket_state = (
                    TicketState.CONSUMED
                    if ticket.use_count + 1 >= ticket.max_uses
                    else TicketState.ISSUED
                )
                ticket_cursor = connection.execute(
                    """
                    UPDATE recovery_approval_tickets
                    SET state=?, use_count=use_count+1, reservation_token=NULL,
                        reserved_at=NULL, version=version+1
                    WHERE ticket_id=? AND state=? AND version=? AND reservation_token=?
                    """,
                    (
                        ticket_state.value,
                        ticket.ticket_id,
                        TicketState.RESERVED.value,
                        ticket.version,
                        reservation_token,
                    ),
                )
                if ticket_cursor.rowcount != 1:
                    raise ConcurrentReservationError("ticket consumption lost CAS race")
                action_cursor = connection.execute(
                    """
                    UPDATE recovery_actions
                    SET state=?, attempt_id=?, fencing_generation=?, version=version+1,
                        updated_at=?, started_at=?
                    WHERE action_id=? AND state=? AND version=?
                    """,
                    (
                        ActionState.STARTED.value,
                        attempt_id,
                        generation,
                        _iso(now),
                        _iso(now),
                        action_id,
                        ActionState.AUTHORIZED.value,
                        action.version,
                    ),
                )
                if action_cursor.rowcount != 1:
                    raise InvalidActionTransition("start lost CAS race")
                self._append_journal(
                    connection,
                    action_id=action_id,
                    from_state=ActionState.AUTHORIZED,
                    to_state=ActionState.STARTED,
                    occurred_at=now,
                    attempt_id=attempt_id,
                    fencing_generation=generation,
                    reason="approval_consumed_and_action_started",
                    details={"ticket_id": ticket.ticket_id},
                )
        if expired:
            raise TicketUnavailableError("ticket expired")
        return AttemptLease(action_id, attempt_id, generation)

    def revoke_ticket(self, ticket_id: str) -> ApprovalTicket:
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM recovery_approval_tickets WHERE ticket_id=?", (ticket_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown ticket: {ticket_id}")
            ticket = self._ticket_from_row(row)
            if ticket.state in {TicketState.CONSUMED, TicketState.EXPIRED}:
                raise TicketUnavailableError(f"cannot revoke {ticket.state.value} ticket")
            connection.execute(
                """
                UPDATE recovery_approval_tickets
                SET state=?, reservation_token=NULL, reserved_at=NULL, version=version+1
                WHERE ticket_id=? AND version=?
                """,
                (TicketState.REVOKED.value, ticket_id, ticket.version),
            )
            updated = connection.execute(
                "SELECT * FROM recovery_approval_tickets WHERE ticket_id=?", (ticket_id,)
            ).fetchone()
        return self._ticket_from_row(updated)

    def _assert_current_lease(self, action: ActionRecord, lease: AttemptLease) -> None:
        if action.action_id != lease.action_id:
            raise FencingConflictError("lease belongs to another action")
        if (
            action.state is not ActionState.STARTED
            or action.attempt_id != lease.attempt_id
            or action.fencing_generation != lease.fencing_generation
        ):
            raise FencingConflictError("stale or inactive fencing token")

    def supersede_attempt(self, lease: AttemptLease, *, reason: str) -> AttemptLease:
        """Rotate the lease for a known-safe retry; stale workers are fenced."""

        action = self.get_action(lease.action_id)
        if action is None:
            raise KeyError(f"unknown action: {lease.action_id}")
        if action.state is ActionState.UNCERTAIN:
            raise UncertainActionError("uncertain actions are never automatically retried")
        if action.effect_kind is EffectKind.EXTERNAL:
            raise UncertainActionError("external side effects are never automatically retried")
        self._assert_current_lease(action, lease)
        if action.effect_kind is EffectKind.FILE_REPLACE:
            target_hash = file_sha256(action.target_realpath or "")
            temp_hash = file_sha256(action.temp_path or "")
            if target_hash != action.expected_old_hash or temp_hash != action.desired_hash:
                raise PreconditionsFailedError("file retry is not provably safe")
        now = self._now()
        new_attempt = f"att_{uuid.uuid4().hex}"
        generation = action.fencing_generation + 1
        with self._transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE recovery_actions
                SET attempt_id=?, fencing_generation=?, version=version+1, updated_at=?
                WHERE action_id=? AND state=? AND attempt_id=?
                    AND fencing_generation=? AND version=?
                """,
                (
                    new_attempt,
                    generation,
                    _iso(now),
                    action.action_id,
                    ActionState.STARTED.value,
                    lease.attempt_id,
                    lease.fencing_generation,
                    action.version,
                ),
            )
            if cursor.rowcount != 1:
                raise FencingConflictError("attempt rotation lost CAS race")
            self._append_journal(
                connection,
                action_id=action.action_id,
                from_state=ActionState.STARTED,
                to_state=ActionState.STARTED,
                occurred_at=now,
                attempt_id=new_attempt,
                fencing_generation=generation,
                reason=reason,
                details={"superseded_attempt_id": lease.attempt_id},
            )
        return AttemptLease(action.action_id, new_attempt, generation)

    def finish_action(
        self,
        lease: AttemptLease,
        *,
        succeeded: bool,
        error: str | None = None,
        reconciled: bool = False,
        reason: str | None = None,
    ) -> ActionRecord:
        now = self._now()
        destination = ActionState.SUCCEEDED if succeeded else ActionState.FAILED
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM recovery_actions WHERE action_id=?", (lease.action_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown action: {lease.action_id}")
            action = self._action_from_row(row)
            self._assert_current_lease(action, lease)
            cursor = connection.execute(
                """
                UPDATE recovery_actions
                SET state=?, version=version+1, updated_at=?, finished_at=?,
                    error=?, reconciled=?
                WHERE action_id=? AND state=? AND attempt_id=?
                    AND fencing_generation=? AND version=?
                """,
                (
                    destination.value,
                    _iso(now),
                    _iso(now),
                    error,
                    int(reconciled),
                    lease.action_id,
                    ActionState.STARTED.value,
                    lease.attempt_id,
                    lease.fencing_generation,
                    action.version,
                ),
            )
            if cursor.rowcount != 1:
                raise FencingConflictError("completion lost fencing CAS race")
            self._append_journal(
                connection,
                action_id=action.action_id,
                from_state=ActionState.STARTED,
                to_state=destination,
                occurred_at=now,
                attempt_id=lease.attempt_id,
                fencing_generation=lease.fencing_generation,
                reason=reason or ("postcondition_verified" if succeeded else "action_failed"),
                details={"error": error, "reconciled": reconciled},
            )
            updated = connection.execute(
                "SELECT * FROM recovery_actions WHERE action_id=?", (action.action_id,)
            ).fetchone()
        return self._action_from_row(updated)

    def mark_uncertain(
        self, lease: AttemptLease, *, reason: str, error: str | None = None
    ) -> ActionRecord:
        """Stop automatic execution when an external commit is ambiguous."""

        now = self._now()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM recovery_actions WHERE action_id=?", (lease.action_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown action: {lease.action_id}")
            action = self._action_from_row(row)
            self._assert_current_lease(action, lease)
            cursor = connection.execute(
                """
                UPDATE recovery_actions
                SET state=?, version=version+1, updated_at=?, finished_at=?, error=?
                WHERE action_id=? AND state=? AND attempt_id=?
                    AND fencing_generation=? AND version=?
                """,
                (
                    ActionState.UNCERTAIN.value,
                    _iso(now),
                    _iso(now),
                    error,
                    action.action_id,
                    ActionState.STARTED.value,
                    lease.attempt_id,
                    lease.fencing_generation,
                    action.version,
                ),
            )
            if cursor.rowcount != 1:
                raise FencingConflictError("uncertain transition lost CAS race")
            self._append_journal(
                connection,
                action_id=action.action_id,
                from_state=ActionState.STARTED,
                to_state=ActionState.UNCERTAIN,
                occurred_at=now,
                attempt_id=lease.attempt_id,
                fencing_generation=lease.fencing_generation,
                reason=reason,
                details={"error": error, "automatic_retry": False},
            )
            updated = connection.execute(
                "SELECT * FROM recovery_actions WHERE action_id=?", (action.action_id,)
            ).fetchone()
        return self._action_from_row(updated)

    @staticmethod
    def _fsync_parent(path: Path) -> None:
        if os.name == "nt":
            return
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def execute_file_replace(
        self, lease: AttemptLease, *, crash_after_replace: bool = False
    ) -> ActionRecord:
        """Execute the staged rename and verify its durable postcondition."""

        action = self.get_action(lease.action_id)
        if action is None:
            raise KeyError(f"unknown action: {lease.action_id}")
        self._assert_current_lease(action, lease)
        if action.effect_kind is not EffectKind.FILE_REPLACE:
            raise ValueError("action is not a file replacement")
        target = Path(action.target_realpath or "")
        temp_path = Path(action.temp_path or "")
        current_hash = file_sha256(target)
        if current_hash == action.desired_hash:
            return self.finish_action(
                lease,
                succeeded=True,
                reconciled=True,
                reason="desired_postcondition_already_present",
            )
        if current_hash != action.expected_old_hash:
            return self.finish_action(
                lease,
                succeeded=False,
                error="target precondition hash mismatch",
                reason="precondition_failed",
            )
        if file_sha256(temp_path) != action.desired_hash:
            return self.finish_action(
                lease,
                succeeded=False,
                error="staged content hash mismatch",
                reason="staging_corrupted",
            )
        os.replace(temp_path, target)
        self._fsync_parent(target.parent)
        if crash_after_replace:
            raise SimulatedCrash("process died after rename and before DB completion")
        if file_sha256(target) != action.desired_hash:
            return self.mark_uncertain(
                lease,
                reason="postcondition_unverifiable",
                error="target does not contain desired hash after replace",
            )
        return self.finish_action(lease, succeeded=True, reason="file_replace_verified")

    def create_checkpoint(
        self,
        *,
        task_id: str,
        label: str,
        action_id: str | None = None,
        payload: Mapping[str, Any] | None = None,
        checkpoint_id: str | None = None,
    ) -> Checkpoint:
        _nonblank(task_id, "task_id")
        _nonblank(label, "label")
        checkpoint_id = checkpoint_id or f"chk_{uuid.uuid4().hex}"
        now = self._now()
        with self._transaction() as connection:
            if action_id is not None:
                exists = connection.execute(
                    "SELECT 1 FROM recovery_actions WHERE action_id=?", (action_id,)
                ).fetchone()
                if exists is None:
                    raise KeyError(f"unknown action: {action_id}")
            sequence = int(
                connection.execute(
                    "SELECT COALESCE(MAX(sequence), 0) FROM recovery_action_journal"
                ).fetchone()[0]
            )
            encoded = json.dumps(
                dict(payload or {}),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            connection.execute(
                """
                INSERT INTO recovery_checkpoints(
                    checkpoint_id, task_id, label, created_at, action_id,
                    journal_sequence, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (checkpoint_id, task_id, label, _iso(now), action_id, sequence, encoded),
            )
        return Checkpoint(
            checkpoint_id, task_id, label, now, action_id, sequence, json.loads(encoded)
        )

    def list_checkpoints(self, *, task_id: str | None = None) -> list[Checkpoint]:
        query = "SELECT * FROM recovery_checkpoints"
        params: tuple[Any, ...] = ()
        if task_id is not None:
            query += " WHERE task_id=?"
            params = (task_id,)
        query += " ORDER BY created_at, checkpoint_id"
        with self._lock:
            rows = self._connection.execute(query, params).fetchall()
        return [
            Checkpoint(
                row["checkpoint_id"],
                row["task_id"],
                row["label"],
                datetime.fromisoformat(row["created_at"]),
                row["action_id"],
                int(row["journal_sequence"]),
                json.loads(row["payload_json"]),
            )
            for row in rows
        ]

    def list_journal(self, *, action_id: str | None = None) -> list[JournalEntry]:
        query = "SELECT * FROM recovery_action_journal"
        params: tuple[Any, ...] = ()
        if action_id is not None:
            query += " WHERE action_id=?"
            params = (action_id,)
        query += " ORDER BY sequence"
        with self._lock:
            rows = self._connection.execute(query, params).fetchall()
        return [
            JournalEntry(
                sequence=int(row["sequence"]),
                action_id=row["action_id"],
                from_state=None
                if row["from_state"] is None
                else ActionState(row["from_state"]),
                to_state=ActionState(row["to_state"]),
                occurred_at=datetime.fromisoformat(row["occurred_at"]),
                attempt_id=row["attempt_id"],
                fencing_generation=int(row["fencing_generation"]),
                reason=row["reason"],
                details=json.loads(row["details_json"]),
            )
            for row in rows
        ]

    def _expire_tickets(self, connection: sqlite3.Connection, now: datetime) -> tuple[str, ...]:
        rows = connection.execute(
            """
            SELECT ticket_id FROM recovery_approval_tickets
            WHERE state IN (?, ?) AND expires_at <= ? ORDER BY ticket_id
            """,
            (TicketState.ISSUED.value, TicketState.RESERVED.value, _iso(now)),
        ).fetchall()
        identifiers = tuple(row["ticket_id"] for row in rows)
        if identifiers:
            connection.execute(
                """
                UPDATE recovery_approval_tickets
                SET state=?, reservation_token=NULL, reserved_at=NULL, version=version+1
                WHERE state IN (?, ?) AND expires_at <= ?
                """,
                (
                    TicketState.EXPIRED.value,
                    TicketState.ISSUED.value,
                    TicketState.RESERVED.value,
                    _iso(now),
                ),
            )
        return identifiers

    def scan_recovery(self) -> RecoveryReport:
        """Reconcile deterministic actions and surface every ambiguous one.

        An interrupted external action is atomically classified UNCERTAIN.  It
        never appears as a retry candidate.  File replacement is recognized as
        successful from its desired hash even if the process died immediately
        after the atomic rename.
        """

        now = self._now()
        reconciled: list[str] = []
        items: list[RecoveryItem] = []
        with self._transaction() as connection:
            expired = self._expire_tickets(connection, now)
            rows = connection.execute(
                """
                SELECT * FROM recovery_actions
                WHERE state IN (?, ?, ?) ORDER BY created_at, action_id
                """,
                (
                    ActionState.PREPARED.value,
                    ActionState.AUTHORIZED.value,
                    ActionState.STARTED.value,
                ),
            ).fetchall()
            for row in rows:
                action = self._action_from_row(row)
                if action.state is ActionState.PREPARED:
                    items.append(RecoveryItem(action, "await_approval", "no authorization"))
                    continue
                if action.state is ActionState.AUTHORIZED:
                    ticket_row = connection.execute(
                        "SELECT state FROM recovery_approval_tickets WHERE ticket_id=?",
                        (action.ticket_id,),
                    ).fetchone()
                    ticket_state = None if ticket_row is None else ticket_row["state"]
                    recommendation = (
                        "resume_start"
                        if ticket_state == TicketState.RESERVED.value
                        else "request_new_approval"
                    )
                    items.append(
                        RecoveryItem(action, recommendation, f"ticket state is {ticket_state}")
                    )
                    continue

                lease = AttemptLease(
                    action.action_id,
                    action.attempt_id or "",
                    action.fencing_generation,
                )
                if action.effect_kind is EffectKind.EXTERNAL:
                    cursor = connection.execute(
                        """
                        UPDATE recovery_actions
                        SET state=?, version=version+1, updated_at=?, finished_at=?,
                            error=COALESCE(error, ?)
                        WHERE action_id=? AND state=? AND version=?
                        """,
                        (
                            ActionState.UNCERTAIN.value,
                            _iso(now),
                            _iso(now),
                            "external side effect interrupted at commit boundary",
                            action.action_id,
                            ActionState.STARTED.value,
                            action.version,
                        ),
                    )
                    if cursor.rowcount == 1:
                        self._append_journal(
                            connection,
                            action_id=action.action_id,
                            from_state=ActionState.STARTED,
                            to_state=ActionState.UNCERTAIN,
                            occurred_at=now,
                            attempt_id=lease.attempt_id,
                            fencing_generation=lease.fencing_generation,
                            reason="external_commit_ambiguous_after_restart",
                            details={"automatic_retry": False},
                        )
                    uncertain_row = connection.execute(
                        "SELECT * FROM recovery_actions WHERE action_id=?",
                        (action.action_id,),
                    ).fetchone()
                    items.append(
                        RecoveryItem(
                            self._action_from_row(uncertain_row),
                            "human_review_only",
                            "external side effect may already have committed",
                        )
                    )
                    continue

                if action.effect_kind is EffectKind.FILE_REPLACE:
                    target_hash = file_sha256(action.target_realpath or "")
                    temp_hash = file_sha256(action.temp_path or "")
                    if target_hash == action.desired_hash:
                        cursor = connection.execute(
                            """
                            UPDATE recovery_actions
                            SET state=?, version=version+1, updated_at=?, finished_at=?,
                                reconciled=1
                            WHERE action_id=? AND state=? AND version=?
                            """,
                            (
                                ActionState.SUCCEEDED.value,
                                _iso(now),
                                _iso(now),
                                action.action_id,
                                ActionState.STARTED.value,
                                action.version,
                            ),
                        )
                        if cursor.rowcount == 1:
                            self._append_journal(
                                connection,
                                action_id=action.action_id,
                                from_state=ActionState.STARTED,
                                to_state=ActionState.SUCCEEDED,
                                occurred_at=now,
                                attempt_id=lease.attempt_id,
                                fencing_generation=lease.fencing_generation,
                                reason="crash_reconciled_from_desired_hash",
                                details={"target_hash": target_hash},
                            )
                            reconciled.append(action.action_id)
                        continue
                    if target_hash == action.expected_old_hash and temp_hash == action.desired_hash:
                        items.append(
                            RecoveryItem(
                                action,
                                "safe_retry_with_new_fence",
                                "old target and desired staging hashes verified",
                            )
                        )
                        continue
                    # Neither old nor desired state is provable.  Do not retry.
                    cursor = connection.execute(
                        """
                        UPDATE recovery_actions
                        SET state=?, version=version+1, updated_at=?, finished_at=?, error=?
                        WHERE action_id=? AND state=? AND version=?
                        """,
                        (
                            ActionState.UNCERTAIN.value,
                            _iso(now),
                            _iso(now),
                            "file state does not match old or desired hashes",
                            action.action_id,
                            ActionState.STARTED.value,
                            action.version,
                        ),
                    )
                    if cursor.rowcount == 1:
                        self._append_journal(
                            connection,
                            action_id=action.action_id,
                            from_state=ActionState.STARTED,
                            to_state=ActionState.UNCERTAIN,
                            occurred_at=now,
                            attempt_id=lease.attempt_id,
                            fencing_generation=lease.fencing_generation,
                            reason="file_postcondition_ambiguous",
                            details={"automatic_retry": False},
                        )
                    uncertain_row = connection.execute(
                        "SELECT * FROM recovery_actions WHERE action_id=?",
                        (action.action_id,),
                    ).fetchone()
                    items.append(
                        RecoveryItem(
                            self._action_from_row(uncertain_row),
                            "human_review_only",
                            "file hashes are ambiguous",
                        )
                    )
                    continue

                items.append(
                    RecoveryItem(action, "safe_retry_with_new_fence", "read-only action")
                )

            # UNCERTAIN is terminal for automatic execution, but not for the
            # operator workflow.  Keep every unacknowledged item visible on
            # later startups instead of showing it only during the one scan
            # that first classified the action.
            visible_ids = {item.action.action_id for item in items}
            uncertain_rows = connection.execute(
                """
                SELECT action.* FROM recovery_actions AS action
                WHERE action.state=?
                  AND NOT EXISTS (
                    SELECT 1 FROM recovery_checkpoints AS checkpoint
                    WHERE checkpoint.action_id=action.action_id
                      AND checkpoint.label='human_acknowledged_uncertain'
                  )
                ORDER BY action.created_at, action.action_id
                """,
                (ActionState.UNCERTAIN.value,),
            ).fetchall()
            for row in uncertain_rows:
                action = self._action_from_row(row)
                if action.action_id not in visible_ids:
                    items.append(
                        RecoveryItem(
                            action,
                            "human_review_only",
                            action.error or "side effect outcome remains uncertain",
                        )
                    )
        return RecoveryReport(now, tuple(reconciled), tuple(items), expired)
