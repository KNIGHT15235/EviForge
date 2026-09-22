from pathlib import Path
from types import SimpleNamespace

import pytest

from eviforge.commands.handlers.memory import handle_memory
from eviforge.conversation import ConversationManager, Message
from eviforge.governance import GovernanceService
from eviforge.memory.auto_memory import MemoryManager


def context(manager, args):
    messages = []
    return SimpleNamespace(memory_manager=manager, args=args, conversation=ConversationManager(),
                           ui=SimpleNamespace(add_system_message=messages.append)), messages


@pytest.mark.asyncio
async def test_governed_memory_edit_explains_candidate_review_without_legacy_file_edit(tmp_path):
    service = GovernanceService(tmp_path, user_root=tmp_path / "user-data")
    ctx, messages = context(MemoryManager(str(tmp_path), governance=service), "edit")
    await handle_memory(ctx)
    output = messages[0]
    assert "governance --scope project import --kind memory" in output
    assert "verify、confirm、publish" in output
    assert "memories.md" not in output
    assert str(service.paths["project"]) in output
    assert service.list_entries() == []


@pytest.mark.asyncio
async def test_governed_clear_revokes_and_removes_already_injected_memory(tmp_path):
    service = GovernanceService(tmp_path, user_root=tmp_path / "user-data")
    entry = service.propose_memory("Previously approved memory", name="fact", source_task="task", source_trace="trace")
    service.record_verification(entry["id"], content_hash=entry["content_hash"], outcome="pass",
                                evidence={"checked": True}, validator="test")
    service.confirm(entry["id"], content_hash=entry["content_hash"], actor="human")
    service.publish(entry["id"], actor="human")
    manager = MemoryManager(str(tmp_path), governance=service)
    ctx, messages = context(manager, "clear")
    ctx.conversation.history = [Message("user", "Real user request")]
    manager.refresh_context(ctx.conversation)
    await handle_memory(ctx)
    assert "撤销" in messages[0] and "审计记录保留" in messages[0]
    assert service.get_entry(entry["id"])["status"] == "revoked"
    assert [message.content for message in ctx.conversation.history] == ["Real user request"]


@pytest.mark.asyncio
async def test_legacy_direct_memory_edit_preserves_original_file_guidance(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    manager = MemoryManager(str(tmp_path))
    ctx, messages = context(manager, "edit")
    await handle_memory(ctx)
    assert str(manager.project_path) in messages[0]
    assert str(manager.user_path) in messages[0]
    assert "import" not in messages[0]
