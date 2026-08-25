from __future__ import annotations

from pathlib import Path

import pytest

from mewcode.execution import ExecutionGateway, ExecutionStatus, ReasonCode, RiskEngine
from mewcode.permissions import (
    DangerousCommandDetector,
    PathSandbox,
    PermissionChecker,
    PermissionMode,
    RuleEngine,
)
from mewcode.tools.read_file import ReadFile
from mewcode.tools.write_file import WriteFile


def checker(root: Path, mode: PermissionMode) -> PermissionChecker:
    return PermissionChecker(
        detector=DangerousCommandDetector(),
        sandbox=PathSandbox(str(root)),
        rule_engine=RuleEngine(),
        mode=mode,
    )


@pytest.mark.asyncio
async def test_existing_read_file_runs_without_adapter(tmp_path: Path) -> None:
    target = tmp_path / "hello.txt"
    target.write_text("hello\nworld", encoding="utf-8")
    gateway = ExecutionGateway(
        permission_checker=checker(tmp_path, PermissionMode.DEFAULT),
        risk_engine=RiskEngine(workspace_root=tmp_path),
    )

    result = await gateway.invoke(ReadFile(), {"file_path": str(target), "limit": 1})

    assert result.status is ExecutionStatus.SUCCEEDED
    assert result.risk_level == "L0"
    assert result.output == "1\thello"


@pytest.mark.asyncio
async def test_existing_write_file_runs_without_adapter(tmp_path: Path) -> None:
    target = tmp_path / "new.txt"
    gateway = ExecutionGateway(
        permission_checker=checker(tmp_path, PermissionMode.ACCEPT_EDITS),
        risk_engine=RiskEngine(workspace_root=tmp_path),
    )

    result = await gateway.invoke(
        WriteFile(),
        {"file_path": str(target), "content": "created by gateway"},
    )

    assert result.status is ExecutionStatus.SUCCEEDED
    assert result.risk_level == "L1"
    assert target.read_text(encoding="utf-8") == "created by gateway"


@pytest.mark.asyncio
async def test_l4_reserved_path_precedes_legacy_bypass(tmp_path: Path) -> None:
    gateway = ExecutionGateway(
        permission_checker=checker(tmp_path, PermissionMode.BYPASS),
        risk_engine=RiskEngine(workspace_root=tmp_path),
    )

    result = await gateway.invoke(
        WriteFile(),
        {
            "file_path": str(tmp_path / ".eviforge" / "runtime.db"),
            "content": "corrupt control plane",
        },
    )

    assert result.status is ExecutionStatus.PERMISSION_DENIED
    assert ReasonCode.L4_RESERVED_PATH.value in result.reason_codes
    assert not (tmp_path / ".eviforge" / "runtime.db").exists()
