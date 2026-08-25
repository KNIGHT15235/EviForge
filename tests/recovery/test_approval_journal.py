from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mewcode.recovery import (
    ActionState,
    BindingMismatchError,
    ConcurrentReservationError,
    EffectKind,
    RecoveryStore,
    TicketState,
    TicketUnavailableError,
    canonical_json_hash,
)


def _prepare(store: RecoveryStore, tmp_path: Path, *, action_id: str = "act-1"):
    return store.prepare_action(
        task_id="task-1",
        action_id=action_id,
        action_type="write",
        idempotency_key=f"idem-{action_id}",
        normalized_args_hash=canonical_json_hash({"path": "answer.txt", "text": "42"}),
        cwd=tmp_path,
        plan_hash="plan-v1",
        expected_pre_state_hash="pre-v1",
        effect_kind=EffectKind.READ_ONLY,
        postcondition={"ok": True},
    )


def test_control_plane_is_wal_and_injectable(tmp_path: Path) -> None:
    database = tmp_path / "control" / "recovery.db"
    with RecoveryStore(database_path=database) as store:
        assert store.db_path == database.resolve()
        with sqlite3.connect(database) as connection:
            assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


def test_ticket_is_bound_to_arguments_cwd_plan_and_prestate(tmp_path: Path) -> None:
    with RecoveryStore(database_path=tmp_path / "runtime.db") as store:
        action = _prepare(store, tmp_path)
        ticket = store.issue_for_action(action.action_id, approver="alice", ttl_seconds=30)

        with pytest.raises(BindingMismatchError, match="normalized_args_hash"):
            store.reserve_ticket(
                ticket.ticket_id,
                action_id=action.action_id,
                normalized_args_hash="different",
                cwd=tmp_path,
                plan_hash=action.plan_hash,
                expected_pre_state_hash=action.expected_pre_state_hash,
            )
        with pytest.raises(BindingMismatchError, match="cwd_realpath"):
            store.reserve_ticket(
                ticket.ticket_id,
                action_id=action.action_id,
                normalized_args_hash=action.normalized_args_hash,
                cwd=tmp_path / "elsewhere",
                plan_hash=action.plan_hash,
                expected_pre_state_hash=action.expected_pre_state_hash,
            )
        with pytest.raises(BindingMismatchError, match="plan_hash"):
            store.reserve_ticket(
                ticket.ticket_id,
                action_id=action.action_id,
                normalized_args_hash=action.normalized_args_hash,
                cwd=tmp_path,
                plan_hash="plan-v2",
                expected_pre_state_hash=action.expected_pre_state_hash,
            )
        with pytest.raises(BindingMismatchError, match="expected_pre_state_hash"):
            store.reserve_ticket(
                ticket.ticket_id,
                action_id=action.action_id,
                normalized_args_hash=action.normalized_args_hash,
                cwd=tmp_path,
                plan_hash=action.plan_hash,
                expected_pre_state_hash="workspace-changed-since-approval",
            )


def test_expiry_is_persisted_and_cannot_be_reserved(tmp_path: Path) -> None:
    now = datetime(2026, 8, 12, tzinfo=timezone.utc)

    def clock() -> datetime:
        return now + timedelta(minutes=5)

    # Issue with a stable clock, then reopen with a future clock.
    db = tmp_path / "runtime.db"
    with RecoveryStore(database_path=db, clock=lambda: now) as store:
        action = _prepare(store, tmp_path)
        ticket = store.issue_for_action(action.action_id, approver="alice", ttl_seconds=1)
    with RecoveryStore(database_path=db, clock=clock) as store:
        with pytest.raises(TicketUnavailableError, match="expired"):
            store.reserve_for_action(ticket.ticket_id, action.action_id)
        assert store.get_ticket(ticket.ticket_id).state is TicketState.EXPIRED


