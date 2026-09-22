from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from eviforge.conversation import ConversationManager, Message

USER_MEMORIES_RELPATH = ".eviforge/memories.md"
PROJECT_MEMORIES_RELPATH = ".eviforge/memories.md"

MEMORY_EXTRACTION_PROMPT = """\
你是一个记忆提取助手。分析下面的对话，提取值得长期记忆的信息，更新 memories.md。

分类规则：
- **用户偏好**：用户的编码习惯和风格要求（如缩进、命名规范、语言偏好）
- **纠正反馈**：用户明确指出的错误和正确做法
- **项目知识**：当前项目的具体技术信息（技术栈、目录结构、部署方式）
- **参考资料**：外部链接和文档地址

规则：
1. 已有相同含义的条目不要重复添加
2. 没有值得记忆的内容，该分类下留空（不要写任何条目，不要写占位符）
3. 每条记忆用一行 `- ` 开头，必须是具体内容，不要用 `...` 占位
4. 输出完整的 memories.md 内容，包含所有四个分类标题

输出格式（严格遵守，没有内容的分类下不写任何条目）：
### 用户偏好
- 用户偏好简洁代码风格

### 纠正反馈

### 项目知识
- 项目使用 PostgreSQL 15

### 参考资料

不要输出任何其他内容，不要调用任何工具。"""

_USER_LEVEL_HEADERS = {"用户偏好", "纠正反馈"}
_PROJECT_LEVEL_HEADERS = {"项目知识", "参考资料"}


