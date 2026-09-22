from __future__ import annotations

import copy
import gc
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from eviforge import __main__ as cli
from eviforge.agent import Agent, CompactNotification
from eviforge.agents.task_manager import BackgroundTask
from eviforge.client import NetworkError
from eviforge.context.manager import CompactBoundary
from eviforge.conversation import ConversationManager, Message, ToolResultBlock, ToolUseBlock
from eviforge.governance import GovernanceService
from eviforge.governance.context import strip_governed_context
from eviforge.memory.auto_memory import MemoryManager
from eviforge.memory.recorder import SessionRecorder
from eviforge.memory.session import SessionManager
from eviforge.permissions import PermissionMode
from eviforge.runtime import RuntimeServices
from eviforge.tools import ToolRegistry
from eviforge.tools.base import StreamEnd, TextDelta, ToolCallComplete
from test_document_headless import isolated_project


def publish_memory(service):
    entry = service.propose_memory("Governed fact", name="fact", source_task="task", source_trace="trace")
    service.record_verification(entry["id"], content_hash=entry["content_hash"], outcome="pass", evidence={"ok": True}, validator="test")
    service.confirm(entry["id"], content_hash=entry["content_hash"], actor="human")
    service.publish(entry["id"], actor="human")
    return entry


@pytest.mark.asyncio
async def test_real_agent_revocation_cannot_skip_new_user_assistant_or_tool_records(tmp_path):
    service = GovernanceService(tmp_path, user_root=tmp_path / "home")
    entry = publish_memory(service)
    manager = MemoryManager(str(tmp_path), governance=service)
    sessions = SessionManager(str(tmp_path))
    session = sessions.create()
    recorder = SessionRecorder(session)
    conversation = ConversationManager()
    conversation.inject_environment("generated environment")
    manager.refresh_context(conversation)
    conversation.add_user_message("first question")
    conversation.add_assistant_message("first answer")
    recorder.flush(conversation)
    conversation.add_user_message("second question")
    recorder.flush(conversation)
    service.revoke(entry["id"], actor="human", reason="obsolete")

    class Client:
        async def stream(self, conversation, system="", tools=None):
            yield TextDelta("second answer")
            yield StreamEnd("end_turn")

    agent = Agent(Client(), ToolRegistry(), "openai", str(tmp_path), memory_manager=manager)
    await agent.run_to_completion("", conversation)
    conversation.history.append(Message("assistant", "checking", tool_uses=[ToolUseBlock("read-1", "ReadFile", {"file_path": "x"})]))
    conversation.history.append(Message("user", "", tool_results=[ToolResultBlock("read-1", "file bytes")]))
    assert recorder.flush(conversation) == 3
    assert recorder.flush(conversation) == 0
    session.close()
    restored = sessions.resume(session.session_id)
    try:
        assert [message.content for message in restored.messages] == ["first question", "first answer", "second question", "second answer", "checking", ""]
        assert restored.messages[-2].tool_uses[0].tool_use_id == "read-1"
        assert restored.messages[-1].tool_results[0].content == "file bytes"
        resumed_recorder = SessionRecorder(restored.session, restored.messages)
        resumed = ConversationManager(restored.messages)
        resumed.add_user_message("third question")
        assert resumed_recorder.flush(resumed) == 1
    finally:
        restored.session.close()


def test_generated_context_markers_survive_copy_but_real_user_tags_are_saved(tmp_path):
    sessions = SessionManager(str(tmp_path))
    session = sessions.create()
    recorder = SessionRecorder(session)
    conversation = ConversationManager()
    conversation.inject_environment("Current working directory: example")
    conversation.inject_long_term_memory("generated instructions", "")
    copied = strip_governed_context(copy.deepcopy(conversation.history))
    assert all(getattr(message, "_generated_context", False) for message in copied)
    literal = "<eviforge-governed-memory>\nThis is a literal user example.\n</eviforge-governed-memory>"
    conversation.add_user_message(literal)
    assert recorder.flush(conversation) == 1
    session.close()
    restored = sessions.resume(session.session_id)
    try:
        assert [message.content for message in restored.messages] == [literal]
    finally:
        restored.session.close()


