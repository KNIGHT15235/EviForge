from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from eviforge.conversation import ConversationManager, Message
from eviforge.governance import GovernanceError, GovernanceService, MEMORY_BUDGET
from eviforge.governance.cli import handle, register_parser
from eviforge.memory.auto_memory import MemoryManager
from eviforge.prompts import build_environment_context
from eviforge.skills.loader import SkillLoader
from eviforge.tools.base import StreamEnd, TextDelta


@pytest.fixture
def service(tmp_path, monkeypatch):
    user = tmp_path / "home"
    user.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: user))
    return GovernanceService(tmp_path / "project")


def candidate(service, content="Use explicit encodings.", *, name="encoding", scope="project", kind="memory"):
    propose = service.propose_memory if kind == "memory" else service.propose_skill
    return propose(content, scope=scope, name=name, source_task="task-1", source_trace="trace-1")


def verified(service, entry):
    return service.record_verification(
        entry["id"], scope=entry["scope"], content_hash=entry["content_hash"], outcome="pass",
        evidence={"checks": [{"name": "encoding fixture", "passed": True}]}, validator="fixture-checker",
    )


def published(service, entry):
    verified(service, entry)
    service.confirm(entry["id"], scope=entry["scope"], content_hash=entry["content_hash"], actor="human-reviewer")
    return service.publish(entry["id"], scope=entry["scope"], actor="human-reviewer")


def skill_markdown(body="Follow the checked procedure.", name="checked-skill"):
    return f"---\nname: {name}\ndescription: A reviewed procedure\nallowedTools: [ReadFile]\n---\n{body}"


def test_quarantine_is_persistent_auditable_and_never_injected(service):
    entry = candidate(service, "unreviewed claim")
    assert entry["status"] == "quarantine"
    assert entry["content_hash"] == hashlib.sha256(b"unreviewed claim").hexdigest()
    reopened = GovernanceService(service.work_dir)
    record = reopened.get_entry(entry["id"])
    assert record["source_task"] == "task-1" and record["source_trace"] == "trace-1"
    assert record["audit"][0]["action"] == "propose"
    assert reopened.render_memory_context() == ""
    assert reopened.list_entries()[0]["integrity_valid"]


def test_user_records_follow_user_and_project_records_stay_local(service, tmp_path):
    published(service, candidate(service, "USER FACT", scope="user"))
    published(service, candidate(service, "PROJECT FACT"))
    other = GovernanceService(tmp_path / "other-project")
    assert "USER FACT" in other.render_memory_context()
    assert "PROJECT FACT" not in other.render_memory_context()
    assert not other.paths["project"].exists()
    assert "PROJECT FACT" in service.render_memory_context()


def test_running_in_home_directory_keeps_user_and_project_scopes_distinct(service):
    at_home = GovernanceService(Path.home())
    user = published(at_home, candidate(at_home, "USER IN HOME", scope="user"))
    project = published(at_home, candidate(at_home, "PROJECT IN HOME"))
    assert at_home.paths["user"] != at_home.paths["project"]
    assert user["version"] == project["version"] == 1
    assert len(at_home.active_entries(kind="memory")) == 2


def test_publication_requires_verification_and_exact_human_confirmation(service):
    entry = candidate(service)
    with pytest.raises(GovernanceError):
        service.publish(entry["id"], actor="operator")
    with pytest.raises(GovernanceError, match="different content hash"):
        service.record_verification(entry["id"], content_hash="bad", outcome="pass", evidence={"ok": True}, validator="test")
    verified(service, entry)
    with pytest.raises(GovernanceError, match="Human confirmation"):
        service.publish(entry["id"], actor="operator")
    with pytest.raises(GovernanceError, match="different content hash"):
        service.confirm(entry["id"], content_hash="bad", actor="human")
    service.confirm(entry["id"], content_hash=entry["content_hash"], actor="human")
    service.publish(entry["id"], actor="human")
    assert "Use explicit encodings." in service.render_memory_context()


