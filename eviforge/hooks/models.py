from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from eviforge.hooks.conditions import ConditionGroup


@dataclass
class Action:
    type: str
    command: str = ""
    message: str = ""
    url: str = ""
    method: str = "POST"
    body: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    prompt: str = ""
    timeout: int = 30
    builtin: str = ""


@dataclass
class ActionResult:
    output: str = ""
    success: bool = True
    blocking: bool = False


@dataclass
class Hook:
    id: str
    event: str
    action: Action
    condition: ConditionGroup | None = None
    reject: bool = False
    once: bool = False
    async_exec: bool = False
    executed: bool = False
    scope: str = "all"


    def should_run(self) -> bool:
        if self.once and self.executed:
            return False
        return True


    def mark_executed(self) -> None:
        self.executed = True


@dataclass
class HookContext:
    event_name: str = ""
    tool_name: str = ""
    tool_args: dict[str, Any] = field(default_factory=dict)
    file_path: str = ""
    message: str = ""
    error: str = ""
    work_dir: str = ""
    session_id: str = ""
    turn_id: str = ""
    agent_id: str = ""
    parent_id: str = ""
    tool_succeeded: bool | None = None
    tool_output: str = ""
    tool_status: str = ""
    run_status: str = ""

    def get_field(self, name: str) -> str:
        if name == "tool":
            return self.tool_name
        if name == "event":
            return self.event_name
        if name in {"session_id", "turn_id", "agent_id", "parent_id", "run_status"}:
            return str(getattr(self, name))
        if name == "tool_succeeded":
            return "" if self.tool_succeeded is None else str(self.tool_succeeded).lower()
        if name.startswith("args."):
            key = name[5:]
            value = self.tool_args.get(key, "")
            return str(value) if value else ""
        return ""

    def expand(self, template: str) -> str:
        result = template
        result = result.replace("$EVENT", self.event_name)
        result = result.replace("$TOOL_NAME", self.tool_name)
        result = result.replace("$FILE_PATH", self.file_path)
        result = result.replace("$MESSAGE", self.message)
        result = result.replace("$ERROR", self.error)
        for name in ("session_id", "turn_id", "agent_id", "parent_id", "tool_output", "tool_status"):
            result = result.replace("$" + name.upper(), str(getattr(self, name)))
        for key, value in self.tool_args.items():
            result = result.replace(f"$TOOL_ARGS.{key}", str(value))
        return result


class ToolRejectedError(Exception):
    def __init__(self, tool: str, reason: str, hook_id: str) -> None:
        self.tool = tool
        self.reason = reason
        self.hook_id = hook_id
        super().__init__(f"Tool '{tool}' rejected by hook '{hook_id}': {reason}")
