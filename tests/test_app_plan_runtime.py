from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from mewcode.app import EviForgeApp
from mewcode.permissions import PermissionMode
from mewcode.plan_dialog import InlinePlanWidget, PlanChoice


class FakeComponents:
    def __init__(self, task_id: str, control_root: Path) -> None:
        self.store = SimpleNamespace(paths=SimpleNamespace(control_root=control_root))
        self.gateway = object()
        self.execution_context = SimpleNamespace(task_id=task_id)
        self.task = SimpleNamespace(task_id=task_id)
        self.evolution = object()
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_each_approved_plan_replaces_terminal_task_runtime(
    tmp_path: Path, monkeypatch
) -> None:
    built: list[FakeComponents] = []
    control_root = tmp_path / "control"

    class FakeBuilder:
        def __init__(self, work_dir, **kwargs) -> None:
            assert Path(work_dir) == tmp_path
            assert kwargs["control_root"] == control_root
            assert kwargs["permission_checker"] is checker

        def build(self, *, task_id: str) -> FakeComponents:
            component = FakeComponents(task_id, control_root)
            built.append(component)
            return component

    checker = object()
    old = FakeComponents("completed-task", control_root)
    app = EviForgeApp(providers=[])
    app.agent = SimpleNamespace(
        work_dir=str(tmp_path),
        permission_checker=checker,
        execution_gateway=old.gateway,
        execution_context=old.execution_context,
        task_runtime=old.task,
        evolution_adapter=old.evolution,
    )
    app.runtime_components = old
    monkeypatch.setattr("mewcode.runtime.RuntimeBuilder", FakeBuilder)

    first = app._replace_runtime_for_plan("plan-one")
    second = app._replace_runtime_for_plan("plan-two")

    assert old.closed
    assert first.closed
    assert not second.closed
    assert app.runtime_components is second
    assert app.agent.task_runtime.task_id == "plan-two"
    assert app.agent.execution_context.task_id == "plan-two"


def test_shift_tab_uses_same_plan_transition_api(monkeypatch) -> None:
    class FakeAgent:
        def __init__(self) -> None:
            self.permission_mode = PermissionMode.ACCEPT_EDITS
            self.plan_session = None
            self.started = 0
            self.closed = 0

        def begin_plan_session(self, *, pre_permission_mode):
            self.started += 1
            self.permission_mode = PermissionMode.PLAN
            self.plan_session = SimpleNamespace(
                pre_permission_mode=pre_permission_mode,
            )
            return self.plan_session

        def close_plan_session(self, *, restore_mode):
            self.closed += 1
            self.permission_mode = restore_mode
            return restore_mode

        def set_permission_mode(self, mode):
            self.permission_mode = mode

    app = EviForgeApp(providers=[])
    agent = FakeAgent()
    app.agent = agent
    monkeypatch.setattr(app, "_update_mode_label", lambda: None)

    # acceptEdits -> Plan starts a fresh state-machine session.
    app.action_cycle_mode()
    assert agent.permission_mode is PermissionMode.PLAN
    assert agent.started == 1
    assert agent.plan_session.pre_permission_mode is PermissionMode.ACCEPT_EDITS

    # Plan -> bypass explicitly closes the draft; it does not approve or run it.
    app.action_cycle_mode()
    assert agent.permission_mode is PermissionMode.BYPASS
    assert agent.closed == 1


def test_escape_cancel_never_starts_plan_execution(monkeypatch) -> None:
    app = EviForgeApp(providers=[])
    agent = SimpleNamespace(
        permission_mode=PermissionMode.PLAN,
        is_plan_review_ready=Mock(return_value=True),
        cancel_plan_review=Mock(),
        start_plan_execution=Mock(),
        begin_contract_execution=Mock(),
    )
    app.agent = agent
    monkeypatch.setattr(app, "query_one", Mock(side_effect=LookupError))
    monkeypatch.setattr(app, "_update_mode_label", Mock())
    monkeypatch.setattr(app, "_show_system_message", Mock())
    event = InlinePlanWidget.Responded(
        PlanChoice.CANCEL,
        session_id="current-session",
        plan_fingerprint="current-hash",
    )

    app.on_inline_plan_widget_responded(event)

    agent.cancel_plan_review.assert_called_once_with()
    agent.start_plan_execution.assert_not_called()
    agent.begin_contract_execution.assert_not_called()
    assert agent.permission_mode is PermissionMode.PLAN


@pytest.mark.asyncio
async def test_recovery_block_defers_provider_hooks_and_mcp() -> None:
    app = EviForgeApp(providers=[])
    app._selected_provider = SimpleNamespace()
    app._recovery_blocked = True
    app.run_worker = Mock()
    app.hook_engine = SimpleNamespace(run_hooks=AsyncMock())
    app._mcp_server_configs = [SimpleNamespace(name="fixture")]
    app._init_mcp = AsyncMock()

    app._start_runtime_services()
    assert app.run_worker.call_count == 0
    app.hook_engine.run_hooks.assert_not_awaited()
    app._init_mcp.assert_not_awaited()

    app._recovery_blocked = False
    app._start_runtime_services()
    await asyncio.sleep(0)
    if app._mcp_init_task is not None:
        await app._mcp_init_task

    assert app.run_worker.call_count == 1
    # The Textual worker owns this coroutine in production; close the fixture
    # coroutine because the mocked worker intentionally does not schedule it.
    app.run_worker.call_args.args[0].close()
    app.hook_engine.run_hooks.assert_awaited_once()
    app._init_mcp.assert_awaited_once()
    assert app._runtime_services_started is True
