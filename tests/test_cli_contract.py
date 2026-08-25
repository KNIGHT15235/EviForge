from __future__ import annotations

import json
from pathlib import Path

import pytest

from mewcode.__main__ import _load_requirement_contract
from mewcode.__main__ import main
from mewcode.config import ConfigError


def test_headless_contract_loader_roundtrips_strict_contract(tmp_path: Path) -> None:
    path = tmp_path / "contract.json"
    path.write_text(
        json.dumps(
            {
                "task_id": "task-cli",
                "objective": "prove behavior",
                "criteria": [
                    {
                        "criterion_id": "tests",
                        "description": "tests pass",
                        "verifier_ids": ["pytest"],
                    }
                ],
                "verifiers": [
                    {
                        "verifier_id": "pytest",
                        "name": "pytest",
                        "argv": ["python", "-m", "pytest", "-q"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    contract = _load_requirement_contract(path)

    assert contract.task_id == "task-cli"
    assert contract.verifiers[0].argv[-2:] == ("pytest", "-q")


def test_headless_contract_loader_fails_closed_on_unknown_fields(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text(
        json.dumps(
            {
                "task_id": "task-cli",
                "objective": "x",
                "criteria": [{"criterion_id": "x", "description": "x"}],
                "model_says_pass": True,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="Invalid requirement contract"):
        _load_requirement_contract(path)


def test_headless_main_returns_nonzero_for_invalid_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "bad.json"
    path.write_text("{not-json", encoding="utf-8")
    monkeypatch.setattr(
        "mewcode.__main__.load_config",
        lambda: type(
            "Config",
            (),
            {
                "permission_mode": "default",
                "raw_hooks": {},
                "providers": [object()],
                "mcp_servers": [],
                "enable_fork": False,
                "enable_verification_agent": False,
                "worktree": None,
                "teammate_mode": "",
                "enable_coordinator_mode": False,
            },
        )(),
    )
    monkeypatch.setattr("mewcode.__main__.load_hooks", lambda _raw: [])

    assert main(["-p", "task", "--contract", str(path)]) == 2
