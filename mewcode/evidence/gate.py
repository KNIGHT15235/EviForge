"""Deterministic completion gate for requirement contracts."""

from __future__ import annotations

from pathlib import Path

from .models import (
    CriterionEvidence,
    CriterionStatus,
    GateDecision,
    GateVerdict,
    RequirementContract,
    VerificationReceipt,
    WorkspaceSnapshot,
)


class EvidenceGate:
    """Evaluate durable evidence without asking a model whether it is done."""

    def evaluate(
        self,
        contract: RequirementContract,
        receipt: VerificationReceipt | None,
        current_snapshot: WorkspaceSnapshot,
    ) -> GateDecision:
        if receipt is None:
            missing = tuple(
                CriterionEvidence(
                    criterion_id=item.criterion_id,
                    status=CriterionStatus.NOT_RUN,
                    reason="no verification receipt",
                )
                for item in contract.criteria
            )
            return GateDecision(
                contract_id=contract.contract_id,
                verdict=GateVerdict.PARTIAL,
                current_diff_sha256=current_snapshot.diff_sha256,
                reasons=("no verification receipt is available",),
                criteria=missing,
            )

        identity_errors: list[str] = []
        if receipt.contract_id != contract.contract_id:
            identity_errors.append("receipt contract_id does not match the requirement contract")
        if receipt.task_id != contract.task_id:
            identity_errors.append("receipt task_id does not match the requirement contract")
        if receipt.workspace_diff_sha256 != current_snapshot.diff_sha256:
            identity_errors.append("workspace changed after evidence was captured")
        if receipt.workspace_before.diff_sha256 != receipt.workspace_after.diff_sha256:
            identity_errors.append("verifier execution changed the workspace")
        if identity_errors:
            return GateDecision(
                contract_id=contract.contract_id,
                receipt_id=receipt.receipt_id,
                verdict=GateVerdict.BLOCKED,
                current_diff_sha256=current_snapshot.diff_sha256,
                stale_evidence=True,
                reasons=tuple(identity_errors),
                criteria=receipt.criteria,
            )

        results = {item.criterion_id: item for item in receipt.criteria}
        commands = {item.evidence_id: item for item in receipt.commands}
        verifier_specs = {item.verifier_id: item for item in contract.verifiers}
        required_results: list[CriterionEvidence] = []
        reasons: list[str] = []
        for criterion in contract.criteria:
            result = results.get(criterion.criterion_id)
            if result is None:
                result = CriterionEvidence(
                    criterion_id=criterion.criterion_id,
                    status=CriterionStatus.NOT_RUN,
                    reason="criterion absent from receipt",
                )
            elif criterion.verifier_ids:
                attached = [commands[evidence_id] for evidence_id in result.evidence_ids]
                attached_verifiers = {item.verifier_id for item in attached}
                missing_verifiers = set(criterion.verifier_ids) - attached_verifiers
                unexpected_verifiers = attached_verifiers - set(criterion.verifier_ids)
                failed_commands = [item for item in attached if item.status.value == "FAIL"]
                blocked_commands = [item for item in attached if item.status.value == "BLOCKED"]
                mismatched_commands = []
                for command in attached:
                    spec = verifier_specs.get(command.verifier_id)
                    if spec is None:
                        mismatched_commands.append(command.verifier_id)
                        continue
                    expected_cwd = Path(spec.cwd)
                    if not expected_cwd.is_absolute():
                        expected_cwd = Path(current_snapshot.repo_root) / expected_cwd
                    try:
                        expected_cwd = expected_cwd.resolve(strict=True)
                        actual_cwd = Path(command.cwd).resolve(strict=True)
                    except OSError:
                        mismatched_commands.append(command.verifier_id)
                        continue
                    if (
                        command.argv != spec.argv
                        or command.expected_exit_codes != spec.expected_exit_codes
                        or actual_cwd != expected_cwd
                    ):
                        mismatched_commands.append(command.verifier_id)
                if missing_verifiers:
                    result = result.model_copy(
                        update={
                            "status": CriterionStatus.NOT_RUN,
                            "reason": "missing declared verifier evidence: "
                            + ", ".join(sorted(missing_verifiers)),
                        }
                    )
                elif unexpected_verifiers:
                    result = result.model_copy(
                        update={
                            "status": CriterionStatus.BLOCKED,
                            "reason": "criterion contains evidence from undeclared verifiers: "
                            + ", ".join(sorted(unexpected_verifiers)),
                        }
                    )
                elif mismatched_commands:
                    result = result.model_copy(
                        update={
                            "status": CriterionStatus.BLOCKED,
                            "reason": "verifier command does not match the requirement contract: "
                            + ", ".join(sorted(set(mismatched_commands))),
                        }
                    )
                elif blocked_commands:
                    result = result.model_copy(
                        update={
                            "status": CriterionStatus.BLOCKED,
                            "reason": "attached verifier command is blocked",
                        }
                    )
                elif failed_commands:
                    result = result.model_copy(
                        update={
                            "status": CriterionStatus.FAIL,
                            "reason": "attached verifier command failed",
                        }
                    )
                elif result.status != CriterionStatus.PASS:
                    # Preserve NOT_RUN/BLOCKED/FAIL set by an independent scorer;
                    # command success alone must not upgrade a criterion to PASS.
                    pass
            results[criterion.criterion_id] = result
            if criterion.required:
                required_results.append(result)

        blocked = [item for item in required_results if item.status == CriterionStatus.BLOCKED]
        failed = [item for item in required_results if item.status == CriterionStatus.FAIL]
        incomplete = [item for item in required_results if item.status == CriterionStatus.NOT_RUN]
        if blocked:
            verdict = GateVerdict.BLOCKED
            reasons.append("required verification is blocked: " + ", ".join(item.criterion_id for item in blocked))
        elif failed:
            verdict = GateVerdict.FAIL
            reasons.append("required criteria failed: " + ", ".join(item.criterion_id for item in failed))
        elif incomplete:
            verdict = GateVerdict.PARTIAL
            reasons.append("required criteria lack evidence: " + ", ".join(item.criterion_id for item in incomplete))
        else:
            verdict = GateVerdict.PASS
            reasons.append("all required criteria have current PASS evidence")

        return GateDecision(
            contract_id=contract.contract_id,
            receipt_id=receipt.receipt_id,
            verdict=verdict,
            current_diff_sha256=current_snapshot.diff_sha256,
            reasons=tuple(reasons),
            criteria=tuple(results.get(item.criterion_id) or CriterionEvidence(
                criterion_id=item.criterion_id,
                status=CriterionStatus.NOT_RUN,
                reason="criterion absent from receipt",
            ) for item in contract.criteria),
        )