@pytest.mark.parametrize("evidence", [{}, [], ["pass"], "pass", None])
def test_verification_requires_nonempty_structured_evidence(service, evidence):
    entry = candidate(service)
    with pytest.raises(GovernanceError):
        service.record_verification(entry["id"], content_hash=entry["content_hash"], outcome="pass",
                                    evidence=evidence, validator="fixture")


def test_negative_feedback_revokes_and_cannot_be_overwritten_by_positive_feedback(service):
    entry = published(service, candidate(service))
    service.feedback(entry["id"], sentiment="negative", reason="failed a real fixture", source_task="task-2")
    assert not service.render_memory_context()
    service.feedback(entry["id"], sentiment="positive", reason="another fixture passed", source_task="task-3")
    verified(service, entry)
    with pytest.raises(GovernanceError):
        service.publish(entry["id"], actor="human")
    assert service.get_entry(entry["id"])["status"] == "revoked"


def test_failed_reverification_immediately_revokes(service):
    entry = published(service, candidate(service))
    service.record_verification(entry["id"], content_hash=entry["content_hash"], outcome="fail",
                                evidence={"regression": "failed"}, validator="test-runner")
    assert service.active_entries(kind="memory") == []


def test_versions_rollback_preserve_audit_and_reject_unpublished_target(service):
    first = published(service, candidate(service, "v1"))
    second = published(service, candidate(service, "v2"))
    third = candidate(service, "v3")
    assert [entry["version"] for entry in service.list_entries()] == [1, 2, 3]
    assert service.get_entry(first["id"])["status"] == "superseded"
    with pytest.raises(GovernanceError, match="previously published"):
        service.rollback(kind="memory", name="encoding", target_version=third["version"], actor="human")
    service.rollback(kind="memory", name="encoding", target_version=1, actor="human")
    assert service.active_entries(kind="memory")[0]["id"] == first["id"]
    assert service.get_entry(second["id"])["status"] == "superseded"
    assert service.get_entry(first["id"])["audit"][-1]["action"] == "rollback"
    service.feedback(first["id"], sentiment="negative", reason="obsolete", source_task="task-4")
    with pytest.raises(GovernanceError):
        service.rollback(kind="memory", name="encoding", target_version=1, actor="human")


@pytest.mark.parametrize("table,column,value", [
    ("entries", "content", "tampered memory"),
    ("verifications", "evidence_json", '{"tampered":true}'),
])
def test_content_and_evidence_hash_tampering_fail_closed(service, table, column, value):
    entry = published(service, candidate(service))
    with sqlite3.connect(service.paths["project"]) as connection:
        connection.execute(f"UPDATE {table} SET {column}=?", (value,))
    assert service.render_memory_context() == ""
    with pytest.raises(GovernanceError):
        service.publish(entry["id"], actor="human")


def test_fair_16000_character_budget_counts_unicode_and_metadata(service):
    published(service, candidate(service, "用" * 20_000, scope="user"))
    published(service, candidate(service, "项" * 20_000))
    text = service.render_memory_context()
    assert len(text) == MEMORY_BUDGET
    assert 7800 < text.count("用") < 8000
    assert text.count("用") - text.count("项") in range(-4, 4)
    assert "[truncated]" in text
    assert len(service.render_memory_context(100)) == 100


def test_short_scope_redistributes_capacity_and_records_share_fairly(service):
    published(service, candidate(service, "short user memory", scope="user"))
    published(service, candidate(service, "甲" * 15_000, name="first"))
    published(service, candidate(service, "乙" * 15_000, name="second"))
    text = service.render_memory_context()
    assert len(text) == MEMORY_BUDGET
    assert "short user memory" in text
    assert 7700 < text.count("甲") < 8000
    assert abs(text.count("甲") - text.count("乙")) <= 1


