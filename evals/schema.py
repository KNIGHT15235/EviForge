from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence


DATASET_SCHEMA_VERSION = "eviforge.eval.dataset.v1"
RUN_SCHEMA_VERSION = "eviforge.eval.run.v1"
MANIFEST_SCHEMA_VERSION = "eviforge.eval.manifest.v1"


class DatasetValidationError(ValueError):
    """Raised when a frozen dataset does not satisfy its declared schema."""


@dataclass(frozen=True, slots=True)
class EvalCase:
    schema_version: str
    experiment: str
    stratum: str
    cluster_id: str
    run_id: str
    seed: int
    case: Mapping[str, Any]
    expected: Mapping[str, Any]
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any], *, line_number: int = 0) -> "EvalCase":
        location = f" at JSONL line {line_number}" if line_number else ""
        required = (
            "schema_version",
            "experiment",
            "stratum",
            "cluster_id",
            "run_id",
            "seed",
            "case",
            "expected",
        )
        missing = [key for key in required if key not in raw]
        if missing:
            raise DatasetValidationError(f"Missing {', '.join(missing)}{location}")
        if raw["schema_version"] != DATASET_SCHEMA_VERSION:
            raise DatasetValidationError(
                f"Unsupported schema_version {raw['schema_version']!r}{location}; "
                f"expected {DATASET_SCHEMA_VERSION!r}"
            )

        text_fields = ("experiment", "stratum", "cluster_id", "run_id")
        for key in text_fields:
            if not isinstance(raw[key], str) or not raw[key].strip():
                raise DatasetValidationError(f"{key} must be a non-empty string{location}")
        if isinstance(raw["seed"], bool) or not isinstance(raw["seed"], int):
            raise DatasetValidationError(f"seed must be an integer{location}")
        if not isinstance(raw["case"], Mapping):
            raise DatasetValidationError(f"case must be an object{location}")
        if not isinstance(raw["expected"], Mapping):
            raise DatasetValidationError(f"expected must be an object{location}")
        metadata = raw.get("metadata", {})
        if not isinstance(metadata, Mapping):
            raise DatasetValidationError(f"metadata must be an object{location}")

        return cls(
            schema_version=str(raw["schema_version"]),
            experiment=str(raw["experiment"]),
            stratum=str(raw["stratum"]),
            cluster_id=str(raw["cluster_id"]),
            run_id=str(raw["run_id"]),
            seed=int(raw["seed"]),
            case=dict(raw["case"]),
            expected=dict(raw["expected"]),
            metadata=dict(metadata),
        )

    @property
    def identity(self) -> tuple[str, str, str, str, int]:
        return self.experiment, self.stratum, self.cluster_id, self.run_id, self.seed

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "experiment": self.experiment,
            "stratum": self.stratum,
            "cluster_id": self.cluster_id,
            "run_id": self.run_id,
            "seed": self.seed,
            "case": dict(self.case),
            "expected": dict(self.expected),
            "metadata": dict(self.metadata),
        }


def load_dataset(path: str | Path) -> list[EvalCase]:
    dataset_path = Path(path)
    cases: list[EvalCase] = []
    with dataset_path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            try:
                raw = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise DatasetValidationError(
                    f"Invalid JSON at line {line_number}: {exc.msg}"
                ) from exc
            if not isinstance(raw, Mapping):
                raise DatasetValidationError(f"JSONL line {line_number} must be an object")
            cases.append(EvalCase.from_mapping(raw, line_number=line_number))

    if not cases:
        raise DatasetValidationError(f"Dataset is empty: {dataset_path}")
    _validate_dataset_relations(cases)
    return cases


def _validate_dataset_relations(cases: Sequence[EvalCase]) -> None:
    identities: set[tuple[str, str, str, str, int]] = set()
    by_run_id: dict[str, EvalCase] = {}
    for case in cases:
        if case.identity in identities:
            raise DatasetValidationError(f"Duplicate dataset identity: {case.identity!r}")
        identities.add(case.identity)
        if case.run_id in by_run_id:
            raise DatasetValidationError(
                f"run_id must be unique inside a dataset: {case.run_id!r}"
            )
        by_run_id[case.run_id] = case

    # A rewrite/paraphrase is an intra-cluster repeated observation.  This
    # validation prevents accidental sample-size inflation by relabelling a
    # rewrite as a new independent cluster.
    for case in cases:
        rewrite_of = case.metadata.get("rewrite_of")
        if rewrite_of is None:
            continue
        if not isinstance(rewrite_of, str) or rewrite_of not in by_run_id:
            raise DatasetValidationError(
                f"rewrite_of for {case.run_id!r} must reference an existing run_id"
            )
        source = by_run_id[rewrite_of]
        if source.cluster_id != case.cluster_id:
            raise DatasetValidationError(
                f"Rewrite {case.run_id!r} must retain cluster_id "
                f"{source.cluster_id!r}, not {case.cluster_id!r}"
            )
        if source.experiment != case.experiment:
            raise DatasetValidationError(
                f"Rewrite {case.run_id!r} cannot cross experiments"
            )
