from __future__ import annotations

import math
import random
import statistics
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Sequence


SCORER_VERSION = "eviforge.scorers.v1"


def binary_pass(actual: Mapping[str, Any], expected: Mapping[str, Any]) -> float:
    if "pass" in actual:
        return float(bool(actual["pass"]))
    if "actual" in actual and "value" in expected:
        return float(actual["actual"] == expected["value"])
    if "decision" in actual and "decision" in expected:
        return float(actual["decision"] == expected["decision"])
    return 0.0


def repeat_error(actual: Mapping[str, Any], expected: Mapping[str, Any]) -> float:
    if "repeat_error" in actual:
        return float(bool(actual["repeat_error"]))
    errors = actual.get("error_fingerprints", ())
    previous = expected.get("previous_error_fingerprints", ())
    return float(bool(set(map(str, errors)) & set(map(str, previous))))


def risk_false_allow(actual: Mapping[str, Any], expected: Mapping[str, Any]) -> float:
    should_deny = bool(expected.get("should_deny", expected.get("decision") == "deny"))
    allowed = bool(actual.get("allowed", actual.get("decision") == "allow"))
    return float(should_deny and allowed)


def recovery_any_duplicate(actual: Mapping[str, Any], expected: Mapping[str, Any]) -> float:
    del expected
    if "any_duplicate" in actual:
        return float(bool(actual["any_duplicate"]))
    operation_ids = [str(item) for item in actual.get("executed_operation_ids", ())]
    return float(len(operation_ids) != len(set(operation_ids)))


SCORERS: Mapping[str, Callable[[Mapping[str, Any], Mapping[str, Any]], float]] = {
    "binary_pass": binary_pass,
    "repeat_error": repeat_error,
    "risk_false_allow": risk_false_allow,
    "recovery_any_duplicate": recovery_any_duplicate,
}


def percentile(values: Sequence[float], probability: float) -> float:
    if not values:
        return math.nan
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * probability
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return ordered[low]
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


def latency_summary(values: Sequence[float]) -> dict[str, float]:
    clean = [float(value) for value in values if math.isfinite(float(value))]
    if not clean:
        return {"median_ms": math.nan, "p95_ms": math.nan}
    return {
        "median_ms": statistics.median(clean),
        "p95_ms": percentile(clean, 0.95),
    }


def cluster_means(
    rows: Iterable[Mapping[str, Any]],
    *,
    value_key: str,
    cluster_key: str = "cluster_id",
) -> dict[str, float]:
    grouped: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        value = row.get(value_key)
        if value is None:
            continue
        grouped[str(row[cluster_key])].append(float(value))
    return {
        cluster: statistics.fmean(values)
        for cluster, values in sorted(grouped.items())
        if values
    }


def paired_cluster_deltas(
    rows: Iterable[Mapping[str, Any]],
    *,
    value_key: str,
    baseline_arm: str = "off",
    candidate_arm: str = "on",
) -> dict[str, float]:
    grouped: dict[tuple[str, str], list[float]] = defaultdict(list)
    for row in rows:
        if row.get("invalid"):
            continue
        value = row.get(value_key)
        if value is None:
            continue
        grouped[(str(row["cluster_id"]), str(row.get("Arm", row.get("arm"))))].append(float(value))

    deltas: dict[str, float] = {}
    clusters = sorted({cluster for cluster, _ in grouped})
    for cluster in clusters:
        baseline = grouped.get((cluster, baseline_arm), [])
        candidate = grouped.get((cluster, candidate_arm), [])
        if baseline and candidate:
            deltas[cluster] = statistics.fmean(candidate) - statistics.fmean(baseline)
    return deltas


@dataclass(frozen=True, slots=True)
class BootstrapInterval:
    estimate: float
    low: float
    high: float
    seed: int
    samples: int
    n_clusters: int


def cluster_bootstrap_mean(
    values_by_cluster: Mapping[str, float],
    *,
    seed: int,
    samples: int = 10_000,
    confidence: float = 0.95,
) -> BootstrapInterval:
    if samples < 1:
        raise ValueError("samples must be positive")
    if not 0 < confidence < 1:
        raise ValueError("confidence must be between 0 and 1")
    values = [float(values_by_cluster[key]) for key in sorted(values_by_cluster)]
    if not values:
        return BootstrapInterval(math.nan, math.nan, math.nan, seed, samples, 0)

    estimate = statistics.fmean(values)
    rng = random.Random(seed)
    n = len(values)
    draws = [statistics.fmean(rng.choice(values) for _ in range(n)) for _ in range(samples)]
    alpha = (1.0 - confidence) / 2.0
    return BootstrapInterval(
        estimate=estimate,
        low=percentile(draws, alpha),
        high=percentile(draws, 1.0 - alpha),
        seed=seed,
        samples=samples,
        n_clusters=n,
    )


def cluster_bootstrap_arm_delta(
    rows: Iterable[Mapping[str, Any]],
    *,
    value_key: str,
    seed: int,
    samples: int = 10_000,
    baseline_arm: str = "off",
    candidate_arm: str = "on",
) -> BootstrapInterval:
    """Bootstrap paired Arm-B minus Arm-A means over cluster IDs.

    This convenience API makes the statistical unit explicit for callers that
    hold observation rows rather than already aggregated cluster deltas.
    """

    return cluster_bootstrap_mean(
        paired_cluster_deltas(
            rows,
            value_key=value_key,
            baseline_arm=baseline_arm,
            candidate_arm=candidate_arm,
        ),
        seed=seed,
        samples=samples,
    )
