"""Host-owned content-addressed artifacts for typed DAG dependencies.

The scheduler passes references between nodes, but a reference is useful only
when the receiving process can resolve and verify it.  ``ArtifactStore`` keeps
the bytes outside model-controlled paths, verifies every read against its
SHA-256 address, and exposes bounded previews for prompt injection.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from .models import ArtifactRef


class ArtifactStoreError(RuntimeError):
    """Raised when an artifact is missing, corrupt, or outside the CAS."""


@dataclass(frozen=True, slots=True)
class ArtifactPreview:
    content: str
    injected_bytes: int
    total_bytes: int
    truncated: bool


class ArtifactStore:
    """A small immutable SHA-256 CAS with atomic, user-local writes."""

    URI_SCHEME = "eviforge-cas"

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        max_blob_bytes: int = 1_048_576,
    ) -> None:
        if max_blob_bytes < 1:
            raise ValueError("max_blob_bytes must be positive")
        self.root = Path(root).expanduser().resolve(strict=False)
        self.blob_root = self.root / "blobs" / "sha256"
        self.blob_root.mkdir(parents=True, exist_ok=True)
        self.max_blob_bytes = max_blob_bytes

    @classmethod
    def temporary(cls) -> "ArtifactStore":
        return cls(Path(tempfile.mkdtemp(prefix="eviforge-dag-cas-")))

    def put_text(self, name: str, text: str) -> ArtifactRef:
        return self.put_bytes(name, text.encode("utf-8"), media_type="text/plain")

    def put_bytes(
        self,
        name: str,
        payload: bytes,
        *,
        media_type: str = "application/octet-stream",
    ) -> ArtifactRef:
        if len(payload) > self.max_blob_bytes:
            raise ArtifactStoreError(
                f"artifact exceeds {self.max_blob_bytes} byte CAS limit"
            )
        digest = hashlib.sha256(payload).hexdigest()
        target = self._path_for_digest(digest)
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{digest}.", suffix=".tmp", dir=target.parent
            )
            temporary = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                try:
                    os.chmod(temporary, 0o600)
                except OSError:
                    pass
                # Identical concurrent writers are harmless.  os.replace is
                # the only publication point; readers never observe a prefix.
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
        return ArtifactRef(
            name=name,
            uri=f"{self.URI_SCHEME}://sha256/{digest}",
            digest=f"sha256:{digest}",
            media_type=media_type,
            size_bytes=len(payload),
        )

    def read_bytes(self, artifact: ArtifactRef) -> bytes:
        digest = self._digest_from_ref(artifact)
        target = self._path_for_digest(digest)
        try:
            payload = target.read_bytes()
        except OSError as exc:
            raise ArtifactStoreError(f"artifact is unavailable: {artifact.uri}") from exc
        if len(payload) > self.max_blob_bytes:
            raise ArtifactStoreError("stored artifact exceeds configured CAS limit")
        if artifact.size_bytes is not None and artifact.size_bytes != len(payload):
            raise ArtifactStoreError("artifact size metadata does not match stored content")
        actual = hashlib.sha256(payload).hexdigest()
        if actual != digest:
            raise ArtifactStoreError("artifact content does not match its SHA-256 address")
        return payload

    def preview_text(self, artifact: ArtifactRef, *, max_bytes: int) -> ArtifactPreview:
        if max_bytes < 0:
            raise ValueError("max_bytes must not be negative")
        payload = self.read_bytes(artifact)
        selected = payload[:max_bytes]
        # Avoid ending a preview in the middle of a UTF-8 sequence.
        content = selected.decode("utf-8", errors="ignore")
        injected = len(content.encode("utf-8"))
        return ArtifactPreview(
            content=content,
            injected_bytes=injected,
            total_bytes=len(payload),
            truncated=injected < len(payload),
        )

    def _path_for_digest(self, digest: str) -> Path:
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ArtifactStoreError("invalid SHA-256 artifact address")
        target = (self.blob_root / digest[:2] / digest).resolve(strict=False)
        try:
            target.relative_to(self.blob_root)
        except ValueError as exc:  # pragma: no cover - digest validation already prevents it
            raise ArtifactStoreError("artifact address escapes CAS root") from exc
        return target

    def _digest_from_ref(self, artifact: ArtifactRef) -> str:
        parsed = urlparse(artifact.uri)
        if parsed.scheme != self.URI_SCHEME or parsed.netloc != "sha256":
            raise ArtifactStoreError(f"unsupported artifact URI: {artifact.uri}")
        digest = parsed.path.lstrip("/").casefold()
        expected = f"sha256:{digest}"
        if artifact.digest is not None and artifact.digest.casefold() != expected:
            raise ArtifactStoreError("artifact metadata digest does not match URI")
        return digest


__all__ = ["ArtifactPreview", "ArtifactStore", "ArtifactStoreError"]
