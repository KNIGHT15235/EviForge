from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from mewcode.evidence import (
    ArtifactRef,
    CommandEvidence,
    CommandStatus,
    CriterionEvidence,
    CriterionStatus,
    EvidenceGate,
    GateVerdict,
    RequirementContract,
    RequirementCriterion,
    VerificationReceipt,
    VerifierSpec,
    WorkspaceSnapshot,
)


def snapshot(diff_hash: str = "a" * 64) -> WorkspaceSnapshot:
    return WorkspaceSnapshot(
        repo_root=str(Path.cwd()),
        base_ref="HEAD",
        base_commit="1" * 40,
        head_commit="1" * 40,
        tracked_diff_sha256="2" * 64,
        untracked_sha256="3" * 64,
        diff_sha256=diff_hash,
    )


@pytest.fixture
def contract() -> RequirementContract:
    return RequirementContract(
        contract_id="contract-1",
        task_id="task-1",
        objective="Provide verified behavior",
        criteria=(
            RequirementCriterion(
                criterion_id="required",
                description="required criterion",
                verifier_ids=("unit",),
            ),
            RequirementCriterion(
                criterion_id="optional",
                description="optional criterion",
                required=False,
            ),
        ),
        verifiers=(VerifierSpec(verifier_id="unit", name="unit", argv=("pytest",), cwd="."),),
    )


def receipt_for(
    contract: RequirementContract,
    status: CriterionStatus,
    *,
    diff_hash: str = "a" * 64,
) -> VerificationReceipt:
    current = snapshot(diff_hash)
    artifact = ArtifactRef(
        sha256="4" * 64,
        size_bytes=0,
        relative_path="artifacts/empty",
    )
    now = datetime.now(timezone.utc)
    command = CommandEvidence(
        verifier_id="unit",
        status=CommandStatus.PASS,
        argv=("pytest",),
        cwd=str(Path.cwd()),
        expected_exit_codes=frozenset({0}),
        exit_code=0,
        started_at=now,
        finished_at=now,
        duration_ms=0,
        workspace_diff_sha256=diff_hash,
        stdout=artifact,
        stderr=artifact,
    )
    return VerificationReceipt(
        receipt_id="receipt-1",
        contract_id=contract.contract_id,
        task_id=contract.task_id,
        workspace_diff_sha256=diff_hash,
        workspace_before=current,
        workspace_after=current,
        commands=(command,),
        criteria=(
            CriterionEvidence(criterion_id="required", status=status, evidence_ids=(command.evidence_id,)),
            CriterionEvidence(criterion_id="optional", status=CriterionStatus.NOT_RUN),
        ),
    )


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (CriterionStatus.PASS, GateVerdict.PASS),
        (CriterionStatus.FAIL, GateVerdict.FAIL),
        (CriterionStatus.BLOCKED, GateVerdict.BLOCKED),
        (CriterionStatus.NOT_RUN, GateVerdict.PARTIAL),
    ],
)
def test_gate_exposes_all_four_verdicts(
    contract: RequirementContract,
    status: CriterionStatus,
    expected: GateVerdict,
) -> None:
    decision = EvidenceGate().evaluate(contract, receipt_for(contract, status), snapshot())

    assert decision.verdict == expected


def test_gate_returns_partial_without_receipt(contract: RequirementContract) -> None:
    decision = EvidenceGate().evaluate(contract, None, snapshot())

    assert decision.verdict == GateVerdict.PARTIAL
    assert all(item.status == CriterionStatus.NOT_RUN for item in decision.criteria)


def test_gate_blocks_stale_diff_bound_evidence(contract: RequirementContract) -> None:
    decision = EvidenceGate().evaluate(
        contract,
        receipt_for(contract, CriterionStatus.PASS),
        snapshot("f" * 64),
    )

    assert decision.verdict == GateVerdict.BLOCKED
    assert decision.stale_evidence is True
    assert "workspace changed" in " ".join(decision.reasons)


def test_gate_does_not_trust_pass_without_declared_verifier_evidence(
    contract: RequirementContract,
) -> None:
    receipt = receipt_for(contract, CriterionStatus.PASS)
    stripped = receipt.model_copy(
        update={
            "criteria": (
                CriterionEvidence(criterion_id="required", status=CriterionStatus.PASS),
                CriterionEvidence(criterion_id="optional", status=CriterionStatus.NOT_RUN),
            )
        }
    )

    decision = EvidenceGate().evaluate(contract, stripped, snapshot())

    assert decision.verdict == GateVerdict.PARTIAL
    assert "missing declared verifier evidence" in decision.criteria[0].reason


def test_gate_overrides_forged_pass_when_command_failed(contract: RequirementContract) -> None:
    receipt = receipt_for(contract, CriterionStatus.PASS)
    failed_command = receipt.commands[0].model_copy(
        update={"status": CommandStatus.FAIL, "exit_code": 1}
    )
    forged = receipt.model_copy(update={"commands": (failed_command,)})

    decision = EvidenceGate().evaluate(contract, forged, snapshot())

    assert decision.verdict == GateVerdict.FAIL
    assert "command failed" in decision.criteria[0].reason


def test_gate_blocks_same_verifier_id_with_replaced_argv(contract: RequirementContract) -> None:
    receipt = receipt_for(contract, CriterionStatus.PASS)
    replaced = receipt.commands[0].model_copy(update={"argv": ("echo", "not pytest")})
    forged = receipt.model_copy(update={"commands": (replaced,)})

    decision = EvidenceGate().evaluate(contract, forged, snapshot())

    assert decision.verdict == GateVerdict.BLOCKED
    assert "does not match" in decision.criteria[0].reason
