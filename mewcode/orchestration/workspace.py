"""Machine-readable workspace change collection for production DAG nodes."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

from .artifact_store import ArtifactStore
from .models import ChangeEnvelope, TaskEnvelope, normalize_write_scope


_IGNORED_PARTS = frozenset(
    {".git", ".pytest_cache", "__pycache__", ".venv"}
)


@dataclass(frozen=True, slots=True)
class WorkspaceSnapshot:
    files: tuple[tuple[str, str], ...]
    scopes: tuple[str, ...] = ()

    @property
    def by_path(self) -> dict[str, str]:
        return dict(self.files)


class WorkspaceChangeTracker:
    """Hash files before/after one node and emit a lease-bound diff manifest.

    Writable nodes sample only their declared scopes so disjoint nodes remain
    safe to run concurrently.  The ExecutionGateway is still the mandatory
    boundary that prevents those nodes from writing outside the same scopes.
    Read-only roles are not sampled: a whole-workspace before/after comparison
    would attribute a concurrent implementer's legitimate writes to them.
    """

    def __init__(self, workspace: str | os.PathLike[str], artifacts: ArtifactStore) -> None:
        self.workspace = Path(workspace).expanduser().resolve(strict=False)
        self.artifacts = artifacts

    def snapshot(self, envelope: TaskEnvelope) -> WorkspaceSnapshot:
        scopes = (
            (".",)
            if envelope.capability.read_only
            else tuple(envelope.capability.allowed_write_set)
        )
        files: dict[str, str] = {}
        for scope in scopes:
            for relative, path in self._paths_for_scope(scope):
                files[relative] = self._hash_path(path)
        return WorkspaceSnapshot(tuple(sorted(files.items())), scopes=scopes)

    def collect(
        self, envelope: TaskEnvelope, before: WorkspaceSnapshot
    ) -> ChangeEnvelope | None:
        after = self._snapshot_scopes(before.scopes)
        old = before.by_path
        new = after.by_path
        changed = tuple(
            sorted(path for path in old.keys() | new.keys() if old.get(path) != new.get(path))
        )
        if not changed:
            return None
        manifest = {
            "schema_version": "1.0",
            "run_id": envelope.run_id,
            "node_id": envelope.node.node_id,
            "lease_id": envelope.lease.lease_id,
            "lease_generation": envelope.lease.generation,
            "changes": [
                {"path": path, "before_sha256": old.get(path), "after_sha256": new.get(path)}
                for path in changed
            ],
        }
        encoded = json.dumps(
            manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        ref = self.artifacts.put_bytes(
            f"{envelope.node.node_id}.change-manifest",
            encoded,
            media_type="application/json",
        )
        return ChangeEnvelope.from_lease(
            envelope.lease,
            write_set=changed,
            patch_ref=ref.uri,
            metadata=(
                ("change_manifest_sha256", ref.digest or ""),
                ("changed_file_count", str(len(changed))),
            ),
        )

    def _snapshot_scopes(self, scopes: tuple[str, ...]) -> WorkspaceSnapshot:
        files: dict[str, str] = {}
        for scope in scopes:
            for relative, path in self._paths_for_scope(scope):
                files[relative] = self._hash_path(path)
        return WorkspaceSnapshot(tuple(sorted(files.items())), scopes=scopes)

    def _paths_for_scope(self, raw_scope: str):
        scope = normalize_write_scope(raw_scope)
        subtree = scope.endswith("/**")
        base_text = scope[:-3] if subtree else scope
        base = self.workspace if base_text in {"", "."} else self.workspace / base_text
        base = base.resolve(strict=False)
        try:
            base.relative_to(self.workspace)
        except ValueError:
            return
        if subtree:
            if not base.exists():
                return
            for root, directories, names in os.walk(base, followlinks=False):
                directories[:] = [name for name in directories if name not in _IGNORED_PARTS]
                root_path = Path(root)
                for name in names:
                    path = root_path / name
                    relative = path.relative_to(self.workspace).as_posix()
                    if not any(part in _IGNORED_PARTS for part in Path(relative).parts):
                        yield relative, path
        elif base == self.workspace and base.exists():
            for root, directories, names in os.walk(base, followlinks=False):
                directories[:] = [name for name in directories if name not in _IGNORED_PARTS]
                root_path = Path(root)
                for name in names:
                    path = root_path / name
                    relative = path.relative_to(self.workspace).as_posix()
                    if not any(part in _IGNORED_PARTS for part in Path(relative).parts):
                        yield relative, path
        elif base.exists() and (base.is_file() or base.is_symlink()):
            yield base.relative_to(self.workspace).as_posix(), base

    @staticmethod
    def _hash_path(path: Path) -> str:
        digest = hashlib.sha256()
        if path.is_symlink():
            digest.update(b"symlink\x00")
            digest.update(os.readlink(path).encode("utf-8", errors="surrogateescape"))
            return digest.hexdigest()
        try:
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
        except OSError:
            # A concurrent deletion is represented by absence in the next
            # snapshot; this sentinel keeps the current snapshot deterministic.
            digest.update(b"<unreadable>")
        return digest.hexdigest()


__all__ = ["WorkspaceChangeTracker", "WorkspaceSnapshot"]
