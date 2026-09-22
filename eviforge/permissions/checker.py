from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from eviforge.permissions.dangerous import DangerousCommandDetector, is_safe_command
from eviforge.permissions.modes import DecisionEffect, PermissionMode, mode_decide
from eviforge.permissions.rules import RuleEngine, extract_content, extract_path
from eviforge.permissions.sandbox import PathSandbox
from eviforge.tools.base import Tool
from eviforge.tools.work_dir import resolve_tool_path

_PLAN_MODE_ALLOWED_TOOLS = frozenset({"ToolSearch", "AskUserQuestion", "ExitPlanMode"})
_PLAN_MODE_AGENT_TYPES = frozenset({"explore", "plan"})


@dataclass
class Decision:
    effect: DecisionEffect
    reason: str


class PermissionChecker:


    def __init__(
        self,
        detector: DangerousCommandDetector,
        sandbox: PathSandbox,
        rule_engine: RuleEngine,
        mode: PermissionMode = PermissionMode.DEFAULT,
    ) -> None:
        self.detector = detector
        self.sandbox = sandbox
        self.rule_engine = rule_engine
        self.mode = mode
        self.plan_file_path: str = ""


    def check(self, tool: Tool, arguments: dict[str, Any]) -> Decision:
        content = extract_content(tool.name, arguments)
        sandbox_path = extract_path(tool.name, arguments)

        # Layer 1: 危险命令黑名单（仅 command 类工具）
        if tool.category == "command":
            hit, reason = self.detector.detect(content)
            if hit:
                return Decision(effect="deny", reason=f"危险命令拦截: {reason}")

        # Layer 2: 路径沙箱（仅文件类工具）
        if tool.category in ("read", "write") and sandbox_path:
            ok, reason = self.sandbox.check(sandbox_path)
            if not ok:
                return Decision(effect="deny", reason=f"路径沙箱拦截: {reason}")

        # Plan 是不可被 allow 规则或人工批准覆盖的硬边界。
        if self.mode == PermissionMode.PLAN:
            rule_result = self.rule_engine.evaluate(tool.name, content)
            if rule_result == "deny":
                return Decision(effect="deny", reason="权限规则拒绝")
            if tool.name == "Agent":
                if self._is_plan_safe_agent_call(arguments):
                    return Decision(effect="allow", reason="Plan mode: read-only sub-agent")
                return Decision(effect="deny", reason="Plan mode: only read-only sub-agents allowed")
            if tool.name in _PLAN_MODE_ALLOWED_TOOLS:
                return Decision(effect="allow", reason="Plan mode: allowed tool")
            if tool.category == "read":
                return Decision(effect="allow", reason="Plan mode: read-only tool")
            if tool.name in ("WriteFile", "EditFile") and self._is_plan_file(content):
                return Decision(effect="allow", reason="Plan mode: plan file write")
            if tool.category == "command" and is_safe_command(content):
                return Decision(effect="allow", reason="Plan mode: safe read-only command")
            return Decision(effect="deny", reason="Plan mode: read-only operations only")

        # Explicit denials also constrain the safe-command shortcut.
        rule_result = self.rule_engine.evaluate(tool.name, content)
        if rule_result == "deny":
            return Decision(effect="deny", reason="权限规则拒绝")

        # Layer 3: 安全的只读命令（自动放行）
        if tool.category == "command" and is_safe_command(content):
            return Decision(effect="allow", reason="Safe read-only command")

        # Layer 4: 规则引擎匹配
        if rule_result == "allow":
            return Decision(effect="allow", reason="权限规则放行")

        # Layer 5: 权限模式兜底判定
        effect = mode_decide(self.mode, tool.category)
        if effect == "allow":
            return Decision(effect="allow", reason=f"权限模式 {self.mode.value} 放行")
        if effect == "deny":
            return Decision(effect="deny", reason=f"权限模式 {self.mode.value} 拒绝")

        # Layer 6: 触发人工确认（HITL）
        return Decision(effect="ask", reason="需要用户确认")


    @staticmethod
    def _is_plan_safe_agent_call(arguments: dict[str, Any]) -> bool:
        subagent_type = arguments.get("subagent_type")
        if not isinstance(subagent_type, str):
            return False
        if subagent_type.strip().lower() not in _PLAN_MODE_AGENT_TYPES:
            return False
        if arguments.get("team_name"):
            return False
        if arguments.get("isolation"):
            return False
        return True


    def _is_plan_file(self, target_path: str) -> bool:
        if not self.plan_file_path or not target_path:
            return False
        try:
            target = resolve_tool_path(target_path, default=self.sandbox.project_root)
            plan = resolve_tool_path(
                self.plan_file_path,
                default=self.sandbox.project_root,
            )
            return target.resolve(strict=False) == plan.resolve(strict=False)
        except (OSError, RuntimeError, ValueError):
            return False
