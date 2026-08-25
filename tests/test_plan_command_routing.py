from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from mewcode.commands.handlers.permission import handle_permission
from mewcode.permissions import PermissionMode


@pytest.mark.asyncio
async def test_permission_plan_command_routes_through_ui_state_machine() -> None:
    ui = SimpleNamespace(
        set_plan_mode=Mock(),
        add_system_message=Mock(),
        refresh_status=Mock(),
    )
    agent = SimpleNamespace(
        permission_mode=PermissionMode.DEFAULT,
        set_permission_mode=Mock(),
    )
    context = SimpleNamespace(args="mode plan", agent=agent, ui=ui)

    await handle_permission(context)

    ui.set_plan_mode.assert_called_once_with(True)
    agent.set_permission_mode.assert_not_called()


@pytest.mark.asyncio
async def test_non_plan_permission_command_does_not_start_plan_session() -> None:
    ui = SimpleNamespace(
        set_plan_mode=Mock(),
        add_system_message=Mock(),
        refresh_status=Mock(),
    )
    agent = SimpleNamespace(
        permission_mode=PermissionMode.DEFAULT,
        set_permission_mode=Mock(),
    )
    context = SimpleNamespace(args="mode acceptEdits", agent=agent, ui=ui)

    await handle_permission(context)

    agent.set_permission_mode.assert_called_once_with(PermissionMode.ACCEPT_EDITS)
    ui.set_plan_mode.assert_not_called()
