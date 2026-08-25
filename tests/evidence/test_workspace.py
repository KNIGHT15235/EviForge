from __future__ import annotations

from pathlib import Path

import pytest

from mewcode.evidence import WorkspaceError, capture_workspace


@pytest.mark.asyncio
async def test_snapshot_hashes_tracked_and_untracked_content(git_repo: Path) -> None:
    clean = await capture_workspace(git_repo)

    (git_repo / "tracked.txt").write_text("modified\n", encoding="utf-8")
    (git_repo / "new.txt").write_text("one\n", encoding="utf-8")
    changed = await capture_workspace(git_repo)

    assert changed.diff_sha256 != clean.diff_sha256
    assert changed.tracked_files == ("tracked.txt",)
    assert changed.untracked_files == ("new.txt",)

    tracked_hash = changed.tracked_diff_sha256
    untracked_hash = changed.untracked_sha256
    (git_repo / "new.txt").write_text("two\n", encoding="utf-8")
    untracked_changed = await capture_workspace(git_repo)
    assert untracked_changed.tracked_diff_sha256 == tracked_hash
    assert untracked_changed.untracked_sha256 != untracked_hash
    assert untracked_changed.diff_sha256 != changed.diff_sha256


@pytest.mark.asyncio
async def test_snapshot_ignores_gitignored_files(git_repo: Path) -> None:
    before = await capture_workspace(git_repo)
    (git_repo / "ignored.log").write_text("not evidence\n", encoding="utf-8")
    after = await capture_workspace(git_repo)

    assert after.diff_sha256 == before.diff_sha256
    assert "ignored.log" not in after.untracked_files


@pytest.mark.asyncio
async def test_snapshot_requires_repository_top_level(git_repo: Path) -> None:
    child = git_repo / "child"
    child.mkdir()

    with pytest.raises(WorkspaceError, match="top-level"):
        await capture_workspace(child)
