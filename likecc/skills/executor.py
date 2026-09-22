from __future__ import annotations

import copy
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from likecc.agents.tool_filter import ALL_AGENT_DISALLOWED_TOOLS, clone_agent_registry
from likecc.conversation import ConversationManager, Message
from likecc.skills.parser import SkillDef, substitute_arguments
from likecc.tools import ToolRegistry

if TYPE_CHECKING:
    from likecc.agent import Agent
    from likecc.client import LLMClient

log = logging.getLogger(__name__)

SKILL_FORK_DISALLOWED_TOOLS = ALL_AGENT_DISALLOWED_TOOLS | frozenset({
    # These tools hold references to the parent Agent/TeamManager.  Sharing
    # them would let a hidden fork mutate or tear down its parent's team.
    "TeamCreate",
    "TeamDelete",
})

FORK_RECENT_COUNT = 5


class SkillDependencyError(Exception):
    pass


def filter_tool_registry(
    registry: ToolRegistry, allowed: list[str]
) -> ToolRegistry:
    if not allowed:
        return registry

    filtered = ToolRegistry()
    for name in allowed:
        tool = registry.get(name)
        if tool is None:
            raise SkillDependencyError(
                f"Skill requires tool '{name}' but it is not registered"
            )
        filtered.register(tool)

    for tool in registry.list_tools():
        if getattr(tool, "is_system_tool", False) and filtered.get(tool.name) is None:
            filtered.register(tool)

    return filtered


def filter_fork_tool_registry(
    registry: ToolRegistry,
    allowed: list[str],
    protocol: str,
) -> ToolRegistry:
    """Build a fork-local registry without parent-control capabilities.

    Stateful system helpers need special handling: ToolSearch must search only
    this filtered registry, and LoadSkill must be a distinct instance so it can
    later be bound to the fork Agent without rebinding the parent's tool.
    """
    selected = filter_tool_registry(registry, allowed)
    selected_tools = []
    include_tool_search = False

    for tool in selected.list_tools():
        if tool.name in SKILL_FORK_DISALLOWED_TOOLS:
            continue
        if tool.name == "ToolSearch":
            include_tool_search = True
            continue
        selected_tools.append(tool)

    filtered = clone_agent_registry(
        registry, selected_tools, preserve_discovery=True,
    )

    if include_tool_search:
        from likecc.tools.impl.tool_search import ToolSearchTool

        filtered.register(ToolSearchTool(filtered, protocol=protocol))
        if not registry.is_enabled("ToolSearch"):
            filtered.disable("ToolSearch")

    return filtered


class SkillExecutor:


    def __init__(
        self,
        agent: Agent,
        client: LLMClient,
        protocol: str,
    ) -> None:
        self.agent = agent
        self.client = client
        self.protocol = protocol


    def execute_inline(self, skill: SkillDef, args: str) -> None:
        prompt = substitute_arguments(skill.prompt_body, args)
        self.agent.activate_skill(skill.name, prompt)
        if getattr(self.agent, "recovery_state", None) is not None:
            self.agent.recovery_state.record_skill_invocation(skill.name, prompt)


    async def execute_fork(
        self, skill: SkillDef, args: str
    ) -> str:
        # A fork is intentionally detached from later worktree switches made by
        # the parent.  Capture an absolute directory before the first await and
        # give the child an independent permission checker whose sandbox is
        # rooted at that same directory.
        fork_work_dir = str(Path(self.agent.work_dir).resolve())
        prompt = substitute_arguments(skill.prompt_body, args)
        if getattr(self.agent, "recovery_state", None) is not None:
            self.agent.recovery_state.record_skill_invocation(
                skill.name, skill.prompt_body
            )

        fork_conv = ConversationManager()

        context_messages = self._build_fork_context(skill.context)
        for msg in context_messages:
            if msg.role == "user":
                fork_conv.add_user_message(msg.content)
            else:
                fork_conv.add_assistant_message(msg.content)

        fork_conv.add_user_message(prompt)

        try:
            filtered_registry = filter_fork_tool_registry(
                self.agent.registry, skill.allowed_tools, self.protocol
            )
        except SkillDependencyError as e:
            return f"Skill execution failed: {e}"

        from likecc.agent import Agent as AgentClass

        fork_agent = AgentClass(
            client=self.client,
            registry=filtered_registry,
            protocol=self.protocol,
            work_dir=fork_work_dir,
            max_iterations=self.agent.max_iterations,
            permission_checker=self._build_fork_permission_checker(fork_work_dir),
            context_window=self.agent.context_window,
            hook_engine=self.agent.hook_engine,
        )
        load_skill = filtered_registry.get("LoadSkill")
        if load_skill is not None and hasattr(load_skill, "set_agent"):
            load_skill.set_agent(fork_agent)

        # Forked skills have no UI consumer to answer PermissionRequest events.
        # The non-interactive loop turns an `ask` decision into a deterministic
        # denial instead of waiting forever for a response that cannot arrive.
        return await fork_agent.run_to_completion("", conversation=fork_conv)


    def _build_fork_permission_checker(self, work_dir: str):
        """Snapshot the parent's permission policy for a hidden skill agent.

        The checker must not be shared: worktree changes rebase the parent's
        mutable sandbox and permission-mode changes mutate the checker in
        place.  A deep copy keeps detector extensions, rule paths and explicit
        extra sandbox roots while fixing the fork's project root and mode at
        launch time.
        """
        from likecc.permissions import (
            DangerousCommandDetector,
            PathSandbox,
            PermissionChecker,
            PermissionMode,
            RuleEngine,
        )

        parent_checker = self.agent.permission_checker
        mode = getattr(self.agent, "permission_mode", PermissionMode.DEFAULT)

        if parent_checker is None:
            return PermissionChecker(
                detector=DangerousCommandDetector(),
                sandbox=PathSandbox(work_dir),
                rule_engine=RuleEngine(),
                mode=mode,
            )

        fork_checker = copy.deepcopy(parent_checker)
        fork_checker.mode = mode
        fork_checker.sandbox.set_project_root(work_dir)
        return fork_checker


    def _build_fork_context(self, mode: str) -> list[Message]:
        if mode == "none":
            return []

        conversation = getattr(self.agent, "_current_conversation", None)
        main_history = conversation.history if conversation is not None else []

        if mode == "recent":
            content_messages = [
                m for m in main_history
                if m.content and not m.tool_results
            ]
            return content_messages[-FORK_RECENT_COUNT:]

        if mode == "full":
            content_messages = [
                m for m in main_history
                if m.content and not m.tool_results
            ]
            if not content_messages:
                return []
            summary_parts = []
            for m in content_messages:
                prefix = "User" if m.role == "user" else "Assistant"
                text = m.content[:200]
                if len(m.content) > 200:
                    text += "..."
                summary_parts.append(f"{prefix}: {text}")
            summary = "## Previous conversation summary\n\n" + "\n\n".join(summary_parts)
            return [Message(role="user", content=summary)]

        return []
