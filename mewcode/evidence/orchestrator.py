"""Headless orchestration for one contract -> receipt -> gate -> bundle run."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .bundle import BundleExporter, BundlePaths
from .gate import EvidenceGate
from .models import (
    CommandEvidence,
    CommandStatus,
    CriterionEvidence,
    CriterionStatus,
    GateDecision,
    RequirementContract,
    VerificationReceipt,
)
from .runner import TrustedArgvRunner
from .store import EvidenceStore
from .workspace import GitWorkspace


@dataclass(frozen=True, slots=True)
class EvidenceRunResult:
    receipt: VerificationReceipt
    decision: GateDecision
    bundle: BundlePaths


class EvidenceOrchestrator:
    """Run all declared verifiers sequentially and emit durable evidence."""

    def __init__(
        self,
        store: EvidenceStore | None = None,
        *,
        workspace: GitWorkspace | None = None,
        runner: TrustedArgvRunner | None = None,
        gate: EvidenceGate | None = None,
        exporter: BundleExporter | None = None,
    ) -> None:
        self.store = store or EvidenceStore()
        self.workspace = workspace or GitWorkspace()
        self.runner = runner or TrustedArgvRunner(self.store)
        self.gate = gate or EvidenceGate()
        self.exporter = exporter or BundleExporter(self.store)

    async def run(
        self,
        contract: RequirementContract,
        *,
        repo_root: str | Path,
        bundle_destination: str | Path | None = None,
    ) -> EvidenceRunResult:
        self.store.save_contract(contract)
        base_ref = contract.base_commit or "HEAD"
        before = await self.workspace.capture(repo_root, base_ref=base_ref)

        commands: list[CommandEvidence] = []
        for verifier in contract.verifiers:
            commands.append(
                await self.runner.run(
                    verifier,
                    repo_root=repo_root,
                    workspace_diff_sha256=before.diff_sha256,
                )
            )

        after = await self.workspace.capture(repo_root, base_ref=base_ref)
        command_by_id = {item.verifier_id: item for item in commands}
        criterion_results: list[CriterionEvidence] = []
        workspace_drifted = before.diff_sha256 != after.diff_sha256
        for criterion in contract.criteria:
            related = [command_by_id[item] for item in criterion.verifier_ids if item in command_by_id]
            evidence_ids = tuple(item.evidence_id for item in related)
            if workspace_drifted and related:
                status = CriterionStatus.BLOCKED
                reason = "workspace changed while verifiers were running"
            elif not related:
                status = CriterionStatus.NOT_RUN
                reason = "no verifier evidence is attached"
            elif any(item.status == CommandStatus.BLOCKED for item in related):
                status = CriterionStatus.BLOCKED
                reason = "one or more verifier commands could not be executed safely"
            elif any(item.status == CommandStatus.FAIL for item in related):
                status = CriterionStatus.FAIL
                reason = "one or more verifier commands failed"
            else:
                status = CriterionStatus.PASS
                reason = "all attached verifier commands passed"
            criterion_results.append(
                CriterionEvidence(
                    criterion_id=criterion.criterion_id,
                    status=status,
                    evidence_ids=evidence_ids,
                    reason=reason,
                )
            )

        # Evidence describes the pre-verification workspace.  If a verifier
        # mutated it, the gate compares this hash with ``after`` and blocks stale
        # evidence instead of silently blessing the new state.
        receipt = VerificationReceipt(
            contract_id=contract.contract_id,
            task_id=contract.task_id,
            workspace_diff_sha256=before.diff_sha256,
            workspace_before=before,
            workspace_after=after,
            commands=tuple(commands),
            criteria=tuple(criterion_results),
        )
        self.store.save_receipt(receipt)
        decision = self.gate.evaluate(contract, receipt, after)
        self.store.save_gate(decision)
        bundle = self.exporter.export(
            contract,
            after,
            receipt,
            decision,
            destination=bundle_destination,
        )
        return EvidenceRunResult(receipt=receipt, decision=decision, bundle=bundle)

