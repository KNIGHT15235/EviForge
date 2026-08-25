from __future__ import annotations

import sys
from pathlib import Path

import pytest

from mewcode.execution import ExecutionContext, ExecutionGateway, ExecutionStatus
from mewcode.tools.bash import Bash


def _planned(tmp_path: Path, argv: tuple[str, ...]) -> ExecutionContext:
    return ExecutionContext(
        task_id="planned-command",
        cwd=str(tmp_path),
        workspace_root=str(tmp_path),
        plan_hash="reviewed-plan",
        write_set=(),
        commands=(argv,),
        network_hosts=(),
    )


def test_reviewed_plan_rejects_shell_string_even_if_parser_shape_matches(
    tmp_path: Path,
) -> None:
    tool = Bash()
    context = _planned(tmp_path, ("echo", "PLAN_OK&echo", "UNDECLARED"))
    gateway = ExecutionGateway(execution_context=context)

    assessment = gateway.preview(
        tool,
        {"command": "echo PLAN_OK&echo UNDECLARED"},
        invocation_id="shell-escape",
    )

    assert not assessment.valid
    assert "manifest.exact_argv_required" in assessment.reason_codes


@pytest.mark.asyncio
async def test_reviewed_plan_executes_exact_argv_without_shell_metacharacters(
    tmp_path: Path,
) -> None:
    argv = (
        sys.executable,
        "-c",
        "import sys; print(sys.argv[1])",
        "PLAN_OK&echo UNDECLARED",
    )
    context = _planned(tmp_path, argv)
    gateway = ExecutionGateway(execution_context=context)
    tool = Bash()
    assessment = gateway.preview(tool, {"argv": list(argv)}, invocation_id="exact-argv")
    grant = gateway.issue_grant(assessment, approver="test", context=context)

    result = await gateway.invoke(
        tool,
        {"argv": list(argv)},
        invocation_id="exact-argv",
        grant=grant,
        context=context,
    )

    assert result.status is ExecutionStatus.SUCCEEDED
    assert "PLAN_OK&echo UNDECLARED" in result.output
    assert result.output.count("UNDECLARED") == 1


def test_unapproved_exact_argv_is_rejected(tmp_path: Path) -> None:
    allowed = (sys.executable, "-c", "print('allowed')")
    context = _planned(tmp_path, allowed)
    assessment = ExecutionGateway(execution_context=context).preview(
        Bash(),
        {"argv": [sys.executable, "-c", "print('different')"]},
    )

    assert not assessment.valid
    assert "manifest.command_violation" in assessment.reason_codes
