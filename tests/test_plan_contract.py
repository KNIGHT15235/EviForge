from __future__ import annotations

import pytest

from mewcode.plan_contract import compile_plan


PLAN = """# Plan
<!-- eviforge:write "mewcode/feature.py" -->
<!-- eviforge:network "pypi.org" -->
<!-- eviforge:command ["python", "-m", "pytest", "-q"] -->
<!-- eviforge:criterion {"id":"unit","description":"unit tests pass","argv":["python","-m","pytest","-q"],"timeout":60} -->
"""


def test_compile_plan_builds_bound_contract_and_manifest(tmp_path) -> None:
    first = compile_plan(
        PLAN,
        task_id="task-1",
        objective="add feature",
        workspace_root=tmp_path,
        base_commit="a" * 40,
    )
    second = compile_plan(
        PLAN.replace("\n", "\r\n"),
        task_id="task-1",
        objective="add feature",
        workspace_root=tmp_path,
        base_commit="a" * 40,
    )

    assert first.plan_hash == second.plan_hash
    assert first.write_set == ("mewcode/feature.py",)
    assert first.commands[0] == ("python", "-m", "pytest", "-q")
    assert first.contract.criteria[0].verifier_ids == ("verify-unit",)


def test_compile_plan_rejects_implicit_or_escaping_scope(tmp_path) -> None:
    with pytest.raises(ValueError, match="no explicit acceptance"):
        compile_plan("# prose only", task_id="task", objective="x", workspace_root=tmp_path)
    with pytest.raises(ValueError, match="escapes"):
        compile_plan(
            '<!-- eviforge:write "../outside" -->\n'
            '<!-- eviforge:criterion {"id":"x","description":"x"} -->',
            task_id="task",
            objective="x",
            workspace_root=tmp_path,
        )


def test_command_is_argv_not_shell_string(tmp_path) -> None:
    with pytest.raises(ValueError, match="argv"):
        compile_plan(
            '<!-- eviforge:command "pytest -q && curl bad" -->\n'
            '<!-- eviforge:criterion {"id":"x","description":"x"} -->',
            task_id="task",
            objective="x",
            workspace_root=tmp_path,
        )


def test_required_criterion_needs_deterministic_verifier(tmp_path) -> None:
    with pytest.raises(ValueError, match="required criterion"):
        compile_plan(
            '<!-- eviforge:criterion {"id":"manual","description":"looks good"} -->',
            task_id="task",
            objective="x",
            workspace_root=tmp_path,
        )


def test_optional_manual_criterion_is_allowed_but_cannot_grant_pass(tmp_path) -> None:
    manifest = compile_plan(
        '<!-- eviforge:criterion {"id":"note","description":"review later","required":false} -->\n'
        '<!-- eviforge:criterion {"id":"tests","description":"tests pass",'
        '"argv":["python","-m","pytest","-q"]} -->',
        task_id="task",
        objective="x",
        workspace_root=tmp_path,
    )
    assert manifest.contract.criteria[0].required is False
    assert manifest.contract.criteria[1].verifier_ids == ("verify-tests",)