def test_governed_memory_ignores_legacy_files_and_refreshes_revocation(service):
    manager = MemoryManager(str(service.work_dir), governance=service)
    manager.project_path.parent.mkdir(parents=True)
    manager.project_path.write_text("legacy unreviewed memory", encoding="utf-8")
    assert manager.load() == ""
    with pytest.raises(ValueError):
        manager._write_memories("bypass")
    entry = published(service, candidate(service, "approved memory"))
    conversation = ConversationManager([Message("user", "Real user request")])
    assert manager.refresh_context(conversation)
    assert "approved memory" in conversation.history[0].content
    assert len(conversation.history[0].content) <= MEMORY_BUDGET
    conversation.record_usage_anchor(500)
    assert not manager.refresh_context(conversation)
    assert conversation.baseline_tokens == 500
    service.feedback(entry["id"], sentiment="negative", reason="invalidated", source_task="new-task")
    assert manager.refresh_context(conversation)
    assert [message.content for message in conversation.history] == ["Real user request"]
    assert conversation.baseline_tokens == 0
    assert manager.project_path.read_text(encoding="utf-8") == "legacy unreviewed memory"


def test_memory_refresh_enforces_budget_including_wrapper(service):
    published(service, candidate(service, "文" * 20_000))
    conversation = ConversationManager()
    MemoryManager(str(service.work_dir), governance=service).refresh_context(conversation)
    assert len(conversation.history[0].content) == 16_000


def test_memory_clear_revokes_both_scopes_without_erasing_audit(service):
    first = published(service, candidate(service, scope="user"))
    candidate(service)
    MemoryManager(str(service.work_dir), governance=service).clear()
    assert not service.render_memory_context()
    assert {entry["status"] for entry in service.list_entries()} == {"revoked"}
    assert service.get_entry(first["id"], scope="user")["audit"][-1]["action"] == "revoke"


class ExtractionClient:
    def __init__(self, complete=True):
        self.complete = complete

    async def stream(self, conversation, system="", tools=None):
        yield TextDelta("### 用户偏好\n- 用户喜欢中文回答\n### 项目知识\n- 项目使用 SQLite\n")
        if self.complete:
            yield StreamEnd(stop_reason="end_turn")


@pytest.mark.asyncio
async def test_session_extraction_creates_only_traceable_quarantine(service):
    manager = MemoryManager(str(service.work_dir), governance=service)
    conversation = ConversationManager([Message("user", "请用中文回答，这个项目用 SQLite。")])
    await manager.extract(ExtractionClient(), conversation, "openai", source_task="session-123", source_trace="trace-456")
    records = service.list_entries()
    assert {record["scope"] for record in records} == {"project", "user"}
    assert {record["status"] for record in records} == {"quarantine"}
    assert {record["source_task"] for record in records} == {"session-123"}
    assert {record["source_trace"] for record in records} == {"trace-456"}
    assert not manager.load()
    assert not manager.project_path.exists() and not manager.user_path.exists()


@pytest.mark.asyncio
async def test_incomplete_extraction_does_not_create_candidates(service):
    manager = MemoryManager(str(service.work_dir), governance=service)
    await manager.extract(ExtractionClient(complete=False), ConversationManager([Message("user", "A fact")]), "openai")
    assert service.list_entries() == []


def test_published_skill_shadows_disk_and_revocation_never_falls_back(service):
    directory = service.work_dir / ".eviforge" / "skills"
    directory.mkdir(parents=True)
    (directory / "checked-skill.md").write_text(skill_markdown("old unchecked disk content"), encoding="utf-8")
    loader = SkillLoader(str(service.work_dir), governance=service)
    loader.load_all()
    assert "old unchecked disk content" in loader.get("checked-skill").prompt_body
    entry = candidate(service, skill_markdown("approved version"), name="checked-skill", kind="skill")
    assert "old unchecked disk content" in loader.get("checked-skill").prompt_body
    published(service, entry)
    assert "approved version" in loader.get("checked-skill").prompt_body
    assert loader.get_source_label("checked-skill") == "governed:project:v1"
    service.feedback(entry["id"], sentiment="negative", reason="regression", source_task="task-2")
    assert loader.get("checked-skill") is None
    assert "checked-skill" not in loader.reload()
    assert "checked-skill" not in dict(loader.get_catalog())


def test_project_skill_tombstone_shadows_user_governed_skill(service):
    published(service, candidate(service, skill_markdown("user skill"), name="checked-skill", kind="skill", scope="user"))
    project = published(service, candidate(service, skill_markdown("project skill"), name="checked-skill", kind="skill"))
    service.feedback(project["id"], sentiment="negative", reason="disable this skill", source_task="task-2")
    assert service.get_published_skill("checked-skill") is None


