"""Immutable SHA-256 evidence; current workspace files are never the archive."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from eviforge.dag.graph import DAGError
from eviforge.dag.models import ArtifactRef


class ArtifactStore:
    def __init__(self, directory: Path):
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)

    def capture(self, path: Path, *, relative_path: str, run_id: str,
                node_id: str, attempt: int) -> ArtifactRef:
        return self.capture_bytes(path.read_bytes(), relative_path=relative_path, run_id=run_id,
                                  node_id=node_id, attempt=attempt)

    def capture_bytes(self, data: bytes, *, relative_path: str, run_id: str,
                      node_id: str, attempt: int) -> ArtifactRef:
        digest = hashlib.sha256(data).hexdigest()
        target = self.directory / digest
        try:
            with target.open("xb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            if target.read_bytes() != data:
                raise DAGError("Corrupt existing artifact")
        return ArtifactRef(sha256=digest, size=len(data), path=relative_path,
                           run_id=run_id, node_id=node_id, attempt=attempt)

    def verify(self, ref: ArtifactRef) -> None:
        try:
            data = (self.directory / ref.sha256).read_bytes()
        except OSError as exc:
            raise DAGError(f"Missing artifact: {ref.sha256}") from exc
        if len(data) != ref.size or hashlib.sha256(data).hexdigest() != ref.sha256:
            raise DAGError(f"Artifact integrity mismatch: {ref.sha256}")
