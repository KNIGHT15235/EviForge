from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


def run_git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    run_git(repo, "init")
    run_git(repo, "config", "user.email", "evidence-test@example.com")
    run_git(repo, "config", "user.name", "Evidence Test")
    (repo / ".gitignore").write_text("ignored.log\n", encoding="utf-8")
    (repo / "tracked.txt").write_text("baseline\n", encoding="utf-8")
    run_git(repo, "add", ".gitignore", "tracked.txt")
    run_git(repo, "commit", "-m", "baseline")
    return repo