def test_active_skill_env_and_catalog_refresh_after_update_rollback_and_revoke(service):
    first = published(service, candidate(service, skill_markdown("version one $ARGUMENTS"), name="checked-skill", kind="skill"))
    loader = SkillLoader(str(service.work_dir), governance=service)
    loader.load_all()
    body = loader.get("checked-skill").prompt_body.replace("$ARGUMENTS", "target.py")
    conversation = ConversationManager([Message("user", build_environment_context(str(service.work_dir), {"checked-skill": body}))])
    agent = SimpleNamespace(work_dir=str(service.work_dir), active_skills={"checked-skill": body}, conversation=conversation,
                            _current_conversation=conversation, _skill_catalog="", _agent_catalog="")
    loader.refresh_active_skills(agent)
    assert "target.py" in agent.active_skills["checked-skill"]
    assert not loader.refresh_active_skills(agent)
    second = published(service, candidate(service, skill_markdown("version two"), name="checked-skill", kind="skill"))
    conversation.record_usage_anchor(100)
    assert loader.refresh_active_skills(agent)
    assert "version two" in conversation.history[0].content
    assert "version one" not in conversation.history[0].content
    assert conversation.baseline_tokens == 0
    service.rollback(kind="skill", name="checked-skill", target_version=first["version"], actor="human")
    assert loader.refresh_active_skills(agent)
    assert "version one" in conversation.history[0].content
    service.feedback(first["id"], sentiment="negative", reason="retired", source_task="task-2")
    assert loader.refresh_active_skills(agent)
    assert "checked-skill" not in agent.active_skills
    assert "version one" not in conversation.history[0].content
    assert "checked-skill" not in agent._skill_catalog
    assert service.get_entry(second["id"])["status"] == "superseded"


def test_cli_explicit_import_full_review_publish_and_feedback(service, tmp_path, capsys):
    parser = argparse.ArgumentParser()
    register_parser(parser.add_subparsers(dest="command", required=True))
    content = tmp_path / "candidate.md"
    content.write_text("CLI approved fact", encoding="utf-8-sig")
    evidence = tmp_path / "evidence.json"
    evidence.write_text('{"test":"fixture passed"}', encoding="utf-8-sig")

    def run(*argv):
        code = handle(parser.parse_args(["governance", "--work-dir", str(service.work_dir), *argv]))
        return code, json.loads(capsys.readouterr().out)

    code, result = run("import", "--kind", "memory", "--name", "cli-fact", "--file", str(content),
                       "--source-task", "manual-task", "--source-trace", "manual-trace")
    assert code == 0
    entry = result["result"]
    assert run("publish", entry["id"], "--actor", "human")[0] == 2
    assert run("verify", entry["id"], "--content-hash", entry["content_hash"], "--outcome", "pass",
               "--evidence", str(evidence), "--validator", "fixture")[0] == 0
    assert run("confirm", entry["id"], "--content-hash", entry["content_hash"], "--actor", "human")[0] == 0
    assert run("publish", entry["id"], "--actor", "human")[0] == 0
    assert "CLI approved fact" in service.render_memory_context()
    assert run("feedback", entry["id"], "--sentiment", "negative", "--reason", "obsolete", "--source-task", "new-task")[0] == 0
    assert not service.render_memory_context()


@pytest.mark.asyncio
async def test_actual_agent_request_observes_governed_memory_and_revocation(service):
    from eviforge.agent import Agent
    from eviforge.tools import ToolRegistry

    class Client:
        def __init__(self):
            self.calls = []

        async def stream(self, conversation, system="", tools=None):
            self.calls.append(copy.deepcopy(conversation.history))
            yield TextDelta("done")
            yield StreamEnd(stop_reason="end_turn")

    client = Client()
    agent = Agent(client, ToolRegistry(), "openai", str(service.work_dir))
    agent.memory_manager = MemoryManager(str(service.work_dir), governance=service)
    candidate(service, "QUARANTINE SECRET", name="unreviewed")
    entry = published(service, candidate(service, "PUBLISHED MEMORY"))
    await agent.run_to_completion("check first request")
    first = "\n".join(message.content for message in client.calls[-1])
    assert "PUBLISHED MEMORY" in first and "QUARANTINE SECRET" not in first
    assert first.count("PUBLISHED MEMORY") == 1
    service.feedback(entry["id"], sentiment="negative", reason="obsolete", source_task="task-next")
    await agent.run_to_completion("check revoked request")
    assert "PUBLISHED MEMORY" not in "\n".join(message.content for message in client.calls[-1])


