from __future__ import annotations

import sys
from pathlib import Path

import pytest

from mewcode.evidence import (
    CommandStatus,
    EvidenceStore,
    TrustedArgvRunner,
    VerifierSpec,
    default_control_plane_root,
)


def test_default_store_uses_local_app_data(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    assert default_control_plane_root() == tmp_path / "EviForge" / "control-plane" / "evidence"
    assert EvidenceStore().root == (tmp_path / "EviForge" / "control-plane" / "evidence").resolve()


def test_store_deduplicates_and_validates_artifacts(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path / "control")
    first = store.put_artifact(b"same output", media_type="text/plain")
    second = store.put_artifact(b"same output", media_type="text/plain")

    assert first.sha256 == second.sha256
    assert first.relative_path == second.relative_path
    assert store.read_artifact(first) == b"same output"
    assert len(list((store.root / "artifacts").rglob(first.sha256[2:]))) == 1


@pytest.mark.asyncio
async def test_runner_treats_shell_metacharacters_as_literal_argv(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    marker = repo / "must-not-exist.txt"
    literal = f"hello; echo injected > {marker}"
    spec = VerifierSpec(
        verifier_id="literal",
        name="literal argument test",
        argv=(sys.executable, "-c", "import sys; print(sys.argv[1])", literal),
    )
    store = EvidenceStore(tmp_path / "control")

    result = await TrustedArgvRunner(store).run(
        spec,
        repo_root=repo,
        workspace_diff_sha256="a" * 64,
    )

    assert result.status == CommandStatus.PASS
    assert result.exit_code == 0
    assert literal in store.read_artifact(result.stdout).decode("utf-8")
    assert not marker.exists()


@pytest.mark.asyncio
async def test_runner_blocks_cwd_escape(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    spec = VerifierSpec(
        verifier_id="escape",
        name="cwd escape",
        argv=(sys.executable, "-c", "print('must not run')"),
        cwd="..",
    )

    result = await TrustedArgvRunner(EvidenceStore(tmp_path / "control")).run(
        spec,
        repo_root=repo,
        workspace_diff_sha256="b" * 64,
    )

    assert result.status == CommandStatus.BLOCKED
    assert result.exit_code is None
    assert "blocked verifier cwd" in (result.error or "")


@pytest.mark.asyncio
async def test_runner_records_timeout(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    spec = VerifierSpec(
        verifier_id="timeout",
        name="timeout",
        argv=(sys.executable, "-c", "import time; time.sleep(10)"),
        timeout_seconds=0.05,
    )

    result = await TrustedArgvRunner(
        EvidenceStore(tmp_path / "control"), terminate_grace_seconds=0.05
    ).run(spec, repo_root=repo, workspace_diff_sha256="c" * 64)

    assert result.status == CommandStatus.FAIL
    assert result.timed_out is True
    assert "timeout" in (result.error or "")
