from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import mewcode.__main__ as cli
from mewcode.run_result import RunResult


def fake_config():
    return SimpleNamespace(
        permission_mode="default",
        raw_hooks=[],
        providers=[SimpleNamespace(name="primary")],
    )


def test_cli_dag_and_prompt_are_mutually_exclusive(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "load_config", fake_config)
    with pytest.raises(SystemExit) as error:
        cli.main(["--dag", "graph.json", "-p", "prompt"])
    assert error.value.code == 2

def test_cli_dispatches_dag_without_launching_tui(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "load_config", fake_config)
    run = AsyncMock(return_value=7)
    monkeypatch.setattr("mewcode.orchestration.cli.run_dag_file", run)
    code = cli.main(
        [
            "--dag",
            "graph.json",
            "--dag-max-concurrency",
            "2",
            "--dag-total-tokens",
            "123",
            "--dag-wall-time",
            "4.5",
        ]
    )
    assert code == 7
    kwargs = run.await_args.kwargs
    assert kwargs["max_concurrency"] == 2
    assert kwargs["total_tokens"] == 123
    assert kwargs["wall_time_seconds"] == 4.5


def test_cli_rejects_dag_options_without_dag(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as error:
        cli.main(["--dag-total-tokens", "12"])
    assert error.value.code == 2


def test_cli_dispatches_safe_dag_resume(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "load_config", fake_config)
    resume = AsyncMock(return_value=0)
    monkeypatch.setattr("mewcode.orchestration.cli.resume_dag_file", resume)

    code = cli.main(
        [
            "dag",
            "resume",
            "graph.json",
            "run-123",
            "--max-concurrency",
            "2",
            "--allow-recovery",
        ]
    )

    assert code == 0
    assert resume.await_args.args[3:] == (cli.Path("graph.json"), "run-123")
    assert resume.await_args.kwargs["max_concurrency"] == 2
    assert resume.await_args.kwargs["allow_recovery"] is True


def test_cli_session_resume_uses_requested_session(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "load_config", fake_config)
    run = AsyncMock(
        return_value=RunResult(status="succeeded", result="continued", exit_code=0)
    )
    monkeypatch.setattr(cli, "_run_prompt", run)

    code = cli.main(
        ["session", "resume", "session-123", "-p", "continue", "--json"]
    )

    assert code == 0
    assert run.await_args.kwargs["resume_session_id"] == "session-123"
    assert run.await_args.args[3] == "continue"
    assert '"status": "succeeded"' in capsys.readouterr().out