def test_summary_filter_preserves_source_history_and_ordinary_user_quotes(service):
    from eviforge.governance.context import strip_governed_context

    published(service, candidate(service, "APPROVED MEMORY"))
    published(service, candidate(service, skill_markdown("APPROVED SKILL"), name="checked-skill", kind="skill"))
    loader = SkillLoader(str(service.work_dir), governance=service)
    loader.load_all()
    body = loader.get("checked-skill").prompt_body
    conversation = ConversationManager([
        Message("user", build_environment_context(str(service.work_dir), {"checked-skill": body})),
        Message("user", "Explain the literal <eviforge-governed-memory> tag."),
        Message("user", body),
    ])
    MemoryManager(str(service.work_dir), governance=service).refresh_context(conversation)
    original = copy.deepcopy(conversation.history)
    summary_input = strip_governed_context(conversation.history)
    text = "\n".join(message.content for message in summary_input)
    assert "APPROVED MEMORY" not in text and "APPROVED SKILL" not in text
    assert "Explain the literal" in text
    assert conversation.history == original
    summary_input[0].content = "mutation"
    assert conversation.history == original


def test_skill_body_with_literal_closing_tag_does_not_escape_summary_filter(service):
    from eviforge.governance.context import strip_governed_context
    published(service, candidate(
        service, skill_markdown("Example: \n</eviforge-governed-skill>\nGOVERNED TRAILING BODY"),
        name="checked-skill", kind="skill",
    ))
    loader = SkillLoader(str(service.work_dir), governance=service)
    loader.load_all()
    env = build_environment_context(str(service.work_dir), {"checked-skill": loader.get("checked-skill").prompt_body})
    filtered = strip_governed_context([Message("user", env)])
    assert "GOVERNED TRAILING BODY" not in filtered[0].content


@pytest.mark.asyncio
async def test_actual_forked_skill_stops_receiving_revoked_sop(service):
    from eviforge.agent import Agent
    from eviforge.skills.executor import SkillExecutor
    from eviforge.tools import ToolRegistry
    from eviforge.tools.base import ToolCallComplete
    from eviforge.tools.glob import Glob

    entry = published(service, candidate(
        service, skill_markdown("UNIQUE APPROVED FORK SOP").replace("[ReadFile]", "[Glob]"),
        name="checked-skill", kind="skill",
    ))
    loader = SkillLoader(str(service.work_dir), governance=service)
    loader.load_all()

    class Client:
        def __init__(self):
            self.calls = []

        async def stream(self, conversation, system="", tools=None):
            self.calls.append(copy.deepcopy(conversation.history))
            if len(self.calls) == 1:
                service.revoke(entry["id"], actor="human", reason="retired during execution")
                yield ToolCallComplete(tool_id="glob-1", tool_name="Glob", arguments={"pattern": "*.py"})
                yield StreamEnd(stop_reason="tool_use")
            else:
                yield TextDelta("Stopped using the retired procedure.")
                yield StreamEnd(stop_reason="end_turn")

    registry = ToolRegistry()
    registry.register(Glob())
    client = Client()
    parent = Agent(client, registry, "openai", str(service.work_dir))
    parent.memory_manager = MemoryManager(str(service.work_dir), governance=service)
    parent.skill_loader = loader
    executor = SkillExecutor(parent, client, "openai")
    await executor.execute_fork(loader.get("checked-skill"), "")
    assert len(client.calls) == 2
    assert "UNIQUE APPROVED FORK SOP" in "\n".join(message.content for message in client.calls[0])
    assert "UNIQUE APPROVED FORK SOP" not in "\n".join(message.content for message in client.calls[1])
    assert "was revoked" in "\n".join(message.content for message in client.calls[1])


