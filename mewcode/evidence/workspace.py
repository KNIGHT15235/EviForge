"""Deterministic Git workspace snapshots used to bind verification evidence."""

from __future__ import annotations

import asyncio
import hashlib
import os
import stat
from pathlib import Path

from .models import WorkspaceSnapshot


class WorkspaceError(RuntimeError):
    pass


async def _git(repo_root: Path, *args: str) -> bytes:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": ""}
    try:
        process = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=str(repo_root),
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        raise WorkspaceError(f"unable to start git: {exc}") from exc
    stdout, stderr = await process.communicate()
    if process.returncode != 0:
        message = stderr.decode("utf-8", errors="replace").strip()
        raise WorkspaceError(f"git {' '.join(args)} failed: {message[:1000]}")
    return stdout


def _decode_nul_paths(payload: bytes) -> tuple[str, ...]:
    paths = [os.fsdecode(item) for item in payload.split(b"\0") if item]
    return tuple(sorted(path.replace("\\", "/") for path in paths))


def _write_frame(hasher: "hashlib._Hash", label: bytes, payload: bytes) -> None:
    hasher.update(len(label).to_bytes(4, "big"))
    hasher.update(label)
    hasher.update(len(payload).to_bytes(8, "big"))
    hasher.update(payload)


def _hash_regular_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            size += len(chunk)
            digest.update(chunk)
    return digest.hexdigest(), size


def _hash_untracked(repo_root: Path, paths: tuple[str, ...]) -> str:
    manifest = hashlib.sha256()
    manifest.update(b"eviforge-untracked-v1\0")
    for relative_name in paths:
        relative = Path(relative_name)
        path = repo_root / relative
        try:
            path.parent.resolve(strict=True).relative_to(repo_root)
        except (OSError, ValueError) as exc:
            raise WorkspaceError(f"untracked path escapes repository: {relative_name}") from exc

        try:
            metadata = path.lstat()
        except OSError as exc:
            raise WorkspaceError(f"cannot inspect untracked path: {relative_name}") from exc

        if stat.S_ISLNK(metadata.st_mode):
            kind = b"symlink"
            target = os.fsencode(os.readlink(path))
            content_digest = hashlib.sha256(target).hexdigest()
            size = len(target)
        elif stat.S_ISREG(metadata.st_mode):
            kind = b"file"
            content_digest, size = _hash_regular_file(path)
        else:
            kind = b"other"
            content_digest = hashlib.sha256(b"").hexdigest()
            size = metadata.st_size

        _write_frame(manifest, b"path", os.fsencode(relative_name.replace("\\", "/")))
        _write_frame(manifest, b"kind", kind)
        _write_frame(manifest, b"mode", str(stat.S_IMODE(metadata.st_mode)).encode("ascii"))
        _write_frame(manifest, b"size", str(size).encode("ascii"))
        _write_frame(manifest, b"sha256", content_digest.encode("ascii"))
    return manifest.hexdigest()


async def capture_workspace(
    repo_root: str | Path,
    *,
    base_ref: str = "HEAD",
) -> WorkspaceSnapshot:
    """Capture tracked and non-ignored untracked changes under ``repo_root``.

    The combined ``diff_sha256`` changes when either Git's binary tracked diff or
    the path/mode/content manifest of an untracked file changes.  Ignored files
    are intentionally excluded, matching ``git status --porcelain`` semantics.
    """

    if not base_ref or base_ref.startswith("-") or "\x00" in base_ref:
        raise WorkspaceError("base_ref must be a safe git commit-ish")
    root = Path(repo_root).expanduser().resolve(strict=True)
    git_dir = (await _git(root, "rev-parse", "--show-toplevel")).decode().strip()
    actual_root = Path(git_dir).resolve(strict=True)
    if actual_root != root:
        raise WorkspaceError(f"repo_root must be the Git top-level directory: {actual_root}")

    base_commit = (await _git(root, "rev-parse", "--verify", f"{base_ref}^{{commit}}"))
    head_commit = await _git(root, "rev-parse", "--verify", "HEAD^{commit}")
    tracked_diff = await _git(root, "diff", "--binary", "--no-ext-diff", base_ref, "--")
    tracked_paths_raw = await _git(root, "diff", "--name-only", "-z", base_ref, "--")
    untracked_paths_raw = await _git(root, "ls-files", "--others", "--exclude-standard", "-z")

    tracked_files = _decode_nul_paths(tracked_paths_raw)
    untracked_files = _decode_nul_paths(untracked_paths_raw)
    tracked_hash = hashlib.sha256(tracked_diff).hexdigest()
    untracked_hash = _hash_untracked(root, untracked_files)

    combined = hashlib.sha256()
    combined.update(b"eviforge-workspace-v1\0")
    _write_frame(combined, b"base_commit", base_commit.strip())
    _write_frame(combined, b"tracked_diff_sha256", tracked_hash.encode("ascii"))
    _write_frame(combined, b"untracked_sha256", untracked_hash.encode("ascii"))

    return WorkspaceSnapshot(
        repo_root=str(root),
        base_ref=base_ref,
        base_commit=base_commit.decode("ascii").strip(),
        head_commit=head_commit.decode("ascii").strip(),
        tracked_diff_sha256=tracked_hash,
        untracked_sha256=untracked_hash,
        diff_sha256=combined.hexdigest(),
        tracked_files=tracked_files,
        untracked_files=untracked_files,
    )


class GitWorkspace:
    """Small injectable adapter around :func:`capture_workspace`."""

    async def capture(self, repo_root: str | Path, *, base_ref: str = "HEAD") -> WorkspaceSnapshot:
        return await capture_workspace(repo_root, base_ref=base_ref)

