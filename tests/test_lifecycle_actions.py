"""Six-event acceptance: real Agent writes, real Git repositories and failure paths."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import json
from pathlib import Path
import subprocess

import pytest

from eviforge.agent import Agent
from eviforge.client import NetworkError, AmbiguousStreamError
from eviforge.config import AppConfig
from eviforge.hooks import HookContext, create_hook_engine
from eviforge.hooks.defaults import default_hooks
from eviforge.tools import create_default_registry
from eviforge.tools.base import TextDelta, ToolCallComplete
from eviforge.validator import ConfigError, validate_hook_policy
from test_eviforge_runtime import Scripted, turn
from test_tui_presentation import ui_environment, wait_until


def git(root, *args):
    return subprocess.run(["git", "--literal-pathspecs", "-C", str(root), *args], check=True,
                          capture_output=True).stdout.decode().strip()


@pytest.fixture
def repository(tmp_path):
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.name", "Hook Test")
    git(tmp_path, "config", "user.email", "hooks@example.invalid")
    git(tmp_path, "config", "commit.gpgsign", "false")
    (tmp_path / ".gitignore").write_text(".eviforge/\n")
    (tmp_path / "app.py").write_text("value = 0\n")
    git(tmp_path, "add", ".gitignore", "app.py")
    git(tmp_path, "commit", "-qm", "baseline")
    return tmp_path


def make_agent(root, turns, **policy):
    hooks = create_hook_engine(AppConfig(providers=[], hook_policy=policy))
    return Agent(Scripted(turns), create_default_registry(), "openai", work_dir=str(root), hook_engine=hooks)


def records(root, name):
    path = root / ".eviforge/hooks" / (name + ".jsonl")
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def write(name="app.py", content="value = 1\n"):
    read = [ToolCallComplete("read", "ReadFile", {"file_path": name})] if name == "app.py" else []
    return turn(*read, ToolCallComplete("write", "WriteFile", {"file_path": name, "content": content}))


def test_six_events_preserve_evidence_hooks():
    mapped = {(h.event, h.action.builtin or h.id) for h in default_hooks()}
    assert {("session_start", "session_safety"), ("turn_start", "turn_safety"),
            ("turn_start", "notify_turn_start"), ("pre_tool_use", "protect_sensitive_files"),
            ("post_tool_use", "post_tool_safety"), ("post_tool_use", "commit_after_tool"),
            ("turn_end", "log_turn"), ("turn_end", "notify_turn_end"),
            ("session_end", "commit_session"), ("session_end", "notify_session_end"),
            ("turn_end", "check_changed_code"), ("session_end", "final_evidence_report"),
            ("pre_send", "evidence_contract")} == mapped


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["app.py", "new.py", "space ; dollar $.py", ":(glob)*.py"])
async def test_real_agent_checkpoints_only_its_verified_files(repository, name):
    if ":" in name and __import__("os").name == "nt":
        pytest.skip("Colon is not a Windows filename")
    before = git(repository, "rev-parse", "HEAD")
    agent = make_agent(repository, [write(name), turn(TextDelta("done"))])
    events = []
    assert await agent.run_to_completion("edit", event_callback=events.append) == "done"
    assert agent.last_run_status == "success"
    assert git(repository, "rev-parse", "HEAD") != before
    assert git(repository, "rev-list", "--count", "HEAD") == "2"
    assert git(repository, "show", "--format=", "--name-only", "HEAD") == name
    assert git(repository, "status", "--porcelain") == ""
    hooks = [(e["event"], e["hook_id"]) for e in events if e["type"] == "hook"]
    assert ("post_tool_use", "post_tool_safety") in hooks
    assert hooks.index(("post_tool_use", "post_tool_safety")) < hooks.index(("post_tool_use", "commit_after_tool"))
    assert sum(event == "session_end" and name == "notify_session_end" for event, name in hooks) == 1
    notes = records(repository, "notifications")
    assert [n["event"] for n in notes] == ["turn_start", "turn_end", "turn_start", "turn_end", "session_end"]
    assert notes[-1]["run_status"] == "success"
    assert [r["iteration"] for r in records(repository, "lifecycle") if r.get("action") == "turn_log"] == [1, 2]
    assert len([r for r in records(repository, "commits") if r["status"] == "committed"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["dirty", "staged", "foreign", "disabled", "bad_syntax", "failed_check"])
async def test_auto_commit_does_not_capture_unsafe_changes(repository, kind):
    if kind in {"dirty", "staged"}:
        (repository / "mine.txt").write_text("human edit")
        if kind == "staged":
            git(repository, "add", "mine.txt")
    before = git(repository, "rev-parse", "HEAD")
    policy = {"auto_commit": False} if kind == "disabled" else {}
    if kind == "failed_check":
        policy["checks"] = [{"name": "fails", "argv": ["{python}", "-c", "raise SystemExit(1)"], "timeout": 5}]
    agent = make_agent(repository, [write(content="value = (\n" if kind == "bad_syntax" else "value = 1\n"),
                                   turn(TextDelta("done")), turn(TextDelta("done")), turn(TextDelta("done"))], **policy)
    if kind == "foreign":
        original = agent.client.stream
        async def stream(*args, **kwargs):
            if not agent.client.requests:
                (repository / "foreign.txt").write_text("concurrent human edit")
            async for event in original(*args, **kwargs):
                yield event
        agent.client.stream = stream
    await agent.run_to_completion("edit")
    assert git(repository, "rev-parse", "HEAD") == before
    if kind == "staged":
        assert git(repository, "diff", "--cached", "--name-only") == "mine.txt"
    else:
        assert git(repository, "diff", "--cached", "--name-only") == ""
    if kind in {"bad_syntax", "failed_check"}:
        assert agent.last_run_status == "failed"
        assert records(repository, "notifications")[-1]["run_status"] == "failed"
    assert len([r for r in records(repository, "notifications") if r["event"] == "session_end"]) == 1


@pytest.mark.asyncio
async def test_external_rewrite_of_owned_file_prevents_session_commit(repository):
    agent = make_agent(repository, [write(), turn(TextDelta("done"))], auto_commit=False)
    original = agent.client.stream
    async def stream(*args, **kwargs):
        if agent.client.requests:
            (repository / "app.py").write_text("value = 99\n")
            agent.hook_engine.default_runner.auto_commit = True
        async for event in original(*args, **kwargs):
            yield event
    agent.client.stream = stream
    before = git(repository, "rev-parse", "HEAD")
    await agent.run_to_completion("edit")
    assert git(repository, "rev-parse", "HEAD") == before
    assert "externally changed" in records(repository, "commits")[-1]["reason"]


@pytest.mark.asyncio
async def test_session_commit_retries_pending_verified_changes(repository):
    agent = make_agent(repository, [write(), turn(TextDelta("done"))], auto_commit=False)
    original = agent.client.stream
    async def stream(*args, **kwargs):
        if agent.client.requests:
            agent.hook_engine.default_runner.auto_commit = True
        async for event in original(*args, **kwargs):
            yield event
    agent.client.stream = stream
    await agent.run_to_completion("edit")
    committed = [r for r in records(repository, "commits") if r["status"] == "committed"]
    assert len(committed) == 1 and committed[0]["event"] == "session_end"


@pytest.mark.asyncio
async def test_rejecting_git_hook_leaves_worktree_and_index_intact(repository):
    directory = repository / ".git" / "test-hooks"
    directory.mkdir()
    hook = directory / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8", newline="\n")
    hook.chmod(0o755)
    git(repository, "config", "core.hooksPath", str(directory))
    before = git(repository, "rev-parse", "HEAD")
    agent = make_agent(repository, [write(), turn(TextDelta("done"))])
    await agent.run_to_completion("edit")
    assert git(repository, "rev-parse", "HEAD") == before
    assert git(repository, "diff", "--cached", "--name-only") == ""
    assert (repository / "app.py").read_text() == "value = 1\n"
    assert agent.last_run_status == "success"  # ancillary commit failure is reported, not a false code failure
    assert any(r["status"] == "failed" for r in records(repository, "commits"))


@pytest.mark.asyncio
async def test_provider_failure_closes_turn_and_session_without_success_notice(tmp_path):
    agent = make_agent(tmp_path, [[TextDelta("partial"), NetworkError("offline")]])
    delivered = []
    with pytest.raises(AmbiguousStreamError):
        await agent.run_to_completion("hello", event_callback=delivered.append)
    assert delivered[-1]["hook_id"] == "notify_session_end"
    assert agent.hook_engine.drain_notifications() == []
    notes = records(tmp_path, "notifications")
    assert [n["event"] for n in notes] == ["turn_start", "turn_end", "session_end"]
    assert notes[-1]["run_status"] == "ambiguous"


@pytest.mark.asyncio
async def test_cancellation_balances_lifecycle_and_never_commits(repository):
    ready = asyncio.Event()
    agent = make_agent(repository, [])
    async def stream(*args, **kwargs):
        ready.set()
        await asyncio.Event().wait()
        yield TextDelta("never")
    agent.client.stream = stream
    before = git(repository, "rev-parse", "HEAD")
    task = asyncio.create_task(agent.run_to_completion("wait"))
    await asyncio.wait_for(ready.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    notes = records(repository, "notifications")
    assert [n["event"] for n in notes] == ["turn_start", "turn_end", "session_end"]
    assert notes[-1]["run_status"] == "cancelled"
    assert git(repository, "rev-parse", "HEAD") == before


@pytest.mark.asyncio
async def test_logs_do_not_persist_prompt_or_tool_contents(tmp_path):
    secret_marker = "PRIVATE_MARKER_DO_NOT_LOG_12345"
    agent = make_agent(tmp_path, [write("app.py", f'value = "{secret_marker}"\n'), turn(TextDelta(secret_marker))])
    await agent.run_to_completion(secret_marker)
    for path in (tmp_path / ".eviforge/hooks").rglob("*"):
        if path.is_file():
            assert secret_marker not in path.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_failed_write_never_creates_checkpoint(repository):
    agent = make_agent(repository, [write("missing/../.env", "secret"), turn(TextDelta("done"))])
    before = git(repository, "rev-parse", "HEAD")
    await agent.run_to_completion("edit")
    assert agent.last_run_status == "blocked"
    assert git(repository, "rev-parse", "HEAD") == before


@pytest.mark.asyncio
async def test_child_does_not_commit_or_emit_main_lifecycle_actions(repository):
    agent = make_agent(repository, [write(), turn(TextDelta("done"))])
    agent.parent_id = "parent"
    await agent.run_to_completion("edit")
    assert git(repository, "rev-list", "--count", "HEAD") == "1"
    assert not (repository / ".eviforge/hooks/notifications.jsonl").exists()


def test_auto_commit_config_is_strict():
    assert validate_hook_policy({"auto_commit": False})["auto_commit"] is False
    with pytest.raises(ConfigError):
        validate_hook_policy({"auto_commit": "yes"})


@pytest.mark.asyncio
async def test_tui_displays_real_lifecycle_notifications(ui_environment):
    from eviforge.app import HookNotice
    app, client = ui_environment
    app.hook_engine = create_hook_engine(AppConfig(providers=[]))
    async with app.run_test(size=(120, 40)) as pilot:
        client.release.set()
        app.send_user_message("Introduce yourself")
        await wait_until(pilot, lambda: not app._streaming)
        await pilot.pause()
        notices = list(app.query(HookNotice))
        assert len(notices) >= 8
        assert records(Path.cwd(), "notifications")[-1]["event"] == "session_end"
        assert records(Path.cwd(), "notifications")[-1]["run_status"] == "success"


@pytest.mark.asyncio
async def test_cancel_during_git_commit_restores_only_own_staging(repository, monkeypatch):
    import eviforge.hooks.defaults as defaults
    ready = asyncio.Event()
    original = defaults._run_argv
    async def run(argv, *args, **kwargs):
        if "commit" in argv:
            ready.set()
            await asyncio.Event().wait()
        return await original(argv, *args, **kwargs)
    monkeypatch.setattr(defaults, "_run_argv", run)
    agent = make_agent(repository, [write()])
    task = asyncio.create_task(agent.run_to_completion("edit"))
    await asyncio.wait_for(ready.wait(), 10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert git(repository, "diff", "--cached", "--name-only") == ""
    assert git(repository, "rev-list", "--count", "HEAD") == "1"
    assert (repository / "app.py").read_text() == "value = 1\n"


@pytest.mark.asyncio
async def test_symlink_artifact_directory_fails_before_model_request(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "workspace"
    root.mkdir()
    agent = make_agent(root, [])
    try:
        (root / ".eviforge" / "hooks").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Symlink creation not permitted")
    with pytest.raises(RuntimeError, match="artifacts"):
        await agent.run_to_completion("hello")
    assert not agent.client.requests
    assert list(outside.iterdir()) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_tui_delivers_end_notification_on_failure_or_cancel(ui_environment, cancel):
    from eviforge.app import HookNotice
    app, client = ui_environment
    app.hook_engine = create_hook_engine(AppConfig(providers=[]))
    async with app.run_test(size=(120, 40)) as pilot:
        app.send_user_message("hello")
        await wait_until(pilot, lambda: app._connection_state == "模型已连接")
        if cancel:
            await pilot.press("escape")
        else:
            client.fail = True
            client.release.set()
        await wait_until(pilot, lambda: not app._streaming)
        notices = [n for n in app.query(HookNotice) if n._title == "会话结束通知"]
        assert len(notices) == 1 and notices[0]._expanded
        assert ("cancelled" if cancel else "ambiguous") in str(notices[0].render())
        assert app.hook_engine.drain_notifications() == []


@pytest.mark.asyncio
async def test_shared_engine_keeps_child_and_shutdown_notifications_separate(tmp_path):
    from eviforge.hooks import HookEngine, Hook, Action
    hooks = HookEngine([Hook("note", "turn_end", Action("prompt", message="note")),
                        Hook("close", "shutdown", Action("prompt", message="close"))])
    for identity in ("parent", "child"):
        await hooks.run_hooks("turn_end", HookContext(agent_id=identity))
    await hooks.run_hooks("shutdown", HookContext())
    assert [n.agent_id for n in hooks.drain_notifications(agent_id="child")] == ["child"]
    assert [n.agent_id for n in hooks.drain_notifications(agent_id="parent")] == ["parent"]
    assert [n.event for n in hooks.drain_notifications()] == ["shutdown"]


@pytest.mark.asyncio
async def test_cancel_during_turn_validation_still_writes_end_log(tmp_path, monkeypatch):
    ready = asyncio.Event()
    agent = make_agent(tmp_path, [turn(TextDelta("done"))])
    original = agent.hook_engine.default_runner._check
    calls = 0
    async def check(ctx):
        nonlocal calls
        calls += 1
        if calls == 1:
            ready.set()
            await asyncio.Event().wait()
        return await original(ctx)
    monkeypatch.setattr(agent.hook_engine.default_runner, "_check", check)
    task = asyncio.create_task(agent.run_to_completion("hello"))
    await asyncio.wait_for(ready.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert [r["event"] for r in records(tmp_path, "notifications")] == ["turn_start", "turn_end", "session_end"]
    assert records(tmp_path, "notifications")[-1]["run_status"] == "cancelled"
