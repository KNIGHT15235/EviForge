from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from evals.schema import EvalCase, load_dataset


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def dataset_sha256(path: str | Path) -> str:
    """Hash dataset meaning, independent of JSON whitespace and row order."""

    cases = load_dataset(path)
    ordered = sorted(cases, key=lambda case: case.identity)
    payload = "\n".join(canonical_json(case.to_mapping()) for case in ordered) + "\n"
    return sha256_bytes(payload.encode("utf-8"))


def mappings_sha256(rows: Iterable[Mapping[str, Any]]) -> str:
    payload = "\n".join(canonical_json(dict(row)) for row in rows) + "\n"
    return sha256_bytes(payload.encode("utf-8"))


def verify_frozen_dataset(path: str | Path) -> Mapping[str, Any] | None:
    """Validate an optional adjacent ``.manifest.json`` freeze record."""

    dataset_path = Path(path)
    manifest_path = dataset_path.with_suffix(".manifest.json")
    if not manifest_path.is_file():
        return None
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise ValueError(f"Dataset manifest must be an object: {manifest_path}")
    expected = raw.get("dataset_sha256")
    actual = dataset_sha256(dataset_path)
    if expected != actual:
        raise ValueError(
            f"Frozen dataset digest mismatch for {dataset_path}: "
            f"manifest={expected!r}, actual={actual!r}"
        )
    return raw
