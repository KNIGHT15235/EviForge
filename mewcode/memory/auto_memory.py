from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from mewcode.conversation import ConversationManager, Message

USER_MEMORIES_RELPATH = ".mewcode/memories.md"
PROJECT_MEMORIES_RELPATH = ".mewcode/memories.md"
MEMORY_INJECTION_CHAR_BUDGET = 16_000

MEMORY_EXTRACTION_PROMPT = """\
你是一个记忆提取助手。分析下面的对话，提取值得长期记忆的信息，输出待审核的 memories.md 候选内容。

分类规则：
- **用户偏好**：用户的编码习惯和风格要求（如缩进、命名规范、语言偏好）
- **纠正反馈**：用户明确指出的错误和正确做法
- **项目知识**：当前项目的具体技术信息（技术栈、目录结构、部署方式）
- **参考资料**：外部链接和文档地址

规则：
1. 已有相同含义的条目不要重复添加
2. 没有值得记忆的内容，该分类下留空（不要写任何条目，不要写占位符）
3. 每条记忆用一行 `- ` 开头，必须是具体内容，不要用 `...` 占位
4. 只输出本轮新增的候选条目，不要复制、重写或删除“当前 memories.md”中的已激活条目
5. 你只能生成候选，不能修改或删除已激活记忆

输出格式（严格遵守，没有内容的分类下不写任何条目）：
### 用户偏好
- 用户偏好简洁代码风格

### 纠正反馈

### 项目知识
- 项目使用 PostgreSQL 15

### 参考资料

不要输出任何其他内容，不要调用任何工具。"""

_SECTION_INFO = {
    "用户偏好": ("user", "user"),
    "纠正反馈": ("user", "feedback"),
    "项目知识": ("project", "project"),
    "参考资料": ("project", "reference"),
}
_TYPE_HEADER = {value[1]: key for key, value in _SECTION_INFO.items()}
_VALID_SCOPES = {"user", "project"}
_VALID_TYPES = {"user", "feedback", "project", "reference", "legacy"}
_VALID_STATUSES = {
    "active", "quarantine", "promoted", "rejected", "forgotten",
    "expired", "conflict", "failed",
}
_SAFE_RECORD_ID = re.compile(r"\Amem_[a-f0-9]{20}\Z")
_SAFE_CANDIDATE_ID = re.compile(r"\Apending_[0-9A-Za-z_-]{8,96}\Z")

log = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_time(raw: str | None, fallback: datetime) -> datetime:
    if not raw:
        return fallback
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(
            timezone.utc
        )
    except (TypeError, ValueError):
        return fallback


def _normalize_content(content: str) -> str:
    return "\n".join(line.rstrip() for line in content.strip().splitlines()).strip()


def _sha256(content: str) -> str:
    return "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()


def _record_id(scope: str, memory_type: str, content_hash: str) -> str:
    raw = f"{scope}\0{memory_type}\0{content_hash}".encode("utf-8")
    return "mem_" + hashlib.sha256(raw).hexdigest()[:20]


@dataclass(frozen=True, slots=True)
class MemoryRecord:
    """One traceable record from structured storage or legacy projection."""

    record_id: str
    scope: str
    type: str
    source: str
    created_at: datetime
    updated_at: datetime
    status: str
    content_hash: str
    content: str
    storage: str
    path: Path
    size_bytes: int

    def to_dict(self, *, include_path: bool = False) -> dict[str, Any]:
        value: dict[str, Any] = {
            "id": self.record_id,
            "scope": self.scope,
            "type": self.type,
            "source": self.source,
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
            "status": self.status,
            "content_hash": self.content_hash,
            "content": self.content,
            "storage": self.storage,
            "size_bytes": self.size_bytes,
        }
        if include_path:
            value["path"] = str(self.path)
        return value


@dataclass(frozen=True, slots=True)
class MemoryCandidate:
    candidate_id: str
    scope: str
    type: str
    source: str
    created_at: datetime
    updated_at: datetime
    status: str
    content_hash: str
    path: Path
    metadata_path: Path
    size_bytes: int
    source_task: str = ""
    source_trace: str = ""
    protocol: str = ""


@dataclass(frozen=True, slots=True)
class MemoryDiagnostic:
    code: str
    message: str
    path: str = ""
    created_at: datetime = field(default_factory=_now)


@dataclass(frozen=True, slots=True)
class MemoryExport:
    path: Path
    scope: str
    format: str
    record_count: int
    content_hash: str


