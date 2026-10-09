from __future__ import annotations

import asyncio

import pytest
from textual.widgets import Markdown, Static

import eviforge.app as ui
from eviforge.client import LLMClient, NetworkError
from eviforge.config import ProviderConfig
from eviforge.conversation import Message
from eviforge.tools.base import StreamEnd, TextDelta


@pytest.fixture
def ui_environment(tmp_path, monkeypatch):
    project, home = tmp_path / "project", tmp_path / "home"
    project.mkdir()
    home.mkdir()
    monkeypatch.chdir(project)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))

    class Client(LLMClient):
        release = asyncio.Event()
        fail = False

        async def stream(self, conversation, system="", tools=None):
            if not tools:
                yield TextDelta("会话摘要")
                yield StreamEnd("end_turn")
                return
            yield TextDelta("我是 **EviForge**，终端智能编程助手。")
            await self.release.wait()
            if self.fail:
                raise NetworkError("offline stream interrupted")
            yield TextDelta("\n\n可以帮你理解代码。")
            yield StreamEnd("end_turn", input_tokens=1000, output_tokens=200)

    client = Client()
    monkeypatch.setattr(ui, "create_client", lambda _: client)
    async def resolve(_):
        return None
    monkeypatch.setattr(ui, "resolve_context_window", resolve)
    provider = ProviderConfig("test", "openai-compat", "http://unused.invalid", "test-model",
                              api_key="test-only", context_window=10000)
    return ui.EviForgeApp([provider]), client


async def wait_until(pilot, predicate):
    for _ in range(100):
        await pilot.pause(0.02)
        if predicate():
            return
    raise AssertionError("UI did not settle")


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(136, 44), (80, 24), (58, 20)])
async def test_composer_and_status_remain_visible_on_resize(ui_environment, size):
    app, client = ui_environment
    async with app.run_test(size=(136, 44)) as pilot:
        await pilot.resize_terminal(*size)
        await pilot.pause()
        chat = app.query_one("#chat-area")
        editor = app.query_one("#chat-input", ui.ChatInput)
        status = app.query_one("#status-bar")
        assert 0 < chat.region.height
        assert chat.region.bottom <= editor.region.y
        assert editor.region.bottom <= status.region.y
        assert status.region.bottom <= size[1]
        assert editor.region.right <= size[0]
        assert app.focused is editor
        assert app._connection_state == "模型就绪"
        editor.insert("第一行")
        await pilot.press("shift+enter")
        editor.insert("第二行")
        assert editor.text == "第一行\n第二行"
        app.conversation.add_user_message("long " * 600)
        await app._render_restored_messages(app.conversation.history)
        await pilot.pause()
        assert editor.region.bottom <= status.region.y
        assert status.region.bottom <= size[1]


@pytest.mark.asyncio
async def test_stream_finishes_as_markdown_without_cursor_or_duplicate_reply(ui_environment):
    app, client = ui_environment
    async with app.run_test(size=(120, 40)) as pilot:
        editor = app.query_one("#chat-input", ui.ChatInput)
        editor.insert("介绍一下你自己")
        await pilot.press("enter")
        await wait_until(pilot, lambda: app._connection_state == "模型已连接")
        assert "▌" in app.export_screenshot()
        client.release.set()
        await wait_until(pilot, lambda: not app._streaming)
        await pilot.pause()
        assert len(app.query(".ai-row")) == 1
        assert len(app.query(Markdown)) == 1
        assert not app.query("#spinner-live")
        assert "▌" not in app.export_screenshot()
        assert app._usage_tokens == 1200
        assert "1.2k" in str(app.query_one("#token-label", Static).render())
        assert app.focused is editor
        assert editor.text == ""
        await pilot.press("up")
        assert editor.text == "介绍一下你自己"


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [True, False])
async def test_partial_reply_survives_stop_or_provider_failure(ui_environment, cancel):
    app, client = ui_environment
    async with app.run_test(size=(110, 35)) as pilot:
        app.send_user_message("介绍一下你自己")
        await wait_until(pilot, lambda: app._connection_state == "模型已连接")
        if cancel:
            await pilot.press("escape")
        else:
            client.fail = True
            client.release.set()
        await wait_until(pilot, lambda: not app._streaming)
        await pilot.pause()
        assert len(app.query(Markdown)) == 1
        assert "▌" not in app.export_screenshot()
        assert not app.query("#spinner-live")
        assert app.focused is app.query_one("#chat-input")
        if not cancel:
            assert app._connection_state == "请求异常"


@pytest.mark.asyncio
async def test_restored_messages_keep_roles_and_literal_user_text(ui_environment):
    app, _ = ui_environment
    async with app.run_test(size=(100, 35)) as pilot:
        await app._render_restored_messages([
            Message("user", "[bold]介绍一下你自己[/bold]"),
            Message("assistant", "我是 **EviForge**。"),
        ])
        await pilot.pause()
        assert len(app.query(".role-label")) == 2
        assert "[bold]" in str(app.query_one(".user-message", Static).render())
        assert len(app.query(Markdown)) == 1


@pytest.mark.asyncio
async def test_hook_details_expand_by_keyboard_and_errors_start_visible(ui_environment):
    app, _ = ui_environment
    async with app.run_test(size=(110, 35)) as pilot:
        notice = ui.HookNotice("evidence_contract", True, "[bold]literal audit detail[/bold]")
        await app.query_one("#chat-area").mount(notice)
        await pilot.pause()
        assert "literal audit detail" not in str(notice.render())
        notice.focus()
        await pilot.press("enter")
        assert "[bold]literal audit detail[/bold]" in str(notice.render())
        failed = ui.HookNotice("check_changed_code", False, "Actual check failure")
        await app.query_one("#chat-area").mount(failed)
        assert "Actual check failure" in str(failed.render())