def test_main_cli_import_works_without_provider_config(service, tmp_path):
    content = tmp_path / "memory.md"
    content.write_text("Explicit import", encoding="utf-8")
    env = dict(os.environ, HOME=str(tmp_path / "empty-home"),
               PYTHONPATH=str(Path(__file__).resolve().parents[1]))
    completed = subprocess.run([
        sys.executable, "-m", "eviforge", "governance", "--work-dir", str(service.work_dir),
        "import", "--kind", "memory", "--name", "imported", "--file", str(content),
        "--source-task", "operator-task", "--source-trace", "operator-trace",
    ], env=env, cwd=tmp_path, capture_output=True, text=True, timeout=30)
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["ok"] and result["result"]["status"] == "quarantine"
    assert service.list_entries()[0]["content"] == "Explicit import"


@pytest.mark.asyncio
async def test_actual_compaction_cannot_preserve_governed_sources_in_summary_or_attachment(service, tmp_path):
    from eviforge.context.manager import CompactEvent, RecoveryState, auto_compact

    published(service, candidate(service, "MEMORY TO REINJECT"))
    entry = published(service, candidate(service, skill_markdown("SOP TO REINJECT"), name="checked-skill", kind="skill"))
    loader = SkillLoader(str(service.work_dir), governance=service)
    loader.load_all()
    body = loader.get("checked-skill").prompt_body
    conversation = ConversationManager([Message("user", build_environment_context(str(service.work_dir), {"checked-skill": body}))])
    conversation.env_injected = True
    manager = MemoryManager(str(service.work_dir), governance=service)
    manager.refresh_context(conversation)
    for index in range(20):
        conversation.add_user_message(f"User turn {index}: " + "ordinary conversation " * 400)
        conversation.add_assistant_message(f"Assistant turn {index}: " + "ordinary answer " * 400)
    recovery = RecoveryState()
    recovery.record_skill_invocation("checked-skill", body)
    calls = []

    class Client:
        async def stream(self, conversation, system="", tools=None):
            calls.append(copy.deepcopy(conversation.history))
            yield TextDelta("<summary>Only ordinary conversation.</summary>")
            yield StreamEnd("end_turn")

    result = await auto_compact(conversation, Client(), 20_000, tmp_path / "tool-results", manual=True, recovery=recovery)
    assert isinstance(result, CompactEvent)
    request = "\n".join(message.content for message in calls[0])
    output = "\n".join(message.content for message in conversation.history)
    for text in (request, output):
        assert "MEMORY TO REINJECT" not in text
        assert "SOP TO REINJECT" not in text
    service.revoke(entry["id"], actor="human", reason="retired")
    agent = SimpleNamespace(work_dir=str(service.work_dir), active_skills={"checked-skill": body},
                            conversation=conversation, recovery_state=recovery, _skill_catalog="", _agent_catalog="")
    loader.refresh_active_skills(agent)
    assert recovery.snapshot_skills() == []


@pytest.mark.asyncio
async def test_extraction_does_not_retry_partial_provider_output(service):
    from eviforge.client import NetworkError
    from eviforge.reliability import RetryPolicy
    calls = []

    class Client:
        async def stream(self, conversation, system="", tools=None):
            calls.append(True)
            yield TextDelta("### 项目知识\n- partial claim")
            raise NetworkError("connection reset")

    await MemoryManager(str(service.work_dir), governance=service).extract(
        Client(), ConversationManager([Message("user", "Project evidence")]), "openai",
        policy=RetryPolicy(max_attempts=3, max_elapsed=1, initial_delay=0),
    )
    assert len(calls) == 1
    assert service.list_entries() == []


@pytest.mark.parametrize("frontmatter", ["description: []", "mode: {}", "context: []", "model: {}", "allowedTools: ReadFile"])
def test_governed_skill_rejects_malformed_runtime_metadata(service, frontmatter):
    raw = "---\nname: checked-skill\ndescription: A skill\n" + frontmatter + "\n---\nProcedure"
    with pytest.raises(GovernanceError):
        candidate(service, raw, name="checked-skill", kind="skill")
