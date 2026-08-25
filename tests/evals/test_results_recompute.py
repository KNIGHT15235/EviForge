from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from evals.adapters import RiskPolicyMechanismAdapter
from evals.report import write_reports
from evals.runner import ExperimentConfig, execute_experiment


@pytest.mark.asyncio
async def test_result_artifacts_can_be_recomputed_from_manifest_and_runs(tmp_path: Path) -> None:
    dataset = Path("evals/datasets/risk_policy_mechanism.v1.jsonl")
    config = ExperimentConfig(
        dataset_path=dataset,
        output_root=tmp_path,
        adapter_name="risk-policy-mechanism",
        experiment="risk_policy_mechanism_v1",
        stratum="deterministic_safe_fixtures",
        contrast="risk_policy_on_minus_legacy_category_policy",
        primary_scorer="risk_false_allow",
        secondary_scorers=("binary_pass",),
        baseline_sha="baseline-test-sha",
        candidate_sha="candidate-test-sha",
        feature_flag="risk_policy",
        warmup=0,
        repeats=2,
        bootstrap_seed=7,
        bootstrap_samples=200,
        command=("python", "-m", "evals.run", "mechanism"),
        result_dir_name="fixed",
    )
    result = await execute_experiment(
        config,
        RiskPolicyMechanismAdapter(workspace_root=Path.cwd()),
        repo_root=Path.cwd(),
    )
    first = write_reports(result.result_dir)
    csv_before = (result.result_dir / "summary.csv").read_bytes()
    markdown_before = (result.result_dir / "summary.md").read_bytes()

    second = write_reports(result.result_dir)

    assert first == second
    assert (result.result_dir / "summary.csv").read_bytes() == csv_before
    assert (result.result_dir / "summary.md").read_bytes() == markdown_before
    manifest = json.loads((result.result_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["baseline_sha"] == "baseline-test-sha"
    assert manifest["candidate_sha"] == "candidate-test-sha"
    assert manifest["dataset"]["dataset_sha256"]
    assert manifest["uv_lock_sha256"]
    assert manifest["feature_flags"] == {"risk_policy": [False, True]}
    assert manifest["arm_a"]["runtime_sha"] == "candidate-test-sha"
    assert manifest["arm_b"]["runtime_sha"] == "candidate-test-sha"
    assert manifest["paired_runtime_invariant"].startswith("same_candidate_runtime")
    assert isinstance(manifest["repository_state_at_start"]["dirty"], bool)
    assert manifest["warmup"] == 0
    assert manifest["repeats"] == 2
    assert manifest["command"]
    assert manifest["environment"]["python"]
    assert manifest["environment"]["os"]
    assert manifest["environment"]["cpu"]
    assert manifest["generalization_claim"] == "not_an_llm_general_capability_evaluation"
    assert set(manifest["artifacts"]) == {"runs.jsonl", "summary.csv", "summary.md"}

    with (result.result_dir / "summary.csv").open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert {row["EndpointRole"] for row in rows} == {"Primary", "Secondary"}
    assert all(row["Contrast"] for row in rows)
    assert all(row["ArmA"] == "off" and row["ArmB"] == "on" for row in rows)
    assert (result.result_dir / "runs.jsonl").is_file()
    assert (result.result_dir / "failures").is_dir()
    first_run = json.loads((result.result_dir / "runs.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert first_run["Arm"] in {"off", "on"}
    assert first_run["Contrast"] == "risk_policy_on_minus_legacy_category_policy"
    assert first_run["Primary"] == "risk_false_allow"


@pytest.mark.asyncio
async def test_recompute_rejects_modified_raw_runs(tmp_path: Path) -> None:
    dataset = Path("evals/datasets/risk_policy_mechanism.v1.jsonl")
    config = ExperimentConfig(
        dataset_path=dataset,
        output_root=tmp_path,
        adapter_name="risk-policy-mechanism",
        experiment="risk_policy_mechanism_v1",
        stratum="deterministic_safe_fixtures",
        contrast="on_minus_off",
        primary_scorer="risk_false_allow",
        warmup=0,
        repeats=1,
        bootstrap_samples=20,
        result_dir_name="tamper",
    )
    result = await execute_experiment(
        config,
        RiskPolicyMechanismAdapter(workspace_root=Path.cwd()),
        repo_root=Path.cwd(),
    )
    write_reports(result.result_dir)
    runs = result.result_dir / "runs.jsonl"
    runs.write_text(runs.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="digest does not match"):
        write_reports(result.result_dir)
