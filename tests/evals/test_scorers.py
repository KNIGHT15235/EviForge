from __future__ import annotations

import pytest

from evals.scorers import (
    cluster_bootstrap_mean,
    cluster_bootstrap_arm_delta,
    latency_summary,
    paired_cluster_deltas,
    recovery_any_duplicate,
    repeat_error,
    risk_false_allow,
)


def test_required_safety_scorers() -> None:
    assert repeat_error(
        {"error_fingerprints": ["E2", "E3"]},
        {"previous_error_fingerprints": ["E1", "E2"]},
    ) == 1.0
    assert risk_false_allow({"decision": "allow"}, {"should_deny": True}) == 1.0
    assert recovery_any_duplicate(
        {"executed_operation_ids": ["op-1", "op-2", "op-1"]}, {}
    ) == 1.0


def test_cluster_delta_aggregates_repeats_before_estimating() -> None:
    rows = [
        {"cluster_id": "a", "Arm": "off", "score": 0.0},
        {"cluster_id": "a", "Arm": "off", "score": 1.0},
        {"cluster_id": "a", "Arm": "on", "score": 1.0},
        {"cluster_id": "a", "Arm": "on", "score": 1.0},
        {"cluster_id": "b", "Arm": "off", "score": 1.0},
        {"cluster_id": "b", "Arm": "on", "score": 0.0},
        # Invalid infrastructure observations stay recorded but are not part of
        # the estimand.
        {"cluster_id": "c", "Arm": "off", "score": 1.0, "invalid": True},
        {"cluster_id": "c", "Arm": "on", "score": 0.0, "invalid": True},
    ]

    assert paired_cluster_deltas(rows, value_key="score") == {"a": 0.5, "b": -1.0}


def test_cluster_bootstrap_is_reproducible_with_fixed_seed() -> None:
    deltas = {"a": -1.0, "b": 0.0, "c": 0.5, "d": 1.0}

    first = cluster_bootstrap_mean(deltas, seed=20260812, samples=500)
    second = cluster_bootstrap_mean(deltas, seed=20260812, samples=500)
    different = cluster_bootstrap_mean(deltas, seed=3, samples=500)

    assert first == second
    assert first.estimate == 0.125
    assert (first.low, first.high) != (different.low, different.high)


def test_cluster_bootstrap_arm_delta_uses_cluster_as_resampling_unit() -> None:
    rows = [
        {"cluster_id": "a", "Arm": "off", "score": 0.0},
        {"cluster_id": "a", "Arm": "on", "score": 1.0},
        {"cluster_id": "b", "Arm": "off", "score": 1.0},
        {"cluster_id": "b", "Arm": "on", "score": 1.0},
    ]

    interval = cluster_bootstrap_arm_delta(rows, value_key="score", seed=9, samples=100)

    assert interval.n_clusters == 2
    assert interval.estimate == 0.5


def test_latency_summary_reports_median_and_p95() -> None:
    summary = latency_summary([1, 2, 3, 4, 100])

    assert summary["median_ms"] == 3
    assert summary["p95_ms"] == pytest.approx(80.8)
