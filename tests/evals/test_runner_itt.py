from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Mapping

import pytest

from evals.adapters import AdapterOutput
from evals.runner import ExperimentConfig, execute_experiment
from evals.schema import DATASET_SCHEMA_VERSION


class InfrastructureFailure(RuntimeError):
    infrastructure_failure = "filesystem_unavailable"


class OutcomeAdapter:
    name = "outcomes"

    async def run(
        self,
        case: Mapping[str, Any],
        *,
        feature_enabled: bool,
        seed: int,
    ) -> AdapterOutput:
        del feature_enabled, seed
        outcome = case["outcome"]
        if outcome == "timeout":
            await asyncio.sleep(0.1)
        if outcome == "crash":
            raise RuntimeError("candidate bug")
        if outcome == "infrastructure":
            raise InfrastructureFailure("disk unavailable")
        if outcome == "empty":
            return AdapterOutput(actual={}, latency_ms=0.1)
        return AdapterOutput(actual={"pass": True}, latency_ms=0.1)


def _write_dataset(path: Path) -> None:
    outcomes = ("pass", "timeout", "crash", "empty", "infrastructure")
    rows = [
        {
            "schema_version": DATASET_SCHEMA_VERSION,
            "experiment": "itt_test",
            "stratum": "fixture",
            "cluster_id": f"cluster-{outcome}",
            "run_id": f"run-{outcome}",
            "seed": index,
            "case": {"outcome": outcome},
            "expected": {"pass": True},
        }
        for index, outcome in enumerate(outcomes)
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


@pytest.mark.asyncio
async def test_itt_keeps_all_scheduled_failures_and_only_preregistered_infra_is_invalid(
    tmp_path: Path,
) -> None:
    dataset = tmp_path / "dataset.jsonl"
    _write_dataset(dataset)
    config = ExperimentConfig(
        dataset_path=dataset,
        output_root=tmp_path / "results",
        adapter_name="outcomes",
        experiment="itt_test",
        stratum="fixture",
        contrast="on_minus_off",
        primary_scorer="binary_pass",
        warmup=0,
        repeats=1,
        timeout_seconds=0.01,
        bootstrap_samples=100,
        result_dir_name="run",
    )

    result = await execute_experiment(config, OutcomeAdapter(), repo_root=Path.cwd())

    assert len(result.rows) == 10  # five fixtures, paired off/on
    counts = {status: sum(row["status"] == status for row in result.rows) for status in {
        "completed", "timeout", "crash", "no_output"
    }}
    assert counts == {"completed": 2, "timeout": 2, "crash": 4, "no_output": 2}
    ordinary_failures = [row for row in result.rows if row["dataset_run_id"] in {
        "run-timeout", "run-crash", "run-empty"
    }]
    assert all(not row["invalid"] for row in ordinary_failures)
    assert all(row["scores"]["binary_pass"] == 0.0 for row in ordinary_failures)
    infra = [row for row in result.rows if row["dataset_run_id"] == "run-infrastructure"]
    assert len(infra) == 2
    assert all(row["invalid"] for row in infra)
    assert all(row["invalid_reason"] == "filesystem_unavailable" for row in infra)
    assert len(list((result.result_dir / "failures").glob("*.json"))) == 8
