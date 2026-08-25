from __future__ import annotations

from pathlib import Path

import pytest

from mewcode.recovery import (
    ActionState,
    EffectKind,
    FencingConflictError,
    RecoveryStore,
    SimulatedCrash,
    TicketState,
    UncertainActionError,
    canonical_json_hash,
    file_sha256,
)


def _authorize_and_start(store: RecoveryStore, action_id: str):
    ticket = store.issue_for_action(action_id, approver="alice", ttl_seconds=60)
    store.reserve_for_action(
        ticket.ticket_id, action_id, reservation_token="holder"
    )
    store.authorize_action(action_id, ticket.ticket_id, reservation_token="holder")
    return store.start_action(action_id, reservation_token="holder")


def test_rename_then_crash_is_reconciled_from_desired_hash(tmp_path: Path) -> None:
    database = tmp_path / "control" / "runtime.db"
    target = tmp_path / "workspace" / "answer.txt"
    target.parent.mkdir()
    target.write_bytes(b"old")
    with RecoveryStore(database_path=database) as store:
        action = store.prepare_file_replace(
            task_id="task-file",
            target=target,
            content=b"new content",
            idempotency_key="replace-answer",
            args={"path": "answer.txt", "content_hash": "new"},
            cwd=target.parent,
            plan_hash="plan-v1",
        )
        lease = _authorize_and_start(store, action.action_id)
        with pytest.raises(SimulatedCrash):
            store.execute_file_replace(lease, crash_after_replace=True)
        assert target.read_bytes() == b"new content"
        assert store.get_action(action.action_id).state is ActionState.STARTED

    # A fresh process/store sees STARTED, checks the durable postcondition, and
    # records success without repeating the rename.
    with RecoveryStore(database_path=database) as store:
        report = store.scan_recovery()
        assert report.reconciled_action_ids == (action.action_id,)
        recovered = store.get_action(action.action_id)
        assert recovered.state is ActionState.SUCCEEDED
        assert recovered.reconciled is True
        assert file_sha256(target) == action.desired_hash
        assert store.list_journal(action_id=action.action_id)[-1].reason == (
            "crash_reconciled_from_desired_hash"
        )


def test_stale_fencing_generation_cannot_finish_new_attempt(tmp_path: Path) -> None:
    target = tmp_path / "answer.txt"
    target.write_bytes(b"old")
    with RecoveryStore(database_path=tmp_path / "runtime.db") as store:
        action = store.prepare_file_replace(
            task_id="task-file",
            target=target,
            content=b"new",
            idempotency_key="replace-answer",
            args={"path": "answer.txt"},
            cwd=tmp_path,
            plan_hash="plan-v1",
        )
        old_lease = _authorize_and_start(store, action.action_id)
        new_lease = store.supersede_attempt(old_lease, reason="worker lease expired")
        assert new_lease.fencing_generation == old_lease.fencing_generation + 1
        with pytest.raises(FencingConflictError):
            store.finish_action(old_lease, succeeded=True)
        succeeded = store.execute_file_replace(new_lease)
        assert succeeded.state is ActionState.SUCCEEDED


def test_interrupted_external_effect_becomes_uncertain_and_never_retries(
    tmp_path: Path,
) -> None:
    db = tmp_path / "runtime.db"
    with RecoveryStore(database_path=db) as store:
        action = store.prepare_action(
            task_id="task-ext",
            action_id="send-message",
            action_type="http_post",
            idempotency_key="message-1",
            normalized_args_hash=canonical_json_hash({"url": "https://example.test"}),
            cwd=tmp_path,
            plan_hash="plan-v1",
            expected_pre_state_hash="remote-unknown",
            effect_kind=EffectKind.EXTERNAL,
            postcondition={"receipt": "required"},
        )
        lease = _authorize_and_start(store, action.action_id)
        assert store.get_ticket(store.get_action(action.action_id).ticket_id).state is (
            TicketState.CONSUMED
        )

    with RecoveryStore(database_path=db) as store:
        report = store.scan_recovery()
        assert report.reconciled_action_ids == ()
        uncertain = store.get_action(action.action_id)
        assert uncertain.state is ActionState.UNCERTAIN
        item = next(item for item in report.items if item.action.action_id == action.action_id)
        assert item.recommendation == "human_review_only"
        assert "may already have committed" in item.reason
        with pytest.raises(UncertainActionError, match="never automatically retried"):
            store.supersede_attempt(lease, reason="retry")
        # Scanning again keeps the unresolved uncertainty visible without
        # creating a new STARTED attempt or journal entry.
        journal_size = len(store.list_journal(action_id=action.action_id))
        second = store.scan_recovery()
        assert any(item.action.action_id == action.action_id for item in second.items)
        assert len(store.list_journal(action_id=action.action_id)) == journal_size


def test_safe_file_retry_is_reported_but_not_automatically_executed(tmp_path: Path) -> None:
    target = tmp_path / "answer.txt"
    target.write_bytes(b"old")
    with RecoveryStore(database_path=tmp_path / "runtime.db") as store:
        action = store.prepare_file_replace(
            task_id="task-file",
            target=target,
            content=b"new",
            idempotency_key="replace-answer",
            args={"path": "answer.txt"},
            cwd=tmp_path,
            plan_hash="plan-v1",
        )
        _authorize_and_start(store, action.action_id)
        report = store.scan_recovery()
        item = next(item for item in report.items if item.action.action_id == action.action_id)
        assert item.recommendation == "safe_retry_with_new_fence"
        assert target.read_bytes() == b"old"
        assert store.get_action(action.action_id).state is ActionState.STARTED
