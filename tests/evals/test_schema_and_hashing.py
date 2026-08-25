from __future__ import annotations

import json
from pathlib import Path

import pytest

from evals.hashing import dataset_sha256
from evals.schema import DATASET_SCHEMA_VERSION, DatasetValidationError, load_dataset


def _row(run_id: str, cluster_id: str, **metadata: object) -> dict[str, object]:
    return {
        "schema_version": DATASET_SCHEMA_VERSION,
        "experiment": "test",
        "stratum": "fixture",
        "cluster_id": cluster_id,
        "run_id": run_id,
        "seed": 7,
        "case": {"text": run_id},
        "expected": {"pass": True},
        "metadata": metadata,
    }


def test_dataset_hash_is_stable_across_json_formatting_and_row_order(tmp_path: Path) -> None:
    first = _row("a", "cluster-a")
    second = _row("b", "cluster-b")
    compact = tmp_path / "compact.jsonl"
    pretty = tmp_path / "pretty.jsonl"
    compact.write_text(
        json.dumps(first, separators=(",", ":")) + "\n" + json.dumps(second) + "\n",
        encoding="utf-8",
    )
    pretty.write_text(
        json.dumps(second, indent=2).replace("\n", " ")
        + "\n"
        + json.dumps(first, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )

    assert dataset_sha256(compact) == dataset_sha256(pretty)


def test_rewrite_cannot_masquerade_as_independent_cluster(tmp_path: Path) -> None:
    source = _row("source", "same-root-cause")
    rewrite = _row("rewrite", "inflated-cluster", rewrite_of="source")
    path = tmp_path / "invalid.jsonl"
    path.write_text(
        json.dumps(source) + "\n" + json.dumps(rewrite) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(DatasetValidationError, match="must retain cluster_id"):
        load_dataset(path)


def test_frozen_mechanism_dataset_is_valid() -> None:
    path = Path("evals/datasets/risk_policy_mechanism.v1.jsonl")
    cases = load_dataset(path)

    assert len(cases) == 6
    assert len({case.cluster_id for case in cases}) == 4
    assert dataset_sha256(path) == "9d0374ff59d8ab35431773d43dd7028fd39e876a6a7d8b28bf792f8d6106c604"
