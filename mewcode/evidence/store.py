"""Local control-plane storage for contracts, receipts and evidence artifacts."""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

from pydantic import BaseModel

from .models import ArtifactRef, GateDecision, RequirementContract, VerificationReceipt


def default_control_plane_root() -> Path:
    """Return the user-local evidence directory.

    Windows uses ``%LOCALAPPDATA%`` as the explicit control-plane base.  The
    fallback keeps the module usable in Linux CI without adding a platformdirs
    dependency.
    """

    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        base = Path(local_app_data)
    else:
        base = Path.home() / ".local" / "share"
    return base / "EviForge" / "control-plane" / "evidence"


class EvidenceStore:
    """Filesystem store with atomic metadata writes and SHA-256 CAS artifacts."""

    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root) if root is not None else default_control_plane_root()
        self.root = self.root.expanduser().resolve()
        for name in ("artifacts", "contracts", "receipts", "gates", "bundles"):
            (self.root / name).mkdir(parents=True, exist_ok=True)

    def put_artifact(
        self,
        data: bytes,
        *,
        media_type: str = "application/octet-stream",
    ) -> ArtifactRef:
        digest = hashlib.sha256(data).hexdigest()
        relative = Path("artifacts") / "sha256" / digest[:2] / digest[2:]
        path = self.root / relative
        if not path.exists():
            self._atomic_write(path, data, exclusive=True)
        return ArtifactRef(
            sha256=digest,
            size_bytes=len(data),
            relative_path=relative.as_posix(),
            media_type=media_type,
        )

    def read_artifact(self, artifact: ArtifactRef) -> bytes:
        path = (self.root / artifact.relative_path).resolve()
        try:
            path.relative_to(self.root)
        except ValueError as exc:
            raise ValueError("artifact path escapes the evidence store") from exc
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != artifact.sha256:
            raise ValueError(f"artifact integrity check failed: {artifact.sha256}")
        return data

    def save_contract(self, contract: RequirementContract) -> Path:
        return self._save_model(Path("contracts") / f"{contract.contract_id}.json", contract)

    def save_receipt(self, receipt: VerificationReceipt) -> Path:
        return self._save_model(Path("receipts") / f"{receipt.receipt_id}.json", receipt)

    def save_gate(self, decision: GateDecision) -> Path:
        return self._save_model(Path("gates") / f"{decision.decision_id}.json", decision)

    def write_bytes(self, relative_path: str | Path, data: bytes) -> Path:
        relative = Path(relative_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("store path must be relative and must not traverse parents")
        path = self.root / relative
        self._atomic_write(path, data)
        return path

    def write_model(self, relative_path: str | Path, model: BaseModel) -> Path:
        return self.write_bytes(relative_path, model.model_dump_json(indent=2).encode("utf-8"))

    def _save_model(self, relative_path: Path, model: BaseModel) -> Path:
        return self.write_model(relative_path, model)

    @staticmethod
    def _atomic_write(path: Path, data: bytes, *, exclusive: bool = False) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            if exclusive and path.exists():
                return
            os.replace(temporary_path, path)
        finally:
            temporary_path.unlink(missing_ok=True)