class MemoryManager:
    """Review-gated Memory lifecycle with old ``memories.md`` compatibility."""

    def __init__(self, project_root: str) -> None:
        self._project_root = Path(project_root).expanduser().resolve(strict=False)
        self._user_path = Path.home() / USER_MEMORIES_RELPATH
        self._project_path = self._project_root / PROJECT_MEMORIES_RELPATH
        self._last_extraction_msg_count = 0
        self._candidate_dir = self._project_path.parent / "memory-candidates"
        self._history_dir = self._project_path.parent / "memory-history"
        self._export_dir = self._project_path.parent / "memory-exports"
        self.last_error = ""
        self._diagnostics: list[MemoryDiagnostic] = []
        self._diagnostic_keys: set[tuple[str, str, str]] = set()

    @property
    def user_path(self) -> Path:
        return self._user_path

    @property
    def project_path(self) -> Path:
        return self._project_path

    @property
    def user_mem_dir(self) -> Path:
        return Path.home() / ".mewcode" / "memory"

    @property
    def project_mem_dir(self) -> Path:
        return self._project_path.parent / "memory"

    def diagnostics(self) -> list[MemoryDiagnostic]:
        return list(self._diagnostics)

    def _diagnose(self, code: str, message: str, path: Path | str = "") -> None:
        path_text = str(path) if path else ""
        key = (code, message, path_text)
        if key in self._diagnostic_keys:
            return
        self._diagnostic_keys.add(key)
        self._diagnostics.append(MemoryDiagnostic(code, message, path_text))
        self.last_error = f"{code}: {message}"
        log.warning("Memory diagnostic %s: %s (%s)", code, message, path_text)

    def record_diagnostic(
        self, code: str, message: str, path: Path | str = ""
    ) -> None:
        """Record a redacted operator-visible degradation from an outer layer."""

        self._diagnose(code, message, path)

    @staticmethod
    def _validate_scope(scope: str) -> None:
        if scope not in _VALID_SCOPES:
            raise ValueError("memory scope must be user or project")

    @staticmethod
    def _within(path: Path, root: Path) -> bool:
        try:
            path.resolve(strict=False).relative_to(root.resolve(strict=False))
            return True
        except (OSError, ValueError):
            return False

    def load(self, *, max_chars: int = MEMORY_INJECTION_CHAR_BUDGET) -> str:
        """Load a bounded legacy projection for prompt injection.

        User and project scopes receive an equal share when the combined
        projection exceeds the budget, preventing one scope from starving the
        other or causing unbounded context growth.
        """
        if max_chars < 0:
            raise ValueError("memory injection budget must be non-negative")
        sections: list[str] = []
        for path in (self._user_path, self._project_path):
            try:
                content = path.read_text(encoding="utf-8").strip() if path.is_file() else ""
            except OSError as exc:
                self._diagnose("MEMORY_READ_FAILED", str(exc), path)
                continue
            if content:
                sections.append(content)
        combined = "\n\n".join(sections)
        if len(combined) <= max_chars:
            return combined
        if max_chars == 0:
            self._diagnose(
                "MEMORY_INJECTION_TRUNCATED",
                f"active projection omitted by {max_chars}-character budget",
            )
            return ""
        share = max(1, (max_chars - max(0, len(sections) - 1) * 2) // len(sections))
        bounded = [section[:share] for section in sections]
        result = "\n\n".join(bounded)[:max_chars]
        self._diagnose(
            "MEMORY_INJECTION_TRUNCATED",
            f"active projection truncated from {len(combined)} to {len(result)} characters",
        )
        return result

    async def extract(
        self,
        client: Any,
        conversation: ConversationManager,
        protocol: str,
        *,
        source_task: str | None = None,
        source_trace: str | None = None,
    ) -> None:
        from mewcode.tools.base import StreamEnd, TextDelta

        recent = conversation.history[self._last_extraction_msg_count :]
        if not recent:
            return
        conv_lines: list[str] = []
        for msg in recent:
            if msg.role == "user" and msg.content:
                conv_lines.append(f"用户: {msg.content}")
            elif msg.role == "assistant" and msg.content:
                conv_lines.append(f"助手: {msg.content}")
        if not conv_lines:
            return
        current = self.load()
        prompt = (
            f"{MEMORY_EXTRACTION_PROMPT}\n\n## 当前 memories.md\n"
            f"{current if current else '(空)'}\n\n## 最近对话\n"
            f"{chr(10).join(conv_lines)}\n\n请只输出本轮新增的候选内容。"
        )
        extract_conv = ConversationManager()
        extract_conv.history = [Message(role="user", content=prompt)]
        collected = ""
        try:
            async for event in client.stream(
                extract_conv, system="你是一个记忆提取助手。"
            ):
                if isinstance(event, TextDelta):
                    collected += event.text
                elif isinstance(event, StreamEnd):
                    pass
        except Exception as exc:
            self._diagnose(
                "MEMORY_EXTRACTION_FAILED", f"{type(exc).__name__}: {exc}"
            )
            return
        self._last_extraction_msg_count = len(conversation.history)
        collected = collected.strip()
        if not collected:
            self._diagnose("MEMORY_EXTRACTION_EMPTY", "model returned no candidate")
            return
        if source_task is None and source_trace is None:
            material = "\n".join(conv_lines).encode("utf-8")
            source_task = "conversation-" + hashlib.sha256(material).hexdigest()[:16]
        self._stage_candidate(
            collected,
            source_task=source_task,
            source_trace=source_trace,
            protocol=protocol,
        )

    def stage_candidate(
        self,
        content: str,
        *,
        source_task: str | None = None,
        source_trace: str | None = None,
        protocol: str = "",
    ) -> MemoryCandidate | None:
        return self._stage_candidate(
            content,
            source_task=source_task,
            source_trace=source_trace,
            protocol=protocol,
        )

    def _stage_candidate(
        self,
        content: str,
        *,
        source_task: str | None = None,
        source_trace: str | None = None,
        protocol: str = "",
    ) -> MemoryCandidate | None:
        """Put model output in quarantine; active memory is never overwritten."""
        normalized = _normalize_content(content)
        items = self._parse_candidate_items(normalized)
        if not normalized or not items:
            self._diagnose(
                "MEMORY_CANDIDATE_INVALID",
                "candidate contains no supported, non-placeholder entries",
            )
            return None
        now = _now()
        content_hash = _sha256(normalized)
        candidate_id = (
            f"pending_{now.strftime('%Y%m%dT%H%M%S%fZ')}_{content_hash[7:17]}"
        )
        source_task = (source_task or "").strip()
        source_trace = (source_trace or "").strip()
        source = (
            f"trace:{source_trace}"
            if source_trace
            else f"task:{source_task or 'manual-review'}"
        )
        scopes = sorted({scope for scope, _, _ in items})
        types = sorted({memory_type for _, memory_type, _ in items})
        metadata = {
            "schema_version": 1,
            "id": candidate_id,
            "scope": scopes[0] if len(scopes) == 1 else "mixed",
            "type": types[0] if len(types) == 1 else "mixed",
            "source": source,
            "source_task": source_task,
            "source_trace": source_trace,
            "protocol": protocol,
            "created_at": _iso(now),
            "updated_at": _iso(now),
            "status": "quarantine",
            "content_hash": content_hash,
            "entry_count": len(items),
        }
        self._candidate_dir.mkdir(parents=True, exist_ok=True)
        path = self._candidate_dir / f"{candidate_id}.md"
        meta_path = self._candidate_dir / f"{candidate_id}.json"
        try:
            self._atomic_write(path, normalized + "\n")
            self._atomic_write(
                meta_path, json.dumps(metadata, ensure_ascii=False, indent=2) + "\n"
            )
        except OSError as exc:
            self._diagnose("MEMORY_CANDIDATE_WRITE_FAILED", str(exc), path)
            path.unlink(missing_ok=True)
            meta_path.unlink(missing_ok=True)
            return None
        return self._candidate_from_files(path, meta_path)

    def _atomic_write(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if not self._within(path, path.parent):
            raise OSError("unsafe write target")
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            with tmp.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            tmp.replace(path)
        finally:
            tmp.unlink(missing_ok=True)

    def _candidate_from_files(
        self, path: Path, metadata_path: Path
    ) -> MemoryCandidate | None:
        if (
            not self._within(path, self._candidate_dir)
            or path.is_symlink()
            or not self._within(metadata_path, self._candidate_dir)
            or metadata_path.is_symlink()
        ):
            self._diagnose("MEMORY_PATH_UNSAFE", "candidate escapes quarantine", path)
            return None
        try:
            content = _normalize_content(path.read_text(encoding="utf-8"))
            stat = path.stat()
            metadata = (
                json.loads(metadata_path.read_text(encoding="utf-8"))
                if metadata_path.is_file()
                else {}
            )
            if not isinstance(metadata, dict):
                raise ValueError("candidate metadata must be an object")
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            self._diagnose("MEMORY_CANDIDATE_READ_FAILED", str(exc), path)
            return None
        actual_hash = _sha256(content)
        status = str(metadata.get("status", "quarantine"))
        if str(metadata.get("content_hash", actual_hash)) != actual_hash:
            status = "conflict"
            self._diagnose(
                "MEMORY_CANDIDATE_HASH_MISMATCH",
                "candidate no longer matches quarantine metadata",
                path,
            )
        fallback = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
        return MemoryCandidate(
            candidate_id=path.stem,
            scope=str(metadata.get("scope", "mixed")),
            type=str(metadata.get("type", "mixed")),
            source=str(metadata.get("source", "task:legacy-quarantine")),
            created_at=_parse_time(str(metadata.get("created_at", "")), fallback),
            updated_at=_parse_time(str(metadata.get("updated_at", "")), fallback),
            status=status,
            content_hash=actual_hash,
            path=path,
            metadata_path=metadata_path,
            size_bytes=stat.st_size,
            source_task=str(metadata.get("source_task", "")),
            source_trace=str(metadata.get("source_trace", "")),
            protocol=str(metadata.get("protocol", "")),
        )

    def list_candidates(self) -> list[MemoryCandidate]:
        if not self._candidate_dir.is_dir():
            return []
        result: list[MemoryCandidate] = []
        try:
            paths = sorted(self._candidate_dir.glob("pending_*.md"))
        except OSError as exc:
            self._diagnose("MEMORY_CANDIDATE_SCAN_FAILED", str(exc), self._candidate_dir)
            return result
        for path in paths:
            if not _SAFE_CANDIDATE_ID.fullmatch(path.stem):
                self._diagnose("MEMORY_CANDIDATE_ID_INVALID", "unsafe candidate id", path)
                continue
            item = self._candidate_from_files(
                path, self._candidate_dir / f"{path.stem}.json"
            )
            if item is not None:
                result.append(item)
        return result

    def _candidate_path(self, candidate_id: str) -> Path | None:
        if not _SAFE_CANDIDATE_ID.fullmatch(candidate_id):
            self._diagnose("MEMORY_CANDIDATE_ID_INVALID", "unsafe candidate id")
            return None
        path = self._candidate_dir / f"{candidate_id}.md"
        return path if self._within(path, self._candidate_dir) else None

    def _get_pending_candidate(self, candidate_id: str) -> MemoryCandidate | None:
        path = self._candidate_path(candidate_id)
        if path is None or not path.is_file():
            return None
        return self._candidate_from_files(
            path, self._candidate_dir / f"{candidate_id}.json"
        )

    def promote_candidate(self, candidate_id: str) -> bool:
        candidate = self._get_pending_candidate(candidate_id)
        if candidate is None:
            return False
        if candidate.status != "quarantine":
            self._diagnose(
                "MEMORY_CANDIDATE_CONFLICT",
                "candidate failed integrity verification",
                candidate.path,
            )
            return False
        try:
            items = self._parse_candidate_items(
                _normalize_content(candidate.path.read_text(encoding="utf-8"))
            )
            if not items:
                raise ValueError("candidate has no promotable entries")
            created = [
                self._create_structured_record(
                    scope=scope,
                    memory_type=memory_type,
                    content=content,
                    source=candidate.source,
                    created_at=candidate.created_at,
                )
                for scope, memory_type, content in items
            ]
            # Additive projection: a model can never remove/replace active memory.
            self._merge_legacy_projection(created)
            self._archive_candidate(candidate, "promoted")
            self._remove_pending_candidate(candidate)
            return True
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            self._diagnose("MEMORY_PROMOTE_FAILED", str(exc), candidate.path)
            return False

    def reject_candidate(self, candidate_id: str) -> bool:
        candidate = self._get_pending_candidate(candidate_id)
        if candidate is None:
            return False
        try:
            self._archive_candidate(candidate, "rejected")
            self._remove_pending_candidate(candidate)
            return True
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            self._diagnose("MEMORY_REJECT_FAILED", str(exc), candidate.path)
            return False

    def _archive_candidate(self, candidate: MemoryCandidate, status: str) -> None:
        if status not in {"promoted", "rejected"}:
            raise ValueError("invalid candidate terminal status")
        root = self._history_dir / "candidates" / candidate.candidate_id
        root.mkdir(parents=True, exist_ok=True)
        stamp = _now().strftime("%Y%m%dT%H%M%S%fZ")
        metadata: dict[str, Any] = {}
        if candidate.metadata_path.is_file():
            metadata = json.loads(candidate.metadata_path.read_text(encoding="utf-8"))
        metadata.update({"status": status, "updated_at": _iso(_now())})
        self._write_immutable(
            root / f"{stamp}_{status}.md", candidate.path.read_bytes()
        )
        self._write_immutable(
            root / f"{stamp}_{status}.json",
            (json.dumps(metadata, ensure_ascii=False, indent=2) + "\n").encode(
                "utf-8"
            ),
        )

    @staticmethod
    def _write_immutable(path: Path, payload: bytes) -> None:
        with path.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if os.name != "nt":
            try:
                path.chmod(0o444)
            except OSError:
                pass

    @staticmethod
    def _remove_pending_candidate(candidate: MemoryCandidate) -> None:
        candidate.path.unlink()
        candidate.metadata_path.unlink(missing_ok=True)

    @staticmethod
    def _parse_candidate_items(content: str) -> list[tuple[str, str, str]]:
        items: list[tuple[str, str, str]] = []
        section: tuple[str, str] | None = None
        for raw_line in content.splitlines():
            line = raw_line.strip()
            if line.startswith("### "):
                header = line[4:].strip()
                section = next(
                    (value for key, value in _SECTION_INFO.items() if key in header),
                    None,
                )
                continue
            if section is None or not line.startswith("- "):
                continue
            item = line[2:].strip()
            if item and not MemoryManager._is_placeholder(item):
                items.append((section[0], section[1], item))
        return list(dict.fromkeys(items))

    def _create_structured_record(
        self,
        *,
        scope: str,
        memory_type: str,
        content: str,
        source: str,
        created_at: datetime,
    ) -> MemoryRecord:
        self._validate_scope(scope)
        if memory_type not in _VALID_TYPES - {"legacy"}:
            raise ValueError("unsupported memory type")
        normalized = _normalize_content(content)
        content_hash = _sha256(normalized)
        record_id = _record_id(scope, memory_type, content_hash)
        directory = self.user_mem_dir if scope == "user" else self.project_mem_dir
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{record_id}.md"
        if not self._within(path, directory):
            raise OSError("structured memory path escapes scope root")
        if path.exists():
            existing = self._record_for_directory(scope, directory, path)
            if existing is None or existing.content_hash != content_hash:
                raise OSError("immutable memory id collision")
            return existing
        now = _now()
        metadata = {
            "id": record_id,
            "scope": scope,
            "type": memory_type,
            "source": source,
            "created_at": _iso(created_at),
            "updated_at": _iso(now),
            "status": "active",
            "content_hash": content_hash,
        }
        lines = ["---"] + [
            f"{key}: {json.dumps(value, ensure_ascii=False)}"
            for key, value in metadata.items()
        ] + ["---", "", normalized, ""]
        self._atomic_write(path, "\n".join(lines))
        record = self._record_for_directory(scope, directory, path)
        if record is None:
            raise OSError("structured memory unreadable after write")
        return record

    def _merge_legacy_projection(self, records: Iterable[MemoryRecord]) -> None:
        grouped: dict[str, list[MemoryRecord]] = {"user": [], "project": []}
        for record in records:
            grouped[record.scope].append(record)
        for scope, scoped in grouped.items():
            if not scoped:
                continue
            path = self._user_path if scope == "user" else self._project_path
            original = path.read_text(encoding="utf-8") if path.is_file() else ""
            updated = original
            for record in scoped:
                updated = self._append_projection_entry(
                    updated, _TYPE_HEADER[record.type], record.content
                )
            if updated != original:
                path.parent.mkdir(parents=True, exist_ok=True)
                self._backup(path, scope, action="projection-before-promote")
                self._atomic_write(path, updated.rstrip() + "\n")

    @staticmethod
    def _append_projection_entry(text: str, header: str, content: str) -> str:
        bullet = f"- {content}"
        lines = text.splitlines()
        current = ""
        for line in lines:
            if line.startswith("### "):
                current = line[4:].strip()
            elif current == header and line.strip() == bullet:
                return text
        target = f"### {header}"
        if target not in lines:
            prefix = text.rstrip()
            return (prefix + "\n\n" if prefix else "") + target + "\n" + bullet + "\n"
        index = lines.index(target) + 1
        while index < len(lines) and not lines[index].startswith("### "):
            index += 1
        while index > 0 and index <= len(lines) and not lines[index - 1].strip():
            index -= 1
        lines.insert(index, bullet)
        return "\n".join(lines) + ("\n" if text.endswith("\n") else "")

    def _write_memories(self, content: str) -> None:
        """Compatibility helper for explicit trusted writes (not extraction)."""
        grouped: dict[str, list[tuple[str, str]]] = {"user": [], "project": []}
        for scope, memory_type, item in self._parse_candidate_items(content):
            grouped[scope].append((memory_type, item))
        for scope, items in grouped.items():
            if not items:
                continue
            path = self._user_path if scope == "user" else self._project_path
            sections: list[str] = []
            for memory_type in ("user", "feedback", "project", "reference"):
                typed = [item for item_type, item in items if item_type == memory_type]
                if typed:
                    sections.append(
                        f"### {_TYPE_HEADER[memory_type]}\n"
                        + "\n".join(f"- {item}" for item in typed)
                    )
            path.parent.mkdir(parents=True, exist_ok=True)
            self._backup(path, scope, action="before-explicit-write")
            self._atomic_write(path, "\n\n".join(sections).strip() + "\n")

    @staticmethod
    def _is_placeholder(line: str) -> bool:
        stripped = line.strip().removeprefix("- ").strip()
        return stripped in {"", "...", "…", "无", "暂无", "N/A"}

    def _history_dir_for(self, scope: str) -> Path:
        self._validate_scope(scope)
        if scope == "user":
            return Path.home() / ".mewcode" / "memory-history"
        return self._history_dir

    def _backup(self, path: Path, scope: str, *, action: str = "backup") -> Path | None:
        if not path.is_file():
            return None
        payload = path.read_bytes()
        if not payload.strip():
            return None
        history = self._history_dir_for(scope) / "active"
        history.mkdir(parents=True, exist_ok=True)
        stamp = _now().strftime("%Y%m%dT%H%M%S%fZ")
        digest = hashlib.sha256(payload).hexdigest()[:12]
        backup = history / f"{stamp}_{action}_{digest}_{path.name}"
        self._write_immutable(backup, payload)
        return backup

    def list(
        self, *, scope: str | None = None, status: str | None = None
    ) -> list[MemoryRecord]:
        if scope is not None:
            self._validate_scope(scope)
        if status is not None and status not in _VALID_STATUSES:
            raise ValueError("invalid memory status")
        records: list[MemoryRecord] = []
        structured: set[tuple[str, str, str]] = set()
        scopes = (scope,) if scope else ("user", "project")
        for current_scope in scopes:
            directory = (
                self.user_mem_dir if current_scope == "user" else self.project_mem_dir
            )
            for record in self._directory_records(current_scope, directory):
                records.append(record)
                structured.add((record.scope, record.type, record.content_hash))
        for current_scope in scopes:
            path = self._user_path if current_scope == "user" else self._project_path
            for record in self._legacy_records(current_scope, path):
                if (record.scope, record.type, record.content_hash) not in structured:
                    records.append(record)
        if status is not None:
            records = [item for item in records if item.status == status]
        return sorted(records, key=lambda item: (item.updated_at, item.record_id), reverse=True)

    def inventory(self) -> list[MemoryRecord]:
        return self.list()

    def show(self, record_id: str) -> MemoryRecord | None:
        if not _SAFE_RECORD_ID.fullmatch(record_id):
            self._diagnose("MEMORY_RECORD_ID_INVALID", "unsafe memory record id")
            return None
        return next((item for item in self.list() if item.record_id == record_id), None)

    def _directory_records(self, scope: str, directory: Path) -> list[MemoryRecord]:
        if not directory.is_dir():
            return []
        try:
            paths = sorted(directory.rglob("*.md"))
        except OSError as exc:
            self._diagnose("MEMORY_SCAN_FAILED", str(exc), directory)
            return []
        records: list[MemoryRecord] = []
        for path in paths:
            item = self._record_for_directory(scope, directory, path)
            if item is not None:
                records.append(item)
        return records

    def _record_for_directory(
        self, scope: str, directory: Path, path: Path
    ) -> MemoryRecord | None:
        if path.is_symlink() or not self._within(path, directory):
            self._diagnose("MEMORY_PATH_UNSAFE", "memory escapes scope root", path)
            return None
        try:
            raw = path.read_text(encoding="utf-8")
            stat = path.stat()
        except (OSError, UnicodeError) as exc:
            self._diagnose("MEMORY_READ_FAILED", str(exc), path)
            return None
        metadata, body = self._parse_record_document(raw)
        content = _normalize_content(body)
        if not content:
            self._diagnose("MEMORY_RECORD_EMPTY", "record has no content", path)
            return None
        fallback = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
        memory_type = metadata.get("type", "legacy")
        if memory_type not in _VALID_TYPES:
            self._diagnose("MEMORY_TYPE_INVALID", "unsupported memory type", path)
            memory_type = "legacy"
        if metadata.get("scope", scope) != scope:
            self._diagnose(
                "MEMORY_SCOPE_CONFLICT", "metadata disagrees with owning scope", path
            )
        content_hash = _sha256(content)
        status = metadata.get("status", "active")
        if status not in _VALID_STATUSES:
            self._diagnose("MEMORY_STATUS_INVALID", "unsupported status", path)
            status = "conflict"
        if metadata.get("content_hash", content_hash) != content_hash:
            self._diagnose(
                "MEMORY_RECORD_HASH_MISMATCH", "content hash mismatch", path
            )
            status = "conflict"
        expected_id = _record_id(scope, memory_type, content_hash)
        if metadata.get("id", expected_id) != expected_id:
            self._diagnose("MEMORY_RECORD_ID_MISMATCH", "immutable id mismatch", path)
            status = "conflict"
        return MemoryRecord(
            record_id=expected_id,
            scope=scope,
            type=memory_type,
            source=metadata.get("source", "") or "legacy:memory-file",
            created_at=_parse_time(metadata.get("created_at"), fallback),
            updated_at=_parse_time(metadata.get("updated_at"), fallback),
            status=status,
            content_hash=content_hash,
            content=content,
            storage="structured",
            path=path,
            size_bytes=stat.st_size,
        )

    def _legacy_records(self, scope: str, path: Path) -> list[MemoryRecord]:
        if not path.is_file():
            return []
        try:
            raw = path.read_text(encoding="utf-8")
            stat = path.stat()
        except (OSError, UnicodeError) as exc:
            self._diagnose("MEMORY_READ_FAILED", str(exc), path)
            return []
        timestamp = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
        records: list[MemoryRecord] = []
        for item_scope, memory_type, content in self._parse_candidate_items(raw):
            if item_scope != scope:
                self._diagnose(
                    "MEMORY_SCOPE_CONFLICT", "legacy section in wrong scope", path
                )
                continue
            content_hash = _sha256(content)
            records.append(
                MemoryRecord(
                    _record_id(scope, memory_type, content_hash), scope, memory_type,
                    "legacy:memories.md", timestamp, timestamp, "active", content_hash,
                    content, "legacy-flat", path, len(content.encode("utf-8")),
                )
            )
        if not records and raw.strip():
            content = _normalize_content(raw)
            content_hash = _sha256(content)
            records.append(
                MemoryRecord(
                    _record_id(scope, "legacy", content_hash), scope, "legacy",
                    "legacy:memories.md", timestamp, timestamp, "active", content_hash,
                    content, "legacy-flat", path, stat.st_size,
                )
            )
        return records

    @staticmethod
    def _parse_record_document(raw: str) -> tuple[dict[str, str], str]:
        if not raw.startswith("---\n"):
            return {}, raw
        end = raw.find("\n---\n", 4)
        if end < 0:
            return {}, raw
        metadata: dict[str, str] = {}
        accepted = {
            "id", "scope", "type", "source", "created_at", "updated_at",
            "status", "content_hash",
        }
        for line in raw[4:end].splitlines():
            key, separator, value = line.partition(":")
            if not separator or key.strip() not in accepted:
                continue
            key = key.strip()
            value = value.strip()
            try:
                decoded = json.loads(value)
                if isinstance(decoded, str):
                    value = decoded
            except json.JSONDecodeError:
                value = value.strip("\"'")
            metadata[key] = value
        return metadata, raw[end + 5 :]

    def forget(
        self,
        record_id: str,
        *,
        confirm: bool = False,
        scope: str | None = None,
    ) -> bool:
        if not confirm:
            raise PermissionError("memory forget requires explicit confirmation")
        if scope is not None:
            self._validate_scope(scope)
        record = self.show(record_id)
        if record is None:
            return False
        if scope is not None and record.scope != scope:
            self._diagnose("MEMORY_SCOPE_MISMATCH", "record belongs to another scope")
            return False
        try:
            if record.storage == "structured":
                directory = (
                    self.user_mem_dir if record.scope == "user" else self.project_mem_dir
                )
                if record.path.is_symlink() or not self._within(record.path, directory):
                    raise OSError("unsafe structured memory target")
                self._archive_record(record, "forgotten")
                record.path.unlink()
                self._remove_projection_entry(record)
            else:
                self._remove_legacy_record(record)
            return True
        except OSError as exc:
            self._diagnose("MEMORY_FORGET_FAILED", str(exc), record.path)
            return False

    def _archive_record(self, record: MemoryRecord, action: str) -> None:
        history = self._history_dir_for(record.scope) / "records" / record.record_id
        history.mkdir(parents=True, exist_ok=True)
        stamp = _now().strftime("%Y%m%dT%H%M%S%fZ")
        self._write_immutable(history / f"{stamp}_{action}.md", record.path.read_bytes())
        metadata = record.to_dict()
        metadata.update({"status": action, "updated_at": _iso(_now())})
        self._write_immutable(
            history / f"{stamp}_{action}.json",
            (json.dumps(metadata, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        )

    def _remove_projection_entry(self, record: MemoryRecord) -> None:
        flat = self._user_path if record.scope == "user" else self._project_path
        for legacy in self._legacy_records(record.scope, flat):
            if legacy.content_hash == record.content_hash and legacy.type == record.type:
                self._remove_legacy_record(legacy)
                return

    def _remove_legacy_record(self, record: MemoryRecord) -> None:
        expected = self._user_path if record.scope == "user" else self._project_path
        if record.path.resolve(strict=False) != expected.resolve(strict=False):
            raise OSError("legacy memory path does not match scope")
        original = record.path.read_text(encoding="utf-8")
        self._backup(record.path, record.scope, action="before-forget")
        updated = (
            "" if record.type == "legacy"
            else self._remove_bullet(original, _TYPE_HEADER[record.type], record.content)
        )
        self._atomic_write(record.path, updated)

    @staticmethod
    def _remove_bullet(text: str, header: str, content: str) -> str:
        current = ""
        removed = False
        output: list[str] = []
        for line in text.splitlines():
            if line.startswith("### "):
                current = line[4:].strip()
            if not removed and current == header and line.strip() == f"- {content}":
                removed = True
                continue
            output.append(line)
        return "\n".join(output).rstrip() + ("\n" if output else "")

    def clear(
        self,
        scope: str,
        *,
        confirm: bool = False,
        include_directory: bool = True,
    ) -> int:
        """Clear exactly one active scope after explicit confirmation."""
        self._validate_scope(scope)
        if not confirm:
            raise PermissionError("memory clear requires explicit confirmation")
        path = self._user_path if scope == "user" else self._project_path
        changed = 0
        if path.is_file() and path.read_text(encoding="utf-8").strip():
            self._backup(path, scope, action="before-clear")
            self._atomic_write(path, "")
            changed += 1
        if include_directory:
            directory = self.user_mem_dir if scope == "user" else self.project_mem_dir
            for record in self._directory_records(scope, directory):
                self._archive_record(record, "forgotten")
                record.path.unlink()
                changed += 1
        return changed

    def export(
        self,
        *,
        scope: str | None = None,
        format: str = "json",
        filename: str | None = None,
    ) -> MemoryExport:
        if scope is not None:
            self._validate_scope(scope)
        if format not in {"json", "markdown"}:
            raise ValueError("memory export format must be json or markdown")
        records = self.list(scope=scope)
        exported_scope = scope or "all"
        suffix = "json" if format == "json" else "md"
        if filename is None:
            stamp = _now().strftime("%Y%m%dT%H%M%SZ")
            filename = f"memory-{exported_scope}-{stamp}.{suffix}"
        if Path(filename).name != filename or not re.fullmatch(
            r"[0-9A-Za-z._-]{1,128}", filename
        ):
            raise ValueError("memory export filename must be a safe basename")
        if not filename.endswith(f".{suffix}"):
            raise ValueError(f"memory export filename must end with .{suffix}")
        if format == "json":
            output = json.dumps(
                {
                    "schema_version": 1,
                    "scope": exported_scope,
                    "exported_at": _iso(_now()),
                    "records": [record.to_dict() for record in records],
                    "diagnostics": [
                        {
                            "code": item.code,
                            "message": item.message,
                            "created_at": _iso(item.created_at),
                        }
                        for item in self.diagnostics()
                    ],
                },
                ensure_ascii=False,
                indent=2,
            ) + "\n"
        else:
            lines = [f"# EviForge Memory Export ({exported_scope})", ""]
            for record in records:
                lines += [
                    f"## {record.record_id}", "",
                    f"- scope: `{record.scope}`", f"- type: `{record.type}`",
                    f"- source: `{record.source}`", f"- status: `{record.status}`",
                    f"- created_at: `{_iso(record.created_at)}`",
                    f"- updated_at: `{_iso(record.updated_at)}`",
                    f"- content_hash: `{record.content_hash}`", "", record.content, "",
                ]
            output = "\n".join(lines).rstrip() + "\n"
        self._export_dir.mkdir(parents=True, exist_ok=True)
        path = self._export_dir / filename
        if not self._within(path, self._export_dir):
            raise ValueError("memory export path escapes export directory")
        self._atomic_write(path, output)
        return MemoryExport(
            path, exported_scope, format, len(records), _sha256(output)
        )

    def get_display_text(self) -> str:
        parts: list[str] = []
        for label, path in (("用户级", self._user_path), ("项目级", self._project_path)):
            try:
                content = path.read_text(encoding="utf-8").strip() if path.is_file() else ""
            except OSError as exc:
                self._diagnose("MEMORY_READ_FAILED", str(exc), path)
                continue
            if content:
                parts.append(f"[{label}] {path}\n{content}")
        return "\n\n".join(parts) if parts else "当前没有任何自动记忆。"

    def get_inventory_text(self, *, scope: str | None = None) -> str:
        records = self.list(scope=scope)
        if not records:
            return "当前没有任何自动记忆。"
        lines = ["记忆清单："]
        for record in records:
            lines.append(
                f"  {record.record_id} scope={record.scope} type={record.type} "
                f"status={record.status} source={record.source} "
                f"hash={record.content_hash[7:19]} bytes={record.size_bytes}"
            )
        if self.last_error:
            lines.append(f"最近错误: {self.last_error}")
        return "\n".join(lines)
