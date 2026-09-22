from __future__ import annotations

import argparse
import json

import pytest
from textual.app import App, ComposeResult

from eviforge.plan_dialog import InlinePlanWidget, PlanChoice
from eviforge.planning import PlanService, PlanState
from eviforge.planning.cli import handle, register_parser
from eviforge.tools.exit_plan_mode import ExitPlanModeParams, ExitPlanModeTool


def test_plan_cli_creates_reviews_and_rejects_without_model(tmp_path, capsys):
    planfile = tmp_path / "plan.md"
    planfile.write_text("A local reviewable plan", encoding="utf-8")
    parser = argparse.ArgumentParser()
    register_parser(parser.add_subparsers(dest="command"))
    args = parser.parse_args(["plan", "--work-dir", str(tmp_path), "create", "--session-id", "session", "--turn-id", "turn", "--content-file", str(planfile)])
    assert handle(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["state"] == "submitted"
    args = parser.parse_args(["plan", "--work-dir", str(tmp_path), "approve", result["plan_id"], "--hash", "wrong", "--session-id", "session", "--source-turn-id", "turn", "--execution-turn-id", "execute", "--agent-id", "a"])
    assert handle(args) == 2
    assert json.loads(capsys.readouterr().out)["error"] == "PLAN_CHANGED"
    args.content_hash = result["content_hash"]
    assert handle(args) == 0
    assert "No grant survives" in json.loads(capsys.readouterr().out)["execution_authority"]
    args = parser.parse_args(["plan", "--work-dir", str(tmp_path), "reject", result["plan_id"]])
    assert handle(args) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "rejected"


@pytest.mark.asyncio
async def test_exit_plan_tool_submits_but_cannot_approve(tmp_path):
    service = PlanService(tmp_path)
    plan = service.create("s", "t", "Test plan")
    tool = ExitPlanModeTool(submit_plan=lambda actions: service.submit(plan.plan_id, actions=actions))
    result = await tool.execute(ExitPlanModeParams(actions=[]))
    assert not result.is_error
    assert json.loads(result.output)["status"] == "approval_required"
    assert service.get(plan.plan_id).state == PlanState.SUBMITTED
    assert service._grants == []


@pytest.mark.asyncio
async def test_dialog_escape_rejects_frozen_identity():
    class Harness(App):
        response = None
        def compose(self) -> ComposeResult:
            yield InlinePlanWidget(snapshot={"plan_id": "id", "content_hash": "hash", "content": "Review [bold] literally", "actions": []})
        def on_inline_plan_widget_responded(self, event):
            self.response = event
    app = Harness()
    async with app.run_test() as pilot:
        await pilot.press("escape")
        assert app.response.choice == PlanChoice.REJECT
        assert (app.response.plan_id, app.response.content_hash) == ("id", "hash")
        assert app.response.request_id == app.query_one(InlinePlanWidget).request_id


@pytest.mark.asyncio
@pytest.mark.parametrize("choice", ["approve", "reject", "changed", "stale", "session"])
async def test_real_app_plan_submission_approval_and_revocation(tmp_path, monkeypatch, choice):
    import eviforge.app as ui
    from eviforge.client import LLMClient
    from eviforge.config import ProviderConfig
    from eviforge.permissions import PermissionMode
    from eviforge.tools.base import StreamEnd, TextDelta, ToolCallComplete

    home, project = tmp_path / "home", tmp_path / "project"
    home.mkdir()
    project.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.chdir(project)
    actions = [{"tool_name": "WriteFile", "arguments": {"file_path": "result.txt", "content": "approved result"}}]

    class PlanningClient(LLMClient):
        calls = 0
        async def stream(self, conversation, system="", tools=None):
            if not tools:
                yield TextDelta("Plan review")
                yield StreamEnd("end_turn")
                return
            self.calls += 1
            if self.calls == 1:
                yield ToolCallComplete("draft", "WriteFile", {"file_path": str(app.agent._get_plan_path()), "content": "Write result.txt with the approved result"})
            elif self.calls == 2:
                yield ToolCallComplete("submit", "ExitPlanMode", {"actions": actions})
            elif self.calls == 3:
                yield ToolCallComplete("execute", "WriteFile", actions[0]["arguments"])
            else:
                yield TextDelta("Finished the approved action")
            yield StreamEnd("end_turn")

    client = PlanningClient()
    monkeypatch.setattr(ui, "create_client", lambda _: client)
    async def resolve(_):
        return 32000
    monkeypatch.setattr(ui, "resolve_context_window", resolve)
    provider = ProviderConfig("offline", "anthropic", "http://unused.invalid", "offline", api_key="test-only", context_window=32000)
    app = ui.EviForgeApp([provider])
    async with app.run_test(size=(110, 45)) as pilot:
        await pilot.pause()
        app.set_plan_mode(True)
        app.send_user_message("Plan the file update")
        for _ in range(150):
            await pilot.pause(0.01)
            if getattr(app, "_pending_plan_request", None) and not app._streaming:
                break
        assert app._pending_plan_request is not None
        submitted = app.runtime.plan_service.current_plan(app.agent)
        assert submitted.state == PlanState.SUBMITTED
        assert not (project / "result.txt").exists()
        widget = app.query_one(InlinePlanWidget)
        if choice == "changed":
            app.agent._get_plan_path().write_text("edited while user reviewed", encoding="utf-8")
        if choice == "stale":
            app.on_inline_plan_widget_responded(InlinePlanWidget.Responded(PlanChoice.APPROVE,
                plan_id=submitted.plan_id, content_hash=submitted.content_hash, request_id="not-the-displayed-request"))
            assert app.runtime.plan_service.get(submitted.plan_id).state == PlanState.SUBMITTED
            assert app._pending_plan_request is not None
        if choice == "session":
            app._set_session(app.session_manager.create())
            assert app.runtime.plan_service.get(submitted.plan_id).state == PlanState.INVALIDATED
            assert app._pending_plan_request is None
        elif choice in {"reject", "stale"}:
            await pilot.press("escape")
        else:
            widget.focus()
            await pilot.press("enter")
        for _ in range(150):
            await pilot.pause(0.01)
            if not app._streaming and (choice != "approve" or (project / "result.txt").exists()):
                break
        result = app.runtime.plan_service.get(submitted.plan_id)
        if choice == "approve":
            assert (project / "result.txt").read_text() == "approved result"
            assert app.agent.permission_mode == PermissionMode.DEFAULT
            assert result.execution_turn_id != submitted.source_turn_id
            assert result.state == PlanState.COMPLETED
        else:
            assert not (project / "result.txt").exists()
            assert client.calls == 2
            assert result.state == (PlanState.INVALIDATED if choice in {"changed", "session"} else PlanState.REJECTED)


@pytest.mark.asyncio
async def test_real_app_agent_startup_waits_for_provider_and_runs_once(tmp_path, monkeypatch):
    import asyncio
    import eviforge.app as ui
    from eviforge.client import LLMClient
    from eviforge.config import ProviderConfig
    from eviforge.hooks import Action, Hook, HookEngine
    from eviforge.tools.base import StreamEnd, TextDelta, ToolCallComplete

    home, project = tmp_path / "home", tmp_path / "project"
    home.mkdir()
    project.mkdir()
    (project / "source.txt").write_text("startup evidence", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.chdir(project)

    class HookClient(LLMClient):
        calls = 0
        async def stream(self, conversation, system="", tools=None):
            self.calls += 1
            if self.calls == 1:
                yield ToolCallComplete("read", "ReadFile", {"file_path": "source.txt"})
            else:
                assert any("startup evidence" in result.content for message in conversation.history for result in message.tool_results)
                yield TextDelta("startup verified")
            yield StreamEnd("end_turn")

    client = HookClient()
    monkeypatch.setattr(ui, "create_client", lambda _: client)
    async def resolve(_):
        return 32000
    monkeypatch.setattr(ui, "resolve_context_window", resolve)
    providers = [ProviderConfig(name, "anthropic", "http://unused.invalid", "offline", api_key="test-only", context_window=32000) for name in ("one", "two")]
    engine = HookEngine([Hook("first", "startup", Action(type="prompt", message="before agent")),
                         Hook("agent-startup", "startup", Action(type="agent", prompt="Read source.txt and verify startup"))])
    app = ui.EviForgeApp(providers, hook_engine=engine)
    async with app.run_test(size=(100, 35)) as pilot:
        await pilot.pause()
        assert app.runtime is None and app._hook_startup_task is None
        assert not any(hook.executed for hook in engine.hooks), "the entire ordered group waits for the Agent executor"
        app.query_one("#provider-list").focus()
        await pilot.press("enter")
        await pilot.pause()
        await asyncio.wait_for(asyncio.shield(app._hook_startup_task), timeout=3)
        assert client.calls == 2
        assert engine.agent_executor is not None
        assert engine.get_prompt_messages() == ["before agent"]
        notices = engine.drain_notifications()
        assert [note.hook_id for note in notices] == ["first", "agent-startup"]
        assert all(note.success for note in notices)
        assert notices[-1].output == "startup verified"
        task = app._hook_startup_task
        app._schedule_startup_hooks()
        assert app._hook_startup_task is task
        assert len(app.agent.children) == 1
    assert engine._background_tasks == set()


@pytest.mark.asyncio
async def test_nonagent_startup_still_runs_before_provider_selection():
    import eviforge.app as ui
    from eviforge.config import ProviderConfig
    from eviforge.hooks import Action, Hook, HookEngine
    providers = [ProviderConfig(name, "anthropic", "http://unused.invalid", "offline", api_key="test-only") for name in ("one", "two")]
    engine = HookEngine([Hook("prompt", "startup", Action(type="prompt", message="ready"))])
    app = ui.EviForgeApp(providers, hook_engine=engine)
    async with app.run_test(size=(100, 35)) as pilot:
        await pilot.pause()
        assert app.runtime is None
        assert engine.get_prompt_messages() == ["ready"]
        app._schedule_startup_hooks()
        assert engine.get_prompt_messages() == [], "reading consumes the message; startup must not enqueue it again"