def test_compact_callback_persists_boundary_once_then_accepts_new_messages(tmp_path):
    from eviforge.app import EviForgeApp
    sessions = SessionManager(str(tmp_path))
    session = sessions.create()
    conversation = ConversationManager([Message("user", "old question"), Message("assistant", "old answer")])
    recorder = SessionRecorder(session)
    recorder.flush(conversation)
    keep = Message("user", "retained tail")
    conversation.replace_history([Message("user", "summary wrapper"), keep])
    conversation.inject_environment("new generated environment")
    fake = SimpleNamespace(session=session, conversation=conversation, session_recorder=recorder)
    fake._get_session_recorder = lambda: recorder
    EviForgeApp._persist_compact_boundary(fake, CompactNotification(100, "compacted", CompactBoundary("summary", [keep])))
    conversation.add_assistant_message("new answer after compaction")
    assert recorder.flush(conversation) == 1
    session.close()
    restored = sessions.resume(session.session_id)
    try:
        content = [message.content for message in restored.messages]
        assert len(content) == 3
        assert "summary" in content[0]
        assert content[1:] == ["retained tail", "new answer after compaction"]
    finally:
        restored.session.close()
    conversation.history.clear()
    del keep
    gc.collect()
    assert len(recorder._saved) == 0


@pytest.mark.asyncio
async def test_headless_background_notification_cannot_overwrite_blocked_status(isolated_project, monkeypatch):
    created = []
    original = RuntimeServices.create
    def create(*args, **kwargs):
        runtime = original(*args, **kwargs)
        created.append(runtime)
        return runtime
    monkeypatch.setattr(RuntimeServices, "create", create)

    class Client:
        def __init__(self): self.calls = 0
        async def stream(self, conversation, system="", tools=None):
            self.calls += 1
            if self.calls == 1:
                yield ToolCallComplete("write-1", "WriteFile", {"file_path": "denied.txt", "content": "no"})
                yield StreamEnd("tool_use")
            else:
                runtime = created[0]
                runtime.task_manager._tasks["done"] = BackgroundTask("done", "done", runtime.agent, "", status="completed", result="notification")
                runtime.task_manager._notify_queue.put_nowait("done")
                yield TextDelta("permission denied" if self.calls == 2 else "incorrect success")
                yield StreamEnd("end_turn")
    client = Client()
    monkeypatch.setattr("eviforge.client.create_client", lambda _: client)
    result = await cli._run_prompt(isolated_project, PermissionMode.DEFAULT, None, "write denied.txt", options=cli.AutomationOptions(output_format="json"))
    assert client.calls == 2
    assert (result.status, result.exit_code, result.output) == ("blocked", 3, "permission denied")
    assert not Path("denied.txt").exists()
    assert created[0].closed


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["failed", "ambiguous", "blocked", "approval_required", "cancelled"])
async def test_tui_failed_task_and_mailbox_notifications_are_displayed_without_continuation(status):
    from eviforge.app import EviForgeApp
    worker = SimpleNamespace(agent_id="child")
    background = BackgroundTask("done", "done", worker, "", status="completed", result="notification")
    fake = SimpleNamespace(
        agent=SimpleNamespace(last_run_status=status), _streaming=False,
        task_manager=SimpleNamespace(poll_completed=lambda: [background]),
        team_manager=SimpleNamespace(on_teammate_completed=Mock(), drain_lead_mailbox=lambda: ["mailbox notice"]),
        conversation=ConversationManager(), _show_system_message=Mock(),
        _send_message=Mock(side_effect=AssertionError("unexpected automatic continuation")),
    )
    await EviForgeApp._process_task_notifications(fake)
    await EviForgeApp._process_mailbox_notifications(fake)
    assert fake._show_system_message.call_count == 2
    fake._send_message.assert_not_called()


@pytest.mark.asyncio
async def test_real_tui_partial_failure_does_not_consume_background_notification_as_a_new_request(isolated_project, monkeypatch):
    import eviforge.app as ui
    class Client:
        def __init__(self): self.calls = 0
        async def stream(self, conversation, system="", tools=None):
            self.calls += 1
            app.task_manager._tasks["done"] = BackgroundTask("done", "done", app.agent, "", status="completed", result="notification")
            app.task_manager._notify_queue.put_nowait("done")
            yield TextDelta("partial")
            raise NetworkError("stream interrupted")
    client = Client()
    monkeypatch.setattr(ui, "create_client", lambda _: client)
    async def resolve(_): return None
    monkeypatch.setattr(ui, "resolve_context_window", resolve)
    app = ui.EviForgeApp(isolated_project.providers)
    async with app.run_test(size=(100, 35)) as pilot:
        await pilot.pause()
        await app._send_message("trigger partial failure")
        await pilot.pause()
        assert app.agent.last_run_status == "ambiguous"
        assert client.calls == 1
        assert not app._streaming
