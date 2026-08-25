"""Export portable JSON and human-readable Markdown evidence bundles."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .models import EvidenceBundle, GateDecision, RequirementContract, VerificationReceipt, WorkspaceSnapshot
from .store import EvidenceStore


@dataclass(frozen=True, slots=True)
class BundlePaths:
    directory: Path
    json_path: Path
    markdown_path: Path


class BundleExporter:
    def __init__(self, store: EvidenceStore) -> None:
        self.store = store

    def export(
        self,
        contract: RequirementContract,
        snapshot: WorkspaceSnapshot,
        receipt: VerificationReceipt | None,
        decision: GateDecision,
        *,
        destination: str | Path | None = None,
    ) -> BundlePaths:
        bundle = EvidenceBundle(
            contract=contract,
            snapshot=snapshot,
            receipt=receipt,
            decision=decision,
        )
        if destination is None:
            # ``task_id`` is user-originated display data and must never become a
            # filesystem path component.  ``contract_id`` is generated/validated.
            relative = Path("bundles") / contract.contract_id / bundle.bundle_id
            directory = self.store.root / relative
            json_path = self.store.write_model(relative / "bundle.json", bundle)
            markdown_path = self.store.write_bytes(
                relative / "EVIDENCE.md", self._markdown(bundle).encode("utf-8")
            )
        else:
            directory = Path(destination).expanduser().resolve()
            directory.mkdir(parents=True, exist_ok=True)
            json_path = directory / "bundle.json"
            markdown_path = directory / "EVIDENCE.md"
            self._atomic_external_write(json_path, bundle.model_dump_json(indent=2).encode("utf-8"))
            self._atomic_external_write(markdown_path, self._markdown(bundle).encode("utf-8"))
        return BundlePaths(directory=directory, json_path=json_path, markdown_path=markdown_path)

    @staticmethod
    def _atomic_external_write(path: Path, data: bytes) -> None:
        # Reuse the store's tested same-directory atomic write primitive.
        EvidenceStore._atomic_write(path, data)

    @staticmethod
    def _markdown(bundle: EvidenceBundle) -> str:
        contract = bundle.contract
        receipt = bundle.receipt
        decision = bundle.decision
        criterion_results = {item.criterion_id: item for item in decision.criteria}
        lines = [
            f"# Evidence Bundle: {contract.task_id}",
            "",
            f"- Verdict: **{decision.verdict.value}**",
            f"- Contract: `{contract.contract_id}`",
            f"- Receipt: `{decision.receipt_id or 'none'}`",
            f"- Workspace diff: `{bundle.snapshot.diff_sha256}`",
            f"- Base commit: `{bundle.snapshot.base_commit}`",
            "",
            "## Objective",
            "",
            contract.objective,
            "",
            "## Acceptance criteria",
            "",
            "| Criterion | Required | Status | Reason |",
            "|---|---:|---|---|",
        ]
        for criterion in contract.criteria:
            result = criterion_results.get(criterion.criterion_id)
            status = result.status.value if result else "NOT_RUN"
            reason = (result.reason if result else "missing result").replace("|", "\\|").replace("\n", " ")
            lines.append(
                f"| `{criterion.criterion_id}` | {'yes' if criterion.required else 'no'} | "
                f"{status} | {reason} |"
            )

        lines.extend(["", "## Workspace changes", ""])
        if bundle.snapshot.changed_files:
            lines.extend(f"- `{path}`" for path in bundle.snapshot.changed_files)
        else:
            lines.append("- No tracked or non-ignored untracked changes.")

        lines.extend(["", "## Verifier commands", ""])
        if receipt and receipt.commands:
            for command in receipt.commands:
                rendered_argv = json.dumps(list(command.argv), ensure_ascii=False)
                lines.extend(
                    [
                        f"### {command.verifier_id}: {command.status.value}",
                        "",
                        f"- argv: `{rendered_argv}`",
                        f"- exit code: `{command.exit_code}`",
                        f"- duration: `{command.duration_ms:.2f} ms`",
                        f"- stdout artifact: `{command.stdout.sha256}`",
                        f"- stderr artifact: `{command.stderr.sha256}`",
                        f"- error: {command.error or 'none'}",
                        "",
                    ]
                )
        else:
            lines.append("No verifier commands were executed.")

        lines.extend(["", "## Gate reasons", ""])
        lines.extend(f"- {reason}" for reason in decision.reasons)
        lines.append("")
        return "\n".join(lines)
