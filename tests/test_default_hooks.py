"""Default hooks through real files, processes, Agent loops and the public CLI."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

from eviforge.agent import Agent
from eviforge.config import AppConfig, _merge_config
from eviforge.hooks import Action, Hook, HookContext, HookConfigError, create_hook_engine, load_hooks
from eviforge.hooks.defaults import DefaultHookRunner, _run_argv
from eviforge.tools import create_default_registry
from eviforge.tools.base import TextDelta, ToolCallComplete
from eviforge.validator import ConfigError, validate_hook_policy
from test_eviforge_runtime import Scripted, turn
from test_document_headless import isolated_project


def context(tmp_path, **kwargs):
    return HookContext(work_dir=str(tmp_path), session_id="session", turn_id="turn", agent_id="main", **kwargs)


def engine(**policy):
    return create_hook_engine(AppConfig(providers=[], hook_policy=policy))


@pytest.mark.asyncio
@pytest.mark.parametrize("name", [".env", ".env.local", "id_rsa", "id_ed25519", "credentials.json",
                                   ".git/config", ".eviforge/config.yaml", ".eviforge/hooks/guard.py"])
async def test_protected_writes_rejected_and_templates_allowed(tmp_path, name):
    hooks = engine()
    ctx = context(tmp_path, event_name="pre_tool_use", tool_name="WriteFile", file_path=name)
    assert await hooks.run_pre_tool_hooks(ctx) is not None
    for allowed in (".env.example", ".env.template", "src/app.py"):
        assert await hooks.run_pre_tool_hooks(replace(ctx, file_path=allowed)) is None
    assert await hooks.run_pre_tool_hooks(replace(ctx, tool_name="ReadFile")) is None


@pytest.mark.asyncio
async def test_symlink_protected_target_and_protection_failure_fail_closed(tmp_path, monkeypatch):
    secret = tmp_path / ".env"
    secret.write_text("placeholder")
    alias = tmp_path / "alias.txt"
    try:
        alias.symlink_to(secret)
    except OSError:
        pytest.skip("Symlink creation not permitted")
    hooks = engine()
    ctx = context(tmp_path, tool_name="EditFile", file_path=str(alias))
    assert await hooks.run_pre_tool_hooks(ctx) is not None


@pytest.mark.asyncio
async def test_protection_failure_fails_closed_without_symlink_permission(tmp_path, monkeypatch):
    hooks = engine()
    ctx = context(tmp_path, tool_name="EditFile", file_path="ordinary.txt")
    async def broken(*args):
        raise OSError("protection unavailable")
    monkeypatch.setattr(hooks.default_runner, "execute", broken)
    assert await hooks.run_pre_tool_hooks(replace(ctx, file_path="ordinary.txt")) is not None


@pytest.mark.asyncio
async def test_baseline_hash_cache_and_stale_validation(tmp_path):
    source = tmp_path / "sample.py"
    source.write_text("old preexisting syntax error (", encoding="utf-8")
    runner = DefaultHookRunner()
    ctx = context(tmp_path)
    await runner.begin_run(ctx)
    unchanged = await runner._check(ctx)
    assert unchanged["status"] == "no_changes"
    source.write_text("value = 1\n", encoding="utf-8")
    good = await runner._check(ctx)
    assert good["status"] == "partial"
    assert good["changed_files"]["sample.py"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert "No project test commands configured" in good["unverified"]
    assert await runner._check(ctx) is good
    source.write_text("value = (\n", encoding="utf-8")
    bad = await runner._check(ctx)
    assert bad["status"] == "failed" and bad["fingerprint"] != good["fingerprint"]
    source.unlink()
    deleted = await runner._check(ctx)
    assert deleted["changed_files"]["sample.py"] is None


@pytest.mark.asyncio
async def test_explicit_checks_no_shell_injection_and_no_repeat(tmp_path):
    marker = tmp_path / "count.txt"
    command = ["{python}", "-c", "from pathlib import Path; p=Path('count.txt'); p.write_text('called')"]
    # Check artifacts are kept in the ignored runtime directory so checks remain read-only for source inputs.
    command[-1] = "from pathlib import Path; p=Path('.eviforge'); p.mkdir(exist_ok=True); (p/'count.txt').write_text('called')"
    runner = DefaultHookRunner([{"name": "project_test", "argv": command, "timeout": 5}])
    ctx = context(tmp_path)
    await runner.begin_run(ctx)
    source = tmp_path / "name ; echo unsafe.py"
    source.write_text("x = 1\n")
    result = await runner._check(ctx)
    assert result["status"] == "partial"  # no Git whitespace check in this fixture
    assert next(c for c in result["checks"] if c["name"] == "project_test")["exit_code"] == 0
    (tmp_path / ".eviforge/count.txt").unlink()
    assert await runner._check(ctx) is result
    assert not (tmp_path / ".eviforge/count.txt").exists() and not marker.exists()


@pytest.mark.asyncio
async def test_command_cancellation_reaps_child_and_timeout_fails(tmp_path):
    argv = [sys.executable, "-c", "import time; time.sleep(30)"]
    result = await _run_argv(argv, tmp_path, 0.05)
    assert result["status"] == "failed" and "timed out" in result["output"]
    task = asyncio.create_task(_run_argv(argv, tmp_path, 30))
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)


@pytest.mark.asyncio
async def test_main_only_report_with_safe_identifier_and_changed_hash(tmp_path):
    hooks = engine()
    ctx = replace(context(tmp_path), session_id="../../outside")
    await hooks.begin_run(ctx)
    (tmp_path / "app.py").write_text("x = 1\n")
    child = replace(ctx, parent_id="main", agent_id="child")
    await hooks.run_hooks("session_end", child)
    assert not (tmp_path / ".eviforge/hooks/reports").exists()
    await hooks.run_hooks("session_end", ctx)
    summary = hooks.verification_summary(ctx)
    report = Path(summary["report_path"])
    assert report.resolve().is_relative_to(tmp_path)
    evidence = json.loads(report.read_text(encoding="utf-8"))
    assert evidence["session_id"] == "../../outside"
    assert evidence["status"] == "partial" and evidence["changed_files"]["app.py"]


@pytest.mark.asyncio
async def test_real_git_checks_and_mutating_validator_invalidate_evidence(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "sample.py").write_text("x = 1\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "sample.py"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                    "commit", "-qm", "baseline"], check=True)
    runner = DefaultHookRunner([{"name": "test", "argv": ["{python}", "-c", "print('verified')"], "timeout": 5}])
    ctx = context(tmp_path)
    await runner.begin_run(ctx)
    (tmp_path / "sample.py").write_text("x = 2\n")
    report = await runner._check(ctx)
    assert report["status"] == "passed" and not report["unverified"]
    (tmp_path / "sample.py").write_text("x = 2  \n")
    assert (await runner._check(ctx))["status"] == "failed"
    runner.checks = [{"name": "mutates", "argv": ["{python}", "-c",
                     "from pathlib import Path; Path('sample.py').write_text('x = 3\\n')"], "timeout": 5}]
    (tmp_path / "sample.py").write_text("x = 4\n")
    report = await runner._check(ctx)
    assert report["status"] == "failed"
    assert any(check["name"] == "evidence_freshness" for check in report["checks"])


@pytest.mark.asyncio
async def test_workdir_switch_baseline_and_large_file_are_not_false_passes(tmp_path):
    original, switched = tmp_path / "original", tmp_path / "switched"
    original.mkdir()
    switched.mkdir()
    hooks = engine()
    await hooks.begin_run(context(original))
    ctx = context(switched, tool_name="WriteFile", file_path="new.py")
    assert await hooks.run_pre_tool_hooks(ctx) is None
    (switched / "new.py").write_text("x = 1\n")
    await hooks.run_hooks("turn_end", ctx)
    assert hooks.verification_summary(ctx)["status"] == "partial"
    (switched / "large.txt").write_bytes(b"x" * 2_000_001)
    await hooks.run_hooks("turn_end", ctx)
    assert any("Snapshot limits" in item for item in hooks.verification_summary(ctx)["unverified"])


@pytest.mark.asyncio
async def test_failed_code_feeds_back_then_repairs_before_success(tmp_path):
    client = Scripted([
        turn(ToolCallComplete("bad", "WriteFile", {"file_path": "app.py", "content": "x = (\n"})),
        turn(TextDelta("done too early")),
        turn(ToolCallComplete("fix", "WriteFile", {"file_path": "app.py", "content": "x = 1\n"})),
        turn(TextDelta("verified fixed")),
    ])
    hooks = engine()
    agent = Agent(client, create_default_registry(), "openai", work_dir=str(tmp_path), hook_engine=hooks)
    output = await agent.run_to_completion("change app.py")
    assert output == "verified fixed" and agent.last_run_status == "success"
    assert any("Validation failed" in message.content for message in client.requests[2][0])
    assert all("never invent evidence" in request[1] for request in client.requests)
    report = json.loads(Path(hooks.verification_summary(agent._build_hook_context("session_end"))["report_path"]).read_text())
    assert report["status"] == "partial" and report["run_status"] == "success"


@pytest.mark.asyncio
async def test_repeated_false_completion_is_bounded_and_failed(tmp_path):
    client = Scripted([
        turn(ToolCallComplete("bad", "WriteFile", {"file_path": "bad.py", "content": "x = (\n"})),
        turn(TextDelta("done")), turn(TextDelta("done")), turn(TextDelta("done")),
    ])
    agent = Agent(client, create_default_registry(), "openai", work_dir=str(tmp_path), hook_engine=engine())
    await agent.run_to_completion("change bad.py")
    assert agent.last_run_status == "failed" and len(client.requests) == 4


@pytest.mark.asyncio
async def test_default_guard_blocks_real_write_even_when_model_claims_success(tmp_path):
    client = Scripted([turn(ToolCallComplete("secret", "WriteFile", {"file_path": ".env", "content": "no"})),
                       turn(TextDelta("completed"))])
    agent = Agent(client, create_default_registry(), "openai", work_dir=str(tmp_path), hook_engine=engine())
    await agent.run_to_completion("edit config")
    assert agent.last_run_status == "blocked" and not (tmp_path / ".env").exists()


@pytest.mark.asyncio
async def test_default_hooks_preserve_read_parallelism_and_custom_hooks_remain_serial(tmp_path):
    for name in ("a.txt", "b.txt"):
        (tmp_path / name).write_text(name)
    agent = Agent(Scripted([]), create_default_registry(), "openai", work_dir=str(tmp_path), hook_engine=engine())
    calls = [ToolCallComplete(name, "ReadFile", {"file_path": name}) for name in ("a.txt", "b.txt")]
    assert agent._batch_can_run_parallel(calls)
    results = await agent._execute_batch_parallel(calls)
    assert all(not result.result.is_error for result in results)
    agent.hook_engine.hooks.append(Hook("custom", "pre_tool_use", Action("prompt", message="ordered")))
    assert not agent._batch_can_run_parallel(calls)


def test_config_disable_layer_override_and_builtin_validation(tmp_path):
    base = AppConfig(providers=[], hook_policy={"enabled": False})
    assert create_hook_engine(_merge_config(base, AppConfig(providers=[]))) is None
    assert len(engine().hooks) == 13
    with pytest.raises(ConfigError):
        validate_hook_policy({"checks": [{"name": "bad", "argv": "pytest"}]})
    with pytest.raises(HookConfigError, match="builtin"):
        load_hooks([{"event": "startup", "action": {"type": "builtin", "builtin": "check_changed_code"}}])
    with pytest.raises(HookConfigError, match="builtin"):
        load_hooks([{"event": "startup", "action": {"type": "builtin", "builtin": {"invalid": True}}}])


def test_public_cli_default_hooks_report_and_disable(isolated_project, monkeypatch, capsys):
    from eviforge import __main__ as cli
    client = Scripted([
        turn(ToolCallComplete("write", "WriteFile", {"file_path": "app.py", "content": "x = 1\n"})),
        turn(TextDelta("done")),
    ])
    monkeypatch.setattr("eviforge.client.create_client", lambda _: client)
    monkeypatch.setattr(sys, "argv", ["eviforge", "-p", "edit", "--output", "json"])
    cli.main()
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "success"
    verification = result["metadata"]["hook_verification"]
    assert verification["status"] == "partial" and Path(verification["report_path"]).exists()
    config_path = Path(".eviforge/config.yaml")
    config_path.write_text(config_path.read_text() + "hook_policy:\n  enabled: false\n")
    client.turns = iter([turn(TextDelta("disabled"))])
    cli.main()
    result = json.loads(capsys.readouterr().out)
    assert "hook_verification" not in result["metadata"]
