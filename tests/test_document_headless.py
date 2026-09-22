"""Offline regression tests for the real CLI and background-agent lifecycle."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from likecc import __main__ as entrypoint
from likecc.client import LLMClient
from likecc.config import load_config
from likecc.hooks import Action, Hook, HookEngine
from likecc.permissions import PermissionMode
from likecc.tools.base import StreamEnd, TextDelta, ToolCallComplete


@pytest.fixture
def isolated_project(tmp_path, monkeypatch):
    home = tmp_path / "home"
    project = tmp_path / "project"
    home.mkdir()
    (project / ".likecc").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.chdir(project)
    (project / ".likecc" / "config.yaml").write_text(
        "providers:\n"
        "  - name: offline\n"
        "    protocol: openai-compat\n"
        "    base_url: http://unused.invalid\n"
        "    model: offline\n"
        "    api_key: offline-test-only\n"
        "    context_window: 100000\n"
        "permission_mode: bypassPermissions\n"
        "enable_fork: true\n",
        encoding="utf-8",
    )

    async def no_model_fetch(provider):
        return None

    monkeypatch.setattr("likecc.client.resolve_context_window", no_model_fetch)
    return load_config()


@pytest.fixture
def task_managers(monkeypatch):
    from likecc.agents.task_manager import TaskManager

    instances = []

    class TrackedTaskManager(TaskManager):
        def __init__(self):
            super().__init__()
            instances.append(self)

    monkeypatch.setattr("likecc.agents.task_manager.TaskManager", TrackedTaskManager)
    return instances


class BackgroundClient(LLMClient):
    def __init__(self, *, fork=False, blocked=False, parent_error=False, wait_for_child=True):
        self.fork = fork
        self.blocked = blocked
        self.parent_error = parent_error
        self.wait_for_child = wait_for_child
        self.parent_calls = 0
        self.child_started = asyncio.Event()
        self.parent_replied = asyncio.Event()
        self.child_finalized = asyncio.Event()
        self.notifications = []

    async def stream(self, conversation, system="", tools=None):
        is_child = any(
            message.role == "user" and "Do the background child task" in message.content
            for message in conversation.history
        )
        if is_child:
            self.child_started.set()
            try:
                await self.parent_replied.wait()
                if self.blocked:
                    await asyncio.Event().wait()
                yield TextDelta("child-result-marker")
                yield StreamEnd("end_turn", input_tokens=3, output_tokens=2)
            finally:
                self.child_finalized.set()
            return

        self.parent_calls += 1
        if self.parent_calls == 1:
            arguments = {
                "prompt": "Do the background child task",
                "description": "offline child",
                "run_in_background": True,
            }
            if not self.fork:
                arguments["subagent_type"] = "general-purpose"
            yield ToolCallComplete("child-call", "Agent", arguments)
        elif self.parent_calls == 2:
            self.parent_replied.set()
            if self.wait_for_child:
                await self.child_started.wait()
            if self.parent_error:
                raise RuntimeError("parent failed after launching child")
            yield TextDelta("initial-parent-result")
        else:
            content = "\n".join(message.content for message in conversation.history)
            assert "<task-notification>" in content
            assert "child-result-marker" in content
            self.notifications.append(content)
            yield TextDelta("parent-collected-child-result")
        yield StreamEnd("end_turn", input_tokens=5, output_tokens=2)


@pytest.mark.asyncio
@pytest.mark.parametrize("fork", [False, True])
async def test_headless_delivers_background_result_without_team(
    isolated_project, task_managers, monkeypatch, capsys, fork
):
    client = BackgroundClient(fork=fork)
    monkeypatch.setattr("likecc.client.create_client", lambda provider: client)
    await asyncio.wait_for(
        entrypoint._run_prompt_with_hook_cleanup(
            isolated_project, PermissionMode.BYPASS, None, "Delegate the task"
        ),
        timeout=2,
    )
    assert client.child_finalized.is_set()
    assert len(client.notifications) == 1
    assert "parent-collected-child-result" in capsys.readouterr().out
    assert task_managers[0]._async_tasks == {}
    assert [task.status for task in task_managers[0].list_tasks()] == ["completed"]


@pytest.mark.asyncio
async def test_headless_background_deadline_cancels_and_reaps_workers(
    isolated_project, task_managers, monkeypatch
):
    client = BackgroundClient(blocked=True)
    monkeypatch.setattr("likecc.client.create_client", lambda provider: client)
    monkeypatch.setattr(entrypoint, "HEADLESS_BACKGROUND_TIMEOUT", 0.02)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(
            entrypoint._run_prompt_with_hook_cleanup(
                isolated_project, PermissionMode.BYPASS, None, "Delegate the task"
            ),
            timeout=2,
        )
    assert client.child_finalized.is_set()
    assert task_managers[0]._async_tasks == {}
    assert [task.status for task in task_managers[0].list_tasks()] == ["cancelled"]


@pytest.mark.asyncio
@pytest.mark.parametrize("wait_for_child", [False, True])
async def test_headless_parent_failure_preserves_error_and_cleans_workers_and_hooks(
    isolated_project, task_managers, monkeypatch, wait_for_child
):
    client = BackgroundClient(blocked=True, parent_error=True, wait_for_child=wait_for_child)
    monkeypatch.setattr("likecc.client.create_client", lambda provider: client)
    hooks = HookEngine([
        Hook(id="stopped", event="shutdown", action=Action(type="prompt", message="stopped"))
    ])
    with pytest.raises(RuntimeError, match="parent failed after launching child"):
        await asyncio.wait_for(
            entrypoint._run_prompt_with_hook_cleanup(
                isolated_project, PermissionMode.BYPASS, hooks, "Delegate the task"
            ),
            timeout=2,
        )
    if client.child_started.is_set():
        assert client.child_finalized.is_set()
    assert task_managers[0]._async_tasks == {}
    assert [task.status for task in task_managers[0].list_tasks()] == ["cancelled"]
    assert [note.event for note in hooks.drain_notifications()] == ["shutdown"]
    assert hooks._background_tasks == set()


@pytest.mark.asyncio
async def test_headless_external_cancellation_reaps_workers(
    isolated_project, task_managers, monkeypatch
):
    client = BackgroundClient(blocked=True)
    monkeypatch.setattr("likecc.client.create_client", lambda provider: client)
    run = asyncio.create_task(entrypoint._run_prompt_with_hook_cleanup(
        isolated_project, PermissionMode.BYPASS, None, "Delegate the task"
    ))
    try:
        await asyncio.wait_for(client.child_started.wait(), timeout=2)
        run.cancel()
        with pytest.raises(asyncio.CancelledError):
            await run
    finally:
        if not run.done():
            run.cancel()
            await asyncio.gather(run, return_exceptions=True)
    assert client.child_finalized.is_set()
    assert task_managers[0]._async_tasks == {}


def test_real_cli_p_uses_isolated_config_and_fake_provider(
    isolated_project, task_managers, monkeypatch, capsys
):
    class TextClient(LLMClient):
        async def stream(self, conversation, system="", tools=None):
            assert any(message.content == "offline prompt" for message in conversation.history)
            assert {"ReadFile", "WriteFile", "EditFile", "Bash", "Glob", "Grep"} <= {
                tool["name"] for tool in tools
            }
            yield TextDelta("offline-cli-result")
            yield StreamEnd("end_turn")

    monkeypatch.setattr("likecc.client.create_client", lambda provider: TextClient())
    monkeypatch.setattr(sys, "argv", ["likecc", "-p", "offline prompt"])
    entrypoint.main()
    assert capsys.readouterr().out.strip() == "offline-cli-result"
    assert task_managers[0]._async_tasks == {}
