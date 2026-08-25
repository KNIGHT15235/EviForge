from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from mewcode.evidence import (
    EvidenceOrchestrator,
    EvidenceStore,
    GateVerdict,
    RequirementContract,
    RequirementCriterion,
    VerifierSpec,
)


@pytest.mark.asyncio
async def test_orchestrator_emits_pass_receipt_and_bundle(git_repo: Path, tmp_path: Path) -> None:
    (git_repo / "feature.py").write_text("FEATURE = True\n", encoding="utf-8")
    contract = RequirementContract(
        contract_id="contract-e2e",
        task_id="task/evidence-e2e",
        objective="Verify the feature in a dirty Git workspace",
        criteria=(
            RequirementCriterion(
                criterion_id="python-runs",
                description="Python verifier exits successfully",
                verifier_ids=("python",),
            ),
        ),
        verifiers=(
            VerifierSpec(
                verifier_id="python",
                name="Python smoke check",
                argv=(sys.executable, "-c", "print('verified')"),
            ),
        ),
    )
    store = EvidenceStore(tmp_path / "control")

    result = await EvidenceOrchestrator(store).run(contract, repo_root=git_repo)

    assert result.decision.verdict == GateVerdict.PASS
    assert result.receipt.workspace_diff_sha256 == result.decision.current_diff_sha256
    assert "feature.py" in result.receipt.workspace_before.untracked_files
    assert result.bundle.json_path.exists()
    assert result.bundle.markdown_path.exists()
    payload = json.loads(result.bundle.json_path.read_text(encoding="utf-8"))
    assert payload["decision"]["verdict"] == "PASS"
    markdown = result.bundle.markdown_path.read_text(encoding="utf-8")
    assert "**PASS**" in markdown
    assert "feature.py" in markdown
    assert store.read_artifact(result.receipt.commands[0].stdout) == b"verified\r\n" or store.read_artifact(
        result.receipt.commands[0].stdout
    ) == b"verified\n"


@pytest.mark.asyncio
async def test_orchestrator_blocks_verifier_that_mutates_workspace(
    git_repo: Path, tmp_path: Path
) -> None:
    contract = RequirementContract(
        contract_id="contract-mutation",
        task_id="task-mutation",
        objective="Do not trust evidence after verifier-side mutation",
        criteria=(
            RequirementCriterion(
                criterion_id="immutable",
                description="verifier does not mutate source",
                verifier_ids=("mutator",),
            ),
        ),
        verifiers=(
            VerifierSpec(
                verifier_id="mutator",
                name="bad verifier",
                argv=(
                    sys.executable,
                    "-c",
                    "from pathlib import Path; Path('tracked.txt').write_text('mutated')",
                ),
            ),
        ),
    )

    result = await EvidenceOrchestrator(EvidenceStore(tmp_path / "control")).run(
        contract, repo_root=git_repo
    )

    assert result.decision.verdict == GateVerdict.BLOCKED
    assert result.decision.stale_evidence is True
    assert "verifier execution changed" in " ".join(result.decision.reasons)
