from __future__ import annotations

import json
import hashlib
import random
import re
import string
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import IO, Any

from mewcode.conversation import (
    ConversationManager,
    Message,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)

SESSIONS_DIR = ".mewcode/sessions"
DEFAULT_MAX_AGE_DAYS = 30
TITLE_MAX_LENGTH = 50
TIME_GAP_NOTICE_HOURS = 24

SESSION_SUMMARY_PROMPT = (
    "你是一个对话摘要助手。请根据下面的对话内容，用一句话总结这个会话的主要内容。"
    "只输出摘要文本，不要加任何前缀或标点符号外的修饰。不要调用任何工具。"
)


def build_time_gap_message(
    last_active: datetime,
    *,
    now: datetime | None = None,
    threshold_hours: int = TIME_GAP_NOTICE_HOURS,
) -> Message | None:
    """Return a context notice when a resumed session has been idle for a while.

    The notice is deliberately represented as a user-side context message because
    ``ConversationManager`` has no separate system-message channel.  Callers may
    inject it during resume; recent sessions remain byte-for-byte unchanged.
    """

    current = now or datetime.now(timezone.utc)
    if last_active.tzinfo is None:
        last_active = last_active.replace(tzinfo=timezone.utc)
    else:
        last_active = last_active.astimezone(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    else:
        current = current.astimezone(timezone.utc)

    elapsed = current - last_active
    if elapsed < timedelta(hours=threshold_hours):
        return None

    hours = max(1, int(elapsed.total_seconds() // 3600))
    return Message(
        role="user",
        content=(
            f"[会话恢复提示] 距离上次活动已约 {hours} 小时，代码可能有变更。"
            "继续执行前请重新检查工作区状态、计划假设和受影响文件。"
        ),
    )


def workspace_fingerprint(work_dir: str | Path) -> str:
    """Hash the workspace identity, Git HEAD and current porcelain state."""

    root = Path(work_dir).expanduser().resolve(strict=False)
    parts = [str(root)]
    try:
        head = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            check=False,
            timeout=5,
        )
        status = subprocess.run(
            [
                "git", "-C", str(root), "status", "--porcelain=v1", "-z",
                "--untracked-files=normal",
            ],
            capture_output=True,
            check=False,
            timeout=5,
        )
        if head.returncode == 0 and status.returncode == 0:
            parts.extend(
                [
                    head.stdout.decode("utf-8", errors="replace").strip(),
                    status.stdout.decode("utf-8", errors="replace"),
                ]
            )
    except (OSError, subprocess.SubprocessError):
        pass
    return hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# RecordType & SessionRecord
# ---------------------------------------------------------------------------


class RecordType(str, Enum):
    SYSTEM_PROMPT = "system_prompt"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL_RESULT = "tool_result"
    COMPRESSION = "compression"
    # Layer-2 compact 标记。auto_compact 压缩对话记录时写入。
    # 内容为结构化载荷（参见 make_compact_boundary / parse_compact_boundary），
    # 包含摘要文本和原样保留的 keep 尾部（以序列化 record 形式内联），
    # 使 resume 可以仅凭此标记重建压缩后的状态，无需重放标记之前的原始前缀。
    COMPACT_BOUNDARY = "compact_boundary"


@dataclass
class SessionRecord:
    type: RecordType
    content: Any
    timestamp: datetime
    tool_use_id: str | None = None
    is_error: bool = False

    def to_jsonl(self) -> str:
        data: dict[str, Any] = {
            "type": self.type.value,
            "content": self.content,
            "timestamp": self.timestamp.isoformat(),
        }
        if self.tool_use_id is not None:
            data["tool_use_id"] = self.tool_use_id
        if self.type == RecordType.TOOL_RESULT:
            data["is_error"] = self.is_error
        return json.dumps(data, ensure_ascii=False)


    @classmethod
    def from_jsonl(cls, line: str) -> SessionRecord | None:
        try:
            data = json.loads(line)
            return cls(
                type=RecordType(data["type"]),
                content=data["content"],
                timestamp=datetime.fromisoformat(data["timestamp"]),
                tool_use_id=data.get("tool_use_id"),
                is_error=data.get("is_error", False),
            )
        except (json.JSONDecodeError, KeyError, ValueError):
            return None

    @classmethod
    def from_message(cls, message: Message) -> list[SessionRecord]:
        now = datetime.now(timezone.utc)
        records: list[SessionRecord] = []

        if message.tool_results:
            for tr in message.tool_results:
                records.append(
                    cls(
                        type=RecordType.TOOL_RESULT,
                        content=tr.content,
                        timestamp=now,
                        tool_use_id=tr.tool_use_id,
                        is_error=tr.is_error,
                    )
                )
        elif message.role == "assistant":
            if message.tool_uses or message.thinking_blocks:
                content_blocks: list[dict[str, Any]] = []
                if message.content:
                    content_blocks.append({"type": "text", "text": message.content})
                for thinking in message.thinking_blocks:
                    content_blocks.append(
                        {
                            "type": "thinking",
                            "thinking": thinking.thinking,
                            "signature": thinking.signature,
                        }
                    )
                for tu in message.tool_uses:
                    content_blocks.append(
                        {
                            "type": "tool_use",
                            "id": tu.tool_use_id,
                            "name": tu.tool_name,
                            "input": tu.arguments,
                        }
                    )
                records.append(
                    cls(type=RecordType.ASSISTANT, content=content_blocks, timestamp=now)
                )
            else:
                records.append(
                    cls(type=RecordType.ASSISTANT, content=message.content, timestamp=now)
                )
        else:
            records.append(
                cls(type=RecordType.USER, content=message.content, timestamp=now)
            )

        return records


# ---------------------------------------------------------------------------
# Compact boundary 载荷（摘要 + 内联的 keep 尾部）
# ---------------------------------------------------------------------------


def _message_to_record_dicts(message: Message) -> list[dict[str, Any]]:
    """将单条 Message 序列化为与磁盘存储格式一致的 record-dict 列表。

    复用 SessionRecord.from_message，使内联的 keep 尾部与正常追加消息的持久化
    结果逐字节一致（assistant 的 tool_uses 变为 content-blocks 列表，每个
    tool_result 独立成一条 record）。这保证了 tool_use↔tool_result 配对的
    无损往返——不像纯 role+content 文本导出那样会丢失 tool call 的关联关系。
    """
    dicts: list[dict[str, Any]] = []
    for rec in SessionRecord.from_message(message):
        data: dict[str, Any] = {"type": rec.type.value, "content": rec.content}
        if rec.tool_use_id is not None:
            data["tool_use_id"] = rec.tool_use_id
        if rec.type == RecordType.TOOL_RESULT:
            data["is_error"] = rec.is_error
        dicts.append(data)
    return dicts


def make_compact_boundary(summary: str, keep: list[Message]) -> SessionRecord:
    """构建一条 COMPACT_BOUNDARY record，内联摘要和原样保留的 keep 尾部。

    `keep` 是 auto_compact 原样保留的近期尾部消息。将其存储在 boundary record
    内部（而不是依赖它在文件中的物理位置），意味着 resume 可以仅凭 boundary
    重建压缩后的状态——boundary 之前的原始前缀保留在磁盘上但不会被重放。
    """
    keep_dicts: list[dict[str, Any]] = []
    for msg in keep:
        keep_dicts.extend(_message_to_record_dicts(msg))
    payload = {"summary": summary, "keep": keep_dicts}
    return SessionRecord(
        type=RecordType.COMPACT_BOUNDARY,
        content=payload,
        timestamp=datetime.now(timezone.utc),
    )


def parse_compact_boundary(record: SessionRecord) -> tuple[str, list[Message]]:
    """make_compact_boundary 的逆操作：返回 (summary, keep_messages)。

    对遗留或格式异常的 payload 降级返回 ("", [])，确保单条损坏的 boundary
    不会导致 resume 崩溃。
    """
    content = record.content
    if not isinstance(content, dict):
        return "", []
    summary = content.get("summary", "")
    keep_raw = content.get("keep", [])
    keep_records: list[SessionRecord] = []
    for item in keep_raw if isinstance(keep_raw, list) else []:
        if not isinstance(item, dict) or "type" not in item:
            continue
        try:
            keep_records.append(
                SessionRecord(
                    type=RecordType(item["type"]),
                    content=item.get("content"),
                    timestamp=record.timestamp,
                    tool_use_id=item.get("tool_use_id"),
                    is_error=item.get("is_error", False),
                )
            )
        except ValueError:
            continue
    return summary, records_to_messages(keep_records)


# ---------------------------------------------------------------------------
# Record ↔ Message 转换
# ---------------------------------------------------------------------------


def records_to_messages(records: list[SessionRecord]) -> list[Message]:
    messages: list[Message] = []
    pending_tool_results: list[ToolResultBlock] = []

    for record in records:
        if record.type == RecordType.TOOL_RESULT:
            pending_tool_results.append(
                ToolResultBlock(
                    tool_use_id=record.tool_use_id or "",
                    content=(
                        record.content
                        if isinstance(record.content, str)
                        else json.dumps(record.content)
                    ),
                    is_error=record.is_error,
                )
            )
            continue

        if pending_tool_results:
            messages.append(
                Message(role="user", content="", tool_results=pending_tool_results)
            )
            pending_tool_results = []

        if record.type == RecordType.SYSTEM_PROMPT:
            continue

        if record.type == RecordType.COMPRESSION:
            messages.append(
                Message(
                    role="user",
                    content="本次会话延续自之前的对话，因上下文空间不足进行了压缩。以下是早期对话的摘要：\n\n" + (record.content or ""),
                )
            )
            continue

        if record.type == RecordType.COMPACT_BOUNDARY:
            # 内联展开：摘要作为 user 消息，后接原样保留的 keep 尾部。
            # resume() 通常已预裁剪到最后一个 boundary，所以这里只会处理
            # 权威的那一条；但在此展开可以保证 records_to_messages 对任何
            # 直接调用者都保持自洽。
            summary, keep_messages = parse_compact_boundary(record)
            messages.append(Message(role="user", content="本次会话延续自之前的对话，因上下文空间不足进行了压缩。以下是早期对话的摘要：\n\n" + summary))
            messages.extend(keep_messages)
            continue

        if record.type == RecordType.USER:
            messages.append(Message(role="user", content=record.content or ""))
        elif record.type == RecordType.ASSISTANT:
            if isinstance(record.content, list):
                text = ""
                tool_uses: list[ToolUseBlock] = []
                thinking_blocks: list[ThinkingBlock] = []
                for block in record.content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text":
                        text += block.get("text", "")
                    elif block.get("type") == "tool_use":
                        tool_uses.append(
                            ToolUseBlock(
                                tool_use_id=block.get("id", ""),
                                tool_name=block.get("name", ""),
                                arguments=block.get("input", {}),
                            )
                        )
                    elif block.get("type") == "thinking":
                        thinking_blocks.append(
                            ThinkingBlock(
                                thinking=block.get("thinking", ""),
                                signature=block.get("signature", ""),
                            )
                        )
                messages.append(
                    Message(
                        role="assistant",
                        content=text,
                        tool_uses=tool_uses,
                        thinking_blocks=thinking_blocks,
                    )
                )
            else:
                messages.append(
                    Message(role="assistant", content=record.content or "")
                )

    if pending_tool_results:
        messages.append(
            Message(role="user", content="", tool_results=pending_tool_results)
        )

    return messages


# ---------------------------------------------------------------------------
# 消息链校验
# ---------------------------------------------------------------------------


def validate_message_chain(records: list[SessionRecord]) -> int:
    last_valid = 0
    pending_tool_uses: set[str] = set()

    for i, record in enumerate(records):
        if record.type == RecordType.ASSISTANT and isinstance(record.content, list):
            for block in record.content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    tool_id = block.get("id", "")
                    if tool_id:
                        pending_tool_uses.add(tool_id)

        if record.type == RecordType.TOOL_RESULT and record.tool_use_id:
            pending_tool_uses.discard(record.tool_use_id)

        if not pending_tool_uses:
            last_valid = i + 1

    return last_valid


# ---------------------------------------------------------------------------
# SessionMeta
# ---------------------------------------------------------------------------


@dataclass
class SessionMeta:
    id: str
    title: str = ""
    summary: str = ""
    message_count: int = 0
    total_tokens: int = 0
    task_id: str = ""
    trace_id: str = ""
    evidence_bundle_ref: str = ""
    work_dir: str = ""
    workspace_fingerprint: str = ""
    provider_name: str = ""
    provider_profile: dict[str, object] = field(default_factory=dict)
    capabilities: list[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_active: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def save(self, path: Path) -> None:
        data = {
            "id": self.id,
            "title": self.title,
            "summary": self.summary,
            "message_count": self.message_count,
            "total_tokens": self.total_tokens,
            "task_id": self.task_id,
            "trace_id": self.trace_id,
            "evidence_bundle_ref": self.evidence_bundle_ref,
            "work_dir": self.work_dir,
            "workspace_fingerprint": self.workspace_fingerprint,
            "provider_name": self.provider_name,
            "provider_profile": dict(self.provider_profile),
            "capabilities": list(self.capabilities),
            "created_at": self.created_at.isoformat(),
            "last_active": self.last_active.isoformat(),
        }
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    @classmethod
    def load(cls, path: Path) -> SessionMeta | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return cls(
                id=data["id"],
                title=data.get("title", ""),
                summary=data.get("summary", ""),
                message_count=data.get("message_count", 0),
                total_tokens=data.get("total_tokens", 0),
                task_id=data.get("task_id", ""),
                trace_id=data.get("trace_id", ""),
                evidence_bundle_ref=data.get("evidence_bundle_ref", ""),
                work_dir=data.get("work_dir", ""),
                workspace_fingerprint=data.get("workspace_fingerprint", ""),
                provider_name=data.get("provider_name", ""),
                provider_profile=(
                    dict(data.get("provider_profile", {}))
                    if isinstance(data.get("provider_profile", {}), dict)
                    else {}
                ),
                capabilities=[str(item) for item in data.get("capabilities", [])],
                created_at=datetime.fromisoformat(data["created_at"]),
                last_active=datetime.fromisoformat(data["last_active"]),
            )
        except (json.JSONDecodeError, KeyError, ValueError):
            return None


# ---------------------------------------------------------------------------
# Session（活跃会话句柄）
# ---------------------------------------------------------------------------


class Session:
    def __init__(
        self,
        session_id: str,
        file: IO[str],
        meta: SessionMeta,
        sessions_dir: Path,
    ) -> None:
        self.session_id = session_id
        self._file = file
        self.meta = meta
        self._sessions_dir = sessions_dir

    def append(self, message: Message) -> None:
        records = SessionRecord.from_message(message)
        for record in records:
            self._file.write(record.to_jsonl() + "\n")
        self._file.flush()

        self.meta.message_count += 1
        self.meta.last_active = datetime.now(timezone.utc)

        if not self.meta.title and message.role == "user" and message.content:
            self.meta.title = message.content[:TITLE_MAX_LENGTH]

        self.meta.save(self._sessions_dir / f"{self.session_id}.meta")

    def append_record(self, record: SessionRecord) -> None:
        """追加一条原始 SessionRecord（例如 compact_boundary 标记）。

        与 append() 不同，此方法不会更新 message_count/title——boundary 是
        结构性标记而非对话轮次。last_active 仍会更新，以保证 session 按最近
        使用排序。
        """
        self._file.write(record.to_jsonl() + "\n")
        self._file.flush()
        self.meta.last_active = datetime.now(timezone.utc)
        self.meta.save(self._sessions_dir / f"{self.session_id}.meta")

    def save_metadata(self) -> None:
        """Persist caller-updated profile fields through the public handle."""

        self.meta.save(self._sessions_dir / f"{self.session_id}.meta")


    def close(self) -> None:
        if self._file and not self._file.closed:
            self._file.flush()
            self._file.close()


# ---------------------------------------------------------------------------
# ResumeResult
# ---------------------------------------------------------------------------


@dataclass
class ResumeResult:
    session: Session
    messages: list[Message]
    last_active: datetime


# ---------------------------------------------------------------------------
# Session 摘要生成
# ---------------------------------------------------------------------------


async def generate_session_summary(
    client: Any, conversation: ConversationManager, protocol: str
) -> str:
    from mewcode.tools.base import StreamEnd, TextDelta

    recent = conversation.history[-10:]
    if not recent:
        return ""

    summary_conv = ConversationManager()
    summary_conv.history = [Message(role="user", content=SESSION_SUMMARY_PROMPT)]
    for msg in recent:
        summary_conv.history.append(msg)
    summary_conv.history.append(
        Message(role="user", content="请用一句话总结上面的对话内容。不要调用工具。")
    )

    collected = ""
    try:
        async for event in client.stream(
            summary_conv, system=SESSION_SUMMARY_PROMPT
        ):
            if isinstance(event, TextDelta):
                collected += event.text
            elif isinstance(event, StreamEnd):
                pass
    except Exception:
        return ""

    return collected.strip()


# ---------------------------------------------------------------------------
# SessionManager
# ---------------------------------------------------------------------------


def _generate_session_id() -> str:
    now = datetime.now()
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=4))
    return f"session_{now.strftime('%Y%m%d_%H%M%S')}_{suffix}"


def _validate_session_id(session_id: str) -> str:
    """Keep every session operation inside the sessions directory."""

    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", session_id):
        raise ValueError("invalid session id")
    return session_id


class SessionManager:
    def __init__(self, work_dir: str) -> None:
        self._sessions_dir = Path(work_dir) / SESSIONS_DIR
        self._work_dir = str(Path(work_dir).resolve(strict=False))


    def create(
        self,
        *,
        workspace_fingerprint: str = "",
        provider_name: str = "",
        provider_profile: dict[str, object] | None = None,
        capabilities: list[str] | tuple[str, ...] = (),
    ) -> Session:
        self._sessions_dir.mkdir(parents=True, exist_ok=True)
        session_id = _generate_session_id()
        jsonl_path = self._sessions_dir / f"{session_id}.jsonl"
        meta = SessionMeta(
            id=session_id,
            work_dir=self._work_dir,
            workspace_fingerprint=workspace_fingerprint,
            provider_name=provider_name,
            provider_profile=dict(provider_profile or {}),
            capabilities=list(capabilities),
        )
        meta.save(self._sessions_dir / f"{session_id}.meta")

        file = open(jsonl_path, "a", encoding="utf-8")  # noqa: SIM115
        return Session(
            session_id=session_id,
            file=file,
            meta=meta,
            sessions_dir=self._sessions_dir,
        )


    def list(self) -> list[SessionMeta]:
        metas: list[SessionMeta] = []
        for meta_path in self._sessions_dir.glob("*.meta"):
            meta = SessionMeta.load(meta_path)
            if meta is not None:
                metas.append(meta)
        metas.sort(key=lambda m: m.last_active, reverse=True)
        return metas

    def get_meta(self, session_id: str) -> SessionMeta | None:
        """Read one profile without opening an append handle."""

        session_id = _validate_session_id(session_id)
        return SessionMeta.load(self._sessions_dir / f"{session_id}.meta")

    def resume(self, session_id: str) -> ResumeResult | None:
        session_id = _validate_session_id(session_id)
        jsonl_path = self._sessions_dir / f"{session_id}.jsonl"
        meta_path = self._sessions_dir / f"{session_id}.meta"

        if not jsonl_path.exists():
            return None

        meta = SessionMeta.load(meta_path)
        if meta is None:
            return None

        records: list[SessionRecord] = []
        with open(jsonl_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                record = SessionRecord.from_jsonl(line)
                if record is not None:
                    records.append(record)

        # 重建压缩后的状态：仅从最后一个 compact_boundary 开始重放。
        # 该标记之前的 record 是已被摘要过的原始前缀——保留在磁盘上供审计，
        # 但不再重放。标记本身内联了摘要 + 原样 keep 尾部，标记之后追加的
        # 普通消息（续写）照常重放。没有 boundary 则全量重放（兼容旧 session）。
        last_boundary = -1
        for i, rec in enumerate(records):
            if rec.type == RecordType.COMPACT_BOUNDARY:
                last_boundary = i
        if last_boundary >= 0:
            records = records[last_boundary:]

        valid_count = validate_message_chain(records)
        records = records[:valid_count]
        messages = records_to_messages(records)

        file = open(jsonl_path, "a", encoding="utf-8")  # noqa: SIM115
        session = Session(
            session_id=session_id,
            file=file,
            meta=meta,
            sessions_dir=self._sessions_dir,
        )

        return ResumeResult(
            session=session,
            messages=messages,
            last_active=meta.last_active,
        )

    def delete(self, session_id: str) -> bool:
        session_id = _validate_session_id(session_id)
        jsonl_path = self._sessions_dir / f"{session_id}.jsonl"
        meta_path = self._sessions_dir / f"{session_id}.meta"

        deleted = False
        if jsonl_path.exists():
            jsonl_path.unlink()
            deleted = True
        if meta_path.exists():
            meta_path.unlink()
            deleted = True
        return deleted

    @staticmethod
    def _redact(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: (
                    "[REDACTED]"
                    if re.search(r"(?i)(api[_-]?key|authorization|token|secret|password)", str(key))
                    else SessionManager._redact(item)
                )
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [SessionManager._redact(item) for item in value]
        if isinstance(value, str):
            value = re.sub(
                r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{8,}",
                r"\1[REDACTED]",
                value,
            )
            value = re.sub(
                r"(?i)((?:api[_-]?key|token|secret|password)\s*[:=]\s*)\S+",
                r"\1[REDACTED]",
                value,
            )
        return value

    def export(self, session_id: str, *, format: str = "json") -> str | None:
        """Return a redacted, read-only session export without opening append handles."""

        if format not in {"json", "markdown"}:
            raise ValueError("session export format must be json or markdown")
        session_id = _validate_session_id(session_id)
        meta_path = self._sessions_dir / f"{session_id}.meta"
        jsonl_path = self._sessions_dir / f"{session_id}.jsonl"
        meta = SessionMeta.load(meta_path)
        if meta is None or not jsonl_path.is_file():
            return None
        records: list[dict[str, Any]] = []
        for line in jsonl_path.read_text(encoding="utf-8").splitlines():
            record = SessionRecord.from_jsonl(line)
            if record is None:
                continue
            records.append(
                {
                    "type": record.type.value,
                    "content": self._redact(record.content),
                    "timestamp": record.timestamp.isoformat(),
                    "tool_use_id": record.tool_use_id,
                    "is_error": record.is_error,
                }
            )
        metadata = self._redact(
            {
                "id": meta.id,
                "title": meta.title,
                "summary": meta.summary,
                "message_count": meta.message_count,
                "total_tokens": meta.total_tokens,
                "task_id": meta.task_id,
                "trace_id": meta.trace_id,
                "evidence_bundle_ref": meta.evidence_bundle_ref,
                "work_dir": meta.work_dir,
                "workspace_fingerprint": meta.workspace_fingerprint,
                "provider_name": meta.provider_name,
                "provider_profile": meta.provider_profile,
                "capabilities": meta.capabilities,
                "created_at": meta.created_at.isoformat(),
                "last_active": meta.last_active.isoformat(),
            }
        )
        if format == "json":
            return json.dumps(
                {"schema_version": 1, "metadata": metadata, "records": records},
                ensure_ascii=False,
                indent=2,
            )
        lines = [f"# Session {meta.id}", "", f"- Provider: {meta.provider_name or '(unknown)'}"]
        lines.append(f"- Workspace: {meta.work_dir or '(unknown)'}")
        lines.append(f"- Messages: {meta.message_count}")
        lines.extend(["", "## Records", ""])
        for record in records:
            lines.append(f"### {record['type']} · {record['timestamp']}")
            lines.append("")
            content = record["content"]
            lines.append(content if isinstance(content, str) else f"```json\n{json.dumps(content, ensure_ascii=False, indent=2)}\n```")
            lines.append("")
        return "\n".join(lines)

    def cleanup(self, max_age_days: int = DEFAULT_MAX_AGE_DAYS) -> int:
        cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
        removed = 0

        for meta_path in list(self._sessions_dir.glob("*.meta")):
            meta = SessionMeta.load(meta_path)
            if meta is not None and meta.last_active < cutoff:
                self.delete(meta.id)
                removed += 1

        return removed
