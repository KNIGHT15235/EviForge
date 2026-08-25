"""Canonical binding and filesystem hash helpers."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping


MISSING_FILE_HASH = "missing"


def canonical_json_hash(value: Mapping[str, Any]) -> str:
    """Hash JSON arguments after stable, lossless normalization.

    Values outside JSON are rejected instead of stringified, preventing two
    processes from binding an approval to different interpretations.
    """

    encoded = json.dumps(
        dict(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def canonical_realpath(path: str | os.PathLike[str]) -> str:
    return os.path.normcase(str(Path(path).expanduser().resolve(strict=False)))


def bytes_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_sha256(path: str | os.PathLike[str]) -> str:
    source = Path(path)
    if not source.exists():
        return MISSING_FILE_HASH
    digest = hashlib.sha256()
    with source.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
