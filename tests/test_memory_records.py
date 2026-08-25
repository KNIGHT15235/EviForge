from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from mewcode.memory.auto_memory import MemoryManager
from mewcode.memory.recall import scan_memory_files


@pytest.fixture
def manager(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MemoryManager:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    return MemoryManager(str(project))


def _hash_tree(root: Path) -> str:
    digest = hashlib.sha256()
    if not root.exists():
        return digest.hexdigest()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(str(path.relative_to(root)).encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def test_legacy_memories_are_enumerable_records(manager: MemoryManager) -> None:
    manager.user_path.parent.mkdir(parents=True)
    original = "### 用户偏好\n- prefer spaces\n\n### 纠正反馈\n- verify first\n"
    manager.user_path.write_text(original, encoding="utf-8")

    assert manager.load() == original.strip()
    records = manager.list(scope="user")
    assert len(records) == 2
    for record in records:
        assert record.record_id.startswith("mem_")
        assert record.scope == "user"
        assert record.type in {"user", "feedback"}
        assert record.source == "legacy:memories.md"
        assert record.created_at.tzinfo is not None
        assert record.updated_at.tzinfo is not None
        assert record.status == "active"
        assert record.content_hash.startswith("sha256:")
        assert manager.show(record.record_id) == record


def test_prompt_injection_budget_is_bounded_and_scope_fair(
    manager: MemoryManager,
) -> None:
    manager.user_path.parent.mkdir(parents=True)
    manager.project_path.parent.mkdir(parents=True)
    manager.user_path.write_text("U" * 200, encoding="utf-8")
    manager.project_path.write_text("P" * 200, encoding="utf-8")

    injected = manager.load(max_chars=100)

    assert len(injected) <= 100
    assert "U" in injected
    assert "P" in injected
    assert any(
        item.code == "MEMORY_INJECTION_TRUNCATED"
        for item in manager.diagnostics()
    )


def test_candidate_quarantine_metadata_and_tamper_diagnostic(
    manager: MemoryManager,
) -> None:
    manager.project_path.parent.mkdir(parents=True)
    manager.project_path.write_text("### 项目知识\n- existing fact\n", encoding="utf-8")
    candidate = manager.stage_candidate(
        "### 项目知识\n- new fact",
        source_task="task-42",
        source_trace="trace-99",
        protocol="openai",
    )
    assert candidate is not None
    assert candidate.status == "quarantine"
    assert candidate.source == "trace:trace-99"
    metadata = json.loads(candidate.metadata_path.read_text(encoding="utf-8"))
    assert metadata["source_task"] == "task-42"
    assert metadata["source_trace"] == "trace-99"
    assert metadata["content_hash"] == candidate.content_hash
    assert "new fact" not in manager.load()

    candidate.path.write_text("### 项目知识\n- tampered", encoding="utf-8")
    assert manager.promote_candidate(candidate.candidate_id) is False
    assert manager.project_path.read_text(encoding="utf-8") == "### 项目知识\n- existing fact\n"
    assert any(
        item.code == "MEMORY_CANDIDATE_HASH_MISMATCH"
        for item in manager.diagnostics()
    )


def test_promote_is_additive_traceable_and_archived(manager: MemoryManager) -> None:
    manager.project_path.parent.mkdir(parents=True)
    manager.project_path.write_text("### 项目知识\n- existing fact\n", encoding="utf-8")
    candidate = manager.stage_candidate(
        "### 项目知识\n- promoted fact", source_task="task-promote"
    )
    assert candidate is not None

    assert manager.promote_candidate(candidate.candidate_id) is True
    active = manager.project_path.read_text(encoding="utf-8")
    assert "existing fact" in active
    assert "promoted fact" in active
    assert manager.list_candidates() == []

    record = next(
        item for item in manager.list(scope="project") if item.content == "promoted fact"
    )
    assert record.storage == "structured"
    assert record.source == "task:task-promote"
    structured = record.path.read_text(encoding="utf-8")
    for field in (
        "id:", "scope:", "type:", "source:", "created_at:",
        "updated_at:", "status:", "content_hash:",
    ):
        assert field in structured

    history_root = manager.project_path.parent / "memory-history" / "candidates"
    metadata_files = list(history_root.rglob("*_promoted.json"))
    assert len(metadata_files) == 1
    assert json.loads(metadata_files[0].read_text(encoding="utf-8"))["status"] == "promoted"


def test_reject_keeps_immutable_history_without_activation(
    manager: MemoryManager,
) -> None:
    candidate = manager.stage_candidate(
        "### 项目知识\n- reject me", source_trace="trace-reject"
    )
    assert candidate is not None
    assert manager.reject_candidate(candidate.candidate_id) is True
    assert "reject me" not in manager.load()
    assert manager.list_candidates() == []
    metadata_files = list(
        (manager.project_path.parent / "memory-history" / "candidates").rglob(
            "*_rejected.json"
        )
    )
    assert len(metadata_files) == 1
    metadata = json.loads(metadata_files[0].read_text(encoding="utf-8"))
    assert metadata["status"] == "rejected"
    assert metadata["source_trace"] == "trace-reject"


def test_forget_requires_scope_match_and_preserves_other_scope(
    manager: MemoryManager,
) -> None:
    candidate = manager.stage_candidate(
        "### 用户偏好\n- keep user\n\n### 项目知识\n- forget project",
        source_task="task-mixed",
    )
    assert candidate is not None and manager.promote_candidate(candidate.candidate_id)
    project_record = next(
        item for item in manager.list(scope="project") if item.content == "forget project"
    )
    user_before = _hash_tree(manager.user_path.parent)

    assert manager.forget(
        project_record.record_id, scope="user", confirm=True
    ) is False
    assert _hash_tree(manager.user_path.parent) == user_before
    assert manager.show(project_record.record_id) is not None

    assert manager.forget(
        project_record.record_id, scope="project", confirm=True
    ) is True
    assert manager.show(project_record.record_id) is None
    assert "forget project" not in manager.load()
    assert "keep user" in manager.load()
    assert _hash_tree(manager.user_path.parent) == user_before
    history = manager.project_path.parent / "memory-history" / "records"
    assert list(history.rglob("*_forgotten.json"))


def test_clear_is_scope_isolated_and_clears_structured_records(
    manager: MemoryManager,
) -> None:
    candidate = manager.stage_candidate(
        "### 用户偏好\n- user value\n\n### 项目知识\n- project value",
        source_task="task-clear",
    )
    assert candidate is not None and manager.promote_candidate(candidate.candidate_id)
    user_before = _hash_tree(manager.user_path.parent)

    changed = manager.clear("project", confirm=True)
    assert changed == 2  # flat compatibility projection + structured record
    assert manager.list(scope="project") == []
    assert "project value" not in manager.load()
    assert "user value" in manager.load()
    assert _hash_tree(manager.user_path.parent) == user_before


def test_export_has_stable_schema_and_safe_path(manager: MemoryManager) -> None:
    manager.project_path.parent.mkdir(parents=True)
    manager.project_path.write_text("### 项目知识\n- export me\n", encoding="utf-8")
    result = manager.export(
        scope="project", format="json", filename="project-memory.json"
    )
    assert result.path.parent == manager.project_path.parent / "memory-exports"
    payload = json.loads(result.path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["scope"] == "project"
    assert payload["records"][0].keys() >= {
        "id", "scope", "type", "source", "created_at", "updated_at",
        "status", "content_hash", "content",
    }
    with pytest.raises(ValueError, match="safe basename"):
        manager.export(scope="project", filename="../escape.json")


def test_unsafe_ids_do_not_resolve_paths(manager: MemoryManager) -> None:
    assert manager.show("../../mem_deadbeef") is None
    assert manager.promote_candidate("../pending_escape") is False
    assert any(item.code.endswith("ID_INVALID") for item in manager.diagnostics())


def test_recall_rejects_tampered_or_non_active_structured_record(
    manager: MemoryManager,
) -> None:
    candidate = manager.stage_candidate(
        "### 项目知识\n- recall integrity", source_trace="trace-recall"
    )
    assert candidate is not None and manager.promote_candidate(candidate.candidate_id)
    record = next(
        item for item in manager.list(scope="project")
        if item.content == "recall integrity"
    )
    headers = scan_memory_files(manager.project_mem_dir, "project")
    assert len(headers) == 1
    assert headers[0].record_id == record.record_id
    assert headers[0].source == "trace:trace-recall"

    record.path.write_text(
        record.path.read_text(encoding="utf-8").replace(
            "recall integrity", "tampered content"
        ),
        encoding="utf-8",
    )
    assert scan_memory_files(manager.project_mem_dir, "project") == []