class MemoryManager:
    def __init__(self, project_root: str, governance: Any = None) -> None:
        self._user_path = Path.home() / USER_MEMORIES_RELPATH
        self._project_path = Path(project_root) / PROJECT_MEMORIES_RELPATH
        self._last_extraction_msg_count = 0
        self.governance = governance


    @property
    def user_path(self) -> Path:
        return self._user_path


    @property
    def project_path(self) -> Path:
        return self._project_path

    @property
    def user_mem_dir(self) -> Path:
        """User-level memory directory (~/.eviforge/memory/).

        This is where .md memory files with frontmatter (type user/feedback)
        live. Distinct from ``user_path`` which points at the flat
        ``memories.md`` file.
        """
        return Path.home() / ".eviforge" / "memory"

    @property
    def project_mem_dir(self) -> Path:
        """Project-level memory directory (<project>/.eviforge/memory/).

        This is where .md memory files with frontmatter (type
        project/reference) live. Distinct from ``project_path`` which
        points at the flat ``memories.md`` file.
        """
        return self._project_path.parent / "memory"

    def load(self) -> str:
        if self.governance is not None:
            return self.governance.render_memory_context()
        sections: list[str] = []

        if self._user_path.exists():
            content = self._user_path.read_text(encoding="utf-8").strip()
            if content:
                sections.append(content)

        if self._project_path.exists():
            content = self._project_path.read_text(encoding="utf-8").strip()
            if content:
                sections.append(content)

        return "\n\n".join(sections)

    def refresh_context(self, conversation: ConversationManager) -> bool:
        if self.governance is None:
            return False
        from eviforge.governance import MEMORY_BUDGET

        prefix = "<eviforge-governed-memory>\n"
        suffix = "\n</eviforge-governed-memory>"
        body = self.governance.render_memory_context(MEMORY_BUDGET - len(prefix) - len(suffix))
        content = prefix + body + suffix if body else ""
        owned = [i for i, msg in enumerate(conversation.history)
                 if msg.role == "user" and not msg.tool_uses and not msg.tool_results
                 and msg.content.startswith(prefix) and msg.content.endswith(suffix)]
        if len(owned) == 1 and conversation.history[owned[0]].content == content:
            return False
        if not owned and not content:
            return False
        position = owned[0] if owned else (1 if conversation.env_injected and conversation.history else 0)
        for index in reversed(owned):
            conversation.history.pop(index)
        if content:
            message = Message(role="user", content=content)
            message._generated_context = True
            conversation.history.insert(position, message)
        conversation.last_input_tokens = conversation.baseline_tokens = conversation.anchor_count = 0
        return True

    async def extract(
        self,
        client: Any,
        conversation: ConversationManager,
        protocol: str,
        *,
        source_task: str | None = None,
        source_trace: str | None = None,
        policy: Any = None,
    ) -> None:
        from eviforge.tools.base import StreamEnd, TextDelta

        current_memories = self.load()

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

        prompt = (
            f"{MEMORY_EXTRACTION_PROMPT}\n\n"
            f"## 当前 memories.md\n"
            f"{current_memories if current_memories else '(空)'}\n\n"
            f"## 最近对话\n"
            f"{chr(10).join(conv_lines)}\n\n"
            f"请输出更新后的完整 memories.md 内容。"
        )
        if self.governance is not None:
            prompt += "\n以上输出仅是待审查候选。不要声称已验证、已获人工批准或已发布。"

        extract_conv = ConversationManager()
        extract_conv.history = [Message(role="user", content=prompt)]

        collected = ""
        complete = False
        try:
            from eviforge.reliability import stream_with_recovery
            async for event in stream_with_recovery(
                client, extract_conv, system="你是一个记忆提取助手。", policy=policy,
            ):
                if isinstance(event, TextDelta):
                    collected += event.text
                elif isinstance(event, StreamEnd):
                    complete = True
        except Exception:
            return

        self._last_extraction_msg_count = len(conversation.history)

        collected = collected.strip()
        if not collected:
            return

        if self.governance is not None:
            if not complete:
                return
            evidence_hash = hashlib.sha256("\n".join(conv_lines).encode("utf-8")).hexdigest()
            task = source_task or f"conversation:{evidence_hash}"
            trace = source_trace or f"extraction:{evidence_hash}"
            user_sections, project_sections = self._split_sections(collected)
            for scope, sections in (("user", user_sections), ("project", project_sections)):
                if sections:
                    self.governance.propose_memory(
                        "\n\n".join(sections), scope=scope,
                        name=f"session-{hashlib.sha256(task.encode('utf-8')).hexdigest()[:16]}",
                        source_task=task, source_trace=trace,
                    )
            return
        self._write_memories(collected)

    @staticmethod
    def _split_sections(content: str) -> tuple[list[str], list[str]]:
        user_sections: list[str] = []
        project_sections: list[str] = []

        current_header = ""
        current_lines: list[str] = []

        for line in content.split("\n"):
            if line.startswith("### "):
                if current_header:
                    MemoryManager._assign_section(
                        current_header, current_lines, user_sections, project_sections
                    )
                current_header = line
                current_lines = []
            else:
                current_lines.append(line)

        if current_header:
            MemoryManager._assign_section(
                current_header, current_lines, user_sections, project_sections
            )
        return user_sections, project_sections

    def _write_memories(self, content: str) -> None:
        if self.governance is not None:
            raise ValueError("Governed memories must be proposed and reviewed before publication")
        user_sections, project_sections = self._split_sections(content)

        if user_sections:
            self._user_path.parent.mkdir(parents=True, exist_ok=True)
            self._user_path.write_text(
                "\n".join(user_sections).strip() + "\n", encoding="utf-8"
            )

        if project_sections:
            self._project_path.parent.mkdir(parents=True, exist_ok=True)
            self._project_path.write_text(
                "\n".join(project_sections).strip() + "\n", encoding="utf-8"
            )

    @staticmethod
    def _is_placeholder(line: str) -> bool:
        stripped = line.strip().lstrip("- ").strip()
        return stripped in {"", "...", "…", "无", "暂无", "N/A"}


    @staticmethod
    def _assign_section(
        header: str,
        lines: list[str],
        user_sections: list[str],
        project_sections: list[str],
    ) -> None:
        real_lines = [l for l in lines if l.strip().startswith("- ") and not MemoryManager._is_placeholder(l)]
        if not real_lines:
            return

        section_text = header + "\n" + "\n".join(real_lines)

        for keyword in _USER_LEVEL_HEADERS:
            if keyword in header:
                user_sections.append(section_text)
                return

        for keyword in _PROJECT_LEVEL_HEADERS:
            if keyword in header:
                project_sections.append(section_text)
                return


    def clear(self) -> None:
        if self.governance is not None:
            self.governance.clear_memories()
            return
        if self._user_path.exists():
            self._user_path.write_text("", encoding="utf-8")
        if self._project_path.exists():
            self._project_path.write_text("", encoding="utf-8")

    def get_display_text(self) -> str:
        if self.governance is not None:
            entries = self.governance.list_entries(kind="memory")
            return "\n\n".join(
                f"[{entry['scope']}/{entry['status']}] {entry['name']} v{entry['version']}\n"
                f"id={entry['id']} hash={entry['content_hash']}\n"
                f"task={entry['source_task']} trace={entry['source_trace']}\n{entry['content']}"
                for entry in entries
            ) or "当前没有任何受治理的记忆记录。"
        parts: list[str] = []

        if self._user_path.exists():
            content = self._user_path.read_text(encoding="utf-8").strip()
            if content:
                parts.append(f"[用户级] {self._user_path}\n{content}")

        if self._project_path.exists():
            content = self._project_path.read_text(encoding="utf-8").strip()
            if content:
                parts.append(f"[项目级] {self._project_path}\n{content}")

        if not parts:
            return "当前没有任何自动记忆。"

        return "\n\n".join(parts)