def test_one_shot_ticket_has_exactly_one_concurrent_winner(tmp_path: Path) -> None:
    db = tmp_path / "runtime.db"
    with RecoveryStore(database_path=db) as store:
        action = _prepare(store, tmp_path)
        ticket = store.issue_for_action(action.action_id, approver="alice", ttl_seconds=30)

    barrier = threading.Barrier(8)
    winners: list[str] = []
    failures: list[type[BaseException]] = []
    guard = threading.Lock()

    def contender(index: int) -> None:
        try:
            with RecoveryStore(database_path=db) as candidate:
                barrier.wait()
                reserved = candidate.reserve_for_action(
                    ticket.ticket_id,
                    action.action_id,
                    reservation_token=f"worker-{index}",
                )
                with guard:
                    winners.append(reserved.reservation_token or "")
        except BaseException as exc:  # collect failures from worker threads
            with guard:
                failures.append(type(exc))

    threads = [threading.Thread(target=contender, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert all(not thread.is_alive() for thread in threads)
    assert len(winners) == 1
    assert len(failures) == 7
    assert set(failures) == {ConcurrentReservationError}


def test_start_and_ticket_consumption_share_a_transaction(tmp_path: Path) -> None:
    with RecoveryStore(database_path=tmp_path / "runtime.db") as store:
        action = _prepare(store, tmp_path)
        ticket = store.issue_for_action(action.action_id, approver="alice", ttl_seconds=30)
        reserved = store.reserve_for_action(
            ticket.ticket_id, action.action_id, reservation_token="holder"
        )
        assert reserved.state is TicketState.RESERVED
        authorized = store.authorize_action(
            action.action_id, ticket.ticket_id, reservation_token="holder"
        )
        assert authorized.state is ActionState.AUTHORIZED
        lease = store.start_action(action.action_id, reservation_token="holder")

        assert store.get_ticket(ticket.ticket_id).state is TicketState.CONSUMED
        started = store.get_action(action.action_id)
        assert started.state is ActionState.STARTED
        assert started.attempt_id == lease.attempt_id
        assert [entry.to_state for entry in store.list_journal(action_id=action.action_id)] == [
            ActionState.PREPARED,
            ActionState.AUTHORIZED,
            ActionState.STARTED,
        ]


def test_concurrent_consumption_creates_only_one_attempt(tmp_path: Path) -> None:
    db = tmp_path / "runtime.db"
    with RecoveryStore(database_path=db) as store:
        action = _prepare(store, tmp_path)
        ticket = store.issue_for_action(action.action_id, approver="alice", ttl_seconds=30)
        store.reserve_for_action(
            ticket.ticket_id, action.action_id, reservation_token="holder"
        )
        store.authorize_action(
            action.action_id, ticket.ticket_id, reservation_token="holder"
        )

    barrier = threading.Barrier(2)
    leases = []
    failures: list[BaseException] = []
    guard = threading.Lock()

    def starter() -> None:
        try:
            with RecoveryStore(database_path=db) as candidate:
                barrier.wait()
                lease = candidate.start_action(action.action_id, reservation_token="holder")
                with guard:
                    leases.append(lease)
        except BaseException as exc:
            with guard:
                failures.append(exc)

    threads = [threading.Thread(target=starter) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert len(leases) == 1
    assert len(failures) == 1
    with RecoveryStore(database_path=db) as store:
        assert store.get_ticket(ticket.ticket_id).use_count == 1
        starts = [
            entry
            for entry in store.list_journal(action_id=action.action_id)
            if entry.to_state is ActionState.STARTED
        ]
        assert len(starts) == 1


def test_checkpoint_captures_authoritative_journal_cursor(tmp_path: Path) -> None:
    with RecoveryStore(database_path=tmp_path / "runtime.db") as store:
        action = _prepare(store, tmp_path)
        checkpoint = store.create_checkpoint(
            task_id=action.task_id,
            action_id=action.action_id,
            label="prepared",
            payload={"plan_hash": action.plan_hash},
        )
        assert checkpoint.journal_sequence == 1
        assert store.list_checkpoints(task_id=action.task_id) == [checkpoint]
