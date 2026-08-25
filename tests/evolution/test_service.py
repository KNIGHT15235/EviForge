from __future__ import annotations

from pathlib import Path

import pytest

from mewcode.evolution import EvolutionRegistry, TraceEvolutionService, task_signature
from mewcode.runtime import RuntimeStore, TaskRuntime, TraceEvent


def completed_runtime(tmp_path: Path) -> tuple[RuntimeStore, TaskRuntime]:
    store = RuntimeStore(control_root=tmp_path / "control", workspace_id="service")
    task = TaskRuntime.create(store, task_id="task-1", trace_id="trace-1")
    task.prepare_contract("contract-1")
    task.begin_planning()
    task.begin_execution()
    task.begin_verification()
    task.apply_gate_verdict(
        "PASS",
        decision_id="gate-1",
        bundle_ref="evidence://bundle-1",
    )
    return store, task


def test_completed_trace_becomes_quarantined_candidate(tmp_path: Path) -> None:
    store, _ = completed_runtime(tmp_path)
    with EvolutionRegistry(tmp_path / "evolution.db") as registry:
        service = TraceEvolutionService(registry, store, workspace=tmp_path)
        result = service.ingest(
            task_id="task-1",
            objective="Fix asyncio cancellation without leaking workers",
            failure_signature="CancelledError swallowed",
            root_cause_family="async-cancel",
            decision="Re-raise cancellation after deterministic cleanup.",
            procedure=("Clean resources in finally.", "Re-raise CancelledError."),
            source_commit="abc1234",
            source_code_hash="a" * 64,
        )

        candidate = registry.get_candidate(result.candidate_id)
        assert result.disposition == "added"
        assert candidate.candidate.status.value == "quarantine"
        assert candidate.candidate.evidence_refs == ("gate://gate-1", "evidence://bundle-1")


def test_unverified_task_cannot_create_experience(tmp_path: Path) -> None:
    store = RuntimeStore(control_root=tmp_path / "control", workspace_id="blocked")
    TaskRuntime.create(store, task_id="task-1", trace_id="trace-1")
    with EvolutionRegistry(tmp_path / "evolution.db") as registry:
        service = TraceEvolutionService(registry, store, workspace=tmp_path)
        with pytest.raises(ValueError, match="Evidence Gate"):
            service.ingest(
                task_id="task-1",
                objective="claim done",
                failure_signature="no evidence",
                root_cause_family="false-completion",
                decision="require evidence",
                procedure=("Run verifier.",),
                source_commit="abc1234",
                source_code_hash="a" * 64,
            )


def test_task_signature_is_stable_and_bounded() -> None:
    text = "  FIX   AsyncIO cancellation " + " token" * 100
    assert task_signature(text) == task_signature(text.casefold())
    assert len(task_signature(text).split()) == 24


def test_artifact_alone_or_fake_completed_event_is_not_a_pass_receipt(
    tmp_path: Path,
) -> None:
    store = RuntimeStore(control_root=tmp_path / "control", workspace_id="fake-pass")
    task = TaskRuntime.create(store, task_id="task-1", trace_id="trace-1")
    store.append_event(
        TraceEvent(
            trace_id=task.run.trace_id,
            task_id=task.task_id,
            event_type="task_state_changed",
            status="COMPLETED",
            artifact_refs=("evidence://untrusted-artifact",),
            payload={"details": {"decision_id": "forged"}},
        )
    )
    with EvolutionRegistry(tmp_path / "evolution.db") as registry:
        service = TraceEvolutionService(registry, store, workspace=tmp_path)
        assert service.successful_evidence_refs(task.task_id) == ()
