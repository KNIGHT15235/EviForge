from __future__ import annotations

import copy
import logging
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from likecc.permissions.modes import PermissionMode, mode_decide
from likecc.tools.base import Tool, ToolResult

if TYPE_CHECKING:
    from likecc.agent import Agent
    from likecc.agents.loader import AgentLoader
    from likecc.agents.task_manager import TaskManager
    from likecc.agents.trace import TraceManager
    from likecc.client import LLMClient

log = logging.getLogger(__name__)


class AgentToolParams(BaseModel):
    prompt: str
    description: str
    subagent_type: str | None = None
    model: str | None = None
    run_in_background: bool = False
    name: str | None = None
    isolation: str | None = None
    team_name: str | None = Field(
        default=None,
        description=(
            "REQUIRED when creating team members. Spawns the agent as a long-running "
            "teammate under this team (created via TeamCreate). Unlike regular sub-agents, "
            "team members run in their own terminal, persist after the lead returns, and "
            "communicate with each other via SendMessage. Without team_name this is a "
            "one-shot sub-agent: named types run inline unless configured for background "
            "execution; an unnamed fork always returns a background task ID."
        ),
    )


PERMISSION_MODE_MAP = {
    "default": "DEFAULT",
    "acceptEdits": "ACCEPT_EDITS",
    "dontAsk": "DONT_ASK",
}


def _bound_subagent_permission_mode(
    parent_mode: PermissionMode,
    requested_mode: PermissionMode,
) -> PermissionMode:
    if parent_mode == PermissionMode.PLAN:
        return PermissionMode.PLAN
    effect_rank = {"deny": 0, "ask": 1, "allow": 2}
    if any(
        effect_rank[mode_decide(requested_mode, category)]
        > effect_rank[mode_decide(parent_mode, category)]
        for category in ("read", "write", "command")
    ):
        return parent_mode
    return requested_mode


TEAMMATE_ADDENDUM = (
    "\n\nIMPORTANT: You are running as an agent in a team.\n"
    "Just writing a response in text is not visible to others\n"
    "on your team - you MUST use the SendMessage tool.\n"
    "The user interacts primarily with the team lead.\n"
    "Your work is coordinated through the task system\n"
    "and teammate messaging.\n\n"
    "You are working in an isolated Git worktree. "
    "All file paths you use MUST be relative to your current working directory. "
    "Do NOT use absolute paths from the original project — they are outside your sandbox and will be rejected."
)


class AgentTool(Tool):
    name = "Agent"
    description = (
        "Launch a sub-agent to handle a task in an isolated context. "
        "Use subagent_type to select a predefined agent type (e.g. Explore, Plan, general-purpose), "
        "or leave it empty to fork the current conversation. "
        "Use team_name to spawn a teammate in an existing team."
    )
    params_model = AgentToolParams
    category = "command"
    is_concurrency_safe = False


    def __init__(
        self,
        agent_loader: AgentLoader,
        task_manager: TaskManager,
        trace_manager: TraceManager,
        parent_agent: Agent,
        enable_fork: bool = False,
        provider_config: Any = None,
        worktree_manager: Any = None,
        team_manager: Any = None,
    ) -> None:
        self._agent_loader = agent_loader
        self._task_manager = task_manager
        self._trace_manager = trace_manager
        self._parent_agent = parent_agent
        self._enable_fork = enable_fork
        self._provider_config = provider_config
        self._worktree_manager = worktree_manager
        self._team_manager = team_manager

    async def execute(self, params: BaseModel) -> ToolResult:
        p: AgentToolParams = params  # type: ignore[assignment]

        from likecc.agents.fork import has_fork_boilerplate

        caller_conversation = (
            getattr(self._parent_agent, "_current_conversation", None)
            or getattr(self._parent_agent, "_fork_conversation", None)
        )
        if getattr(self._parent_agent, "query_source", "main") == "fork" or (
            caller_conversation is not None
            and has_fork_boilerplate(caller_conversation)
        ):
            return ToolResult(
                output="Cannot launch an Agent from a forked agent. Fork nesting is not allowed.",
                is_error=True,
            )

        plan_rejection = self._validate_plan_subagent(p)
        if plan_rejection is not None:
            return plan_rejection

        if p.team_name:
            return await self._execute_as_teammate(p)

        isolation = p.isolation or ""
        if not isolation and p.subagent_type:
            defn = self._agent_loader.get(p.subagent_type)
            if defn and defn.isolation:
                isolation = defn.isolation

        if isolation == "worktree":
            return await self._execute_with_worktree(p)
        if isolation:
            return ToolResult(output=f"Unsupported isolation mode: '{isolation}'.", is_error=True)

        return await self._execute_subagent(p)

    async def _execute_subagent(self, p: AgentToolParams, *, isolated: bool = False) -> ToolResult:
        from likecc.agents.fork import ForkError, build_forked_messages
        from likecc.agents.parser import AgentDef
        from likecc.agents.tool_filter import clone_agent_registry, resolve_agent_tools
        from likecc.agent import Agent as AgentClass
        from likecc.conversation import ConversationManager

        definition: AgentDef | None = None
        conversation: ConversationManager
        is_fork = not p.subagent_type

        if p.subagent_type:
            definition = self._agent_loader.get(p.subagent_type)
            if definition is None:
                return ToolResult(
                    output=f"Unknown agent type: '{p.subagent_type}'. "
                    f"Available types: {', '.join(t for t, _ in self._agent_loader.list_agents())}",
                    is_error=True,
                )
            conversation = ConversationManager()
        else:
            if not self._enable_fork:
                return ToolResult(
                    output="Fork mode is not enabled. "
                    "Set 'enable_fork: true' in config.yaml to use fork, "
                    "or specify a subagent_type parameter.",
                    is_error=True,
                )
            try:
                parent_conv = getattr(self._parent_agent, '_current_conversation', None)
                if parent_conv is None:
                    return ToolResult(
                        output="Cannot fork: no active conversation in parent agent.",
                        is_error=True,
                    )
                conversation = build_forked_messages(parent_conv, p.prompt)
            except ForkError as e:
                return ToolResult(output=str(e), is_error=True)

            definition = AgentDef(
                agent_type="fork",
                when_to_use="Forked from parent agent",
                system_prompt="",
                disallowed_tools=[],
                model="inherit",
                max_turns=self._parent_agent.max_iterations,
                permission_mode="dontAsk",
                source="builtin",
            )

        # 选择 LLM 客户端
        try:
            client = self._select_llm(p, definition)
        except ValueError as exc:
            return ToolResult(output=str(exc), is_error=True)

        # 判断是否后台运行
        is_background = is_fork or p.run_in_background or definition.background

        work_dir = self._parent_agent.work_dir
        task = p.prompt
        wt = None
        wt_name = ""
        if isolated:
            from likecc.worktree.integration import build_worktree_notice, generate_worktree_name

            wt_name = generate_worktree_name()
            try:
                wt = await self._worktree_manager.create(wt_name, "HEAD")
            except Exception as exc:
                return ToolResult(output=f"Failed to create worktree: {exc}", is_error=True)
            work_dir = wt.path
            notice = build_worktree_notice(self._parent_agent.work_dir, work_dir)
            task = notice + "\n\n" + p.prompt
            if is_fork:
                conversation.history[-1].content += "\n\n" + notice

        # 过滤工具（coordinator 模式可能缩减了注册表，这里用完整注册表）
        _base_registry = getattr(self._parent_agent, '_full_registry', None) or self._parent_agent.registry
        if is_fork:
            # Preserve the parent's schema prefix, with child-local stateful
            # tools. Runtime source checks prohibit recursive/control calls.
            filtered_registry = clone_agent_registry(
                self._parent_agent.registry, preserve_discovery=True,
            )
        else:
            filtered_registry = resolve_agent_tools(_base_registry, definition, is_background)

        # 为子 agent 创建权限检查器
        pm_str = definition.permission_mode
        pm_enum = getattr(
            PermissionMode,
            PERMISSION_MODE_MAP.get(pm_str, "DEFAULT"),
            PermissionMode.DEFAULT,
        )
        pm_enum = _bound_subagent_permission_mode(
            self._parent_agent.permission_mode,
            pm_enum,
        )
        checker = self._make_permission_checker(work_dir, pm_enum)

        # 创建子 agent
        sub_agent = AgentClass(
            client=client,
            registry=filtered_registry,
            protocol=self._parent_agent.protocol,
            work_dir=work_dir,
            max_iterations=definition.max_turns,
            permission_checker=checker,
            context_window=self._parent_agent.context_window,
            instructions_content=(self._parent_agent.instructions_content if is_fork else definition.system_prompt),
            hook_engine=self._parent_agent.hook_engine,
        )
        sub_agent.parent_id = self._parent_agent.agent_id
        sub_agent.trace_id = self._parent_agent.trace_id or self._parent_agent.agent_id

        # fork 子 agent 继承父 agent 的替换状态，确保共享的 tool_use_id 做出一致的
        # 决策——这样父子共享的 prompt cache 前缀才能保持字节级一致
        if is_fork:
            self._inherit_fork_context(sub_agent)
        else:
            sub_agent.query_source = "subagent"
        self._bind_child_tools(sub_agent)

        # 注册追踪节点
        trace_node = self._trace_manager.create(
            agent_type=definition.agent_type,
            parent_id=self._parent_agent.agent_id,
            trace_id=sub_agent.trace_id,
        )
        sub_agent.agent_id = trace_node.agent_id

        agent_name = p.name or p.subagent_type or f"agent-{trace_node.agent_id}"
        if is_background:
            if is_fork:
                sub_agent._fork_conversation = conversation
            task_id = self._task_manager.launch(
                agent=sub_agent,
                task="" if is_fork else task,
                name=agent_name,
                fork_conversation=conversation if is_fork else None,
            )
            return ToolResult(
                output=f"Sub-agent launched in background.\n"
                f"Task ID: {task_id}\n"
                f"Agent: {agent_name}\n"
                f"Type: {definition.agent_type}\n"
                + (f"Worktree preserved at {work_dir}\n" if wt is not None else "") +
                f"The system will notify automatically when it completes.\n"
                f"Do NOT wait, sleep, or poll. Report the task ID to the user and move on.",
            )

        # 前台同步执行
        try:
            if is_fork:
                result_text = await sub_agent.run_to_completion("", conversation)
            else:
                result_text = await sub_agent.run_to_completion(task)
        except Exception as e:
            self._trace_manager.complete(trace_node.agent_id, "failed")
            return ToolResult(
                output=f"Sub-agent failed: {e}", is_error=True
            )

        self._trace_manager.update(
            trace_node.agent_id,
            input_tokens=sub_agent.total_input_tokens,
            output_tokens=sub_agent.total_output_tokens,
        )
        self._trace_manager.complete(trace_node.agent_id, "completed")

        if wt is not None:
            cleanup = await self._worktree_manager.auto_cleanup(wt_name, wt.head_commit)
            if cleanup.kept:
                result_text = (result_text or "") + (
                    f"\n[Worktree preserved at {cleanup.path}, branch {cleanup.branch}]"
                )
        return ToolResult(output=result_text or "(sub-agent returned no output)")

    def _make_permission_checker(self, work_dir: str, mode: PermissionMode):
        from likecc.permissions import DangerousCommandDetector, PathSandbox, PermissionChecker, RuleEngine

        parent_checker = self._parent_agent.permission_checker
        detector = copy.deepcopy(parent_checker.detector) if parent_checker else DangerousCommandDetector()
        parent_rules = parent_checker.rule_engine if parent_checker else None
        rules = parent_rules.clone_for_subagent() if parent_rules else RuleEngine()
        return PermissionChecker(detector, PathSandbox(work_dir), rules, mode)

    def _inherit_fork_context(self, child: Agent) -> None:
        from likecc.context import clone_replacement_state

        child.query_source = "fork"
        child.system_prompt_override = getattr(self._parent_agent, "_last_system_prompt", None)
        child.coordinator_mode = self._parent_agent.coordinator_mode
        child.active_skills = dict(self._parent_agent.active_skills)
        child._skill_catalog = self._parent_agent._skill_catalog
        child._agent_catalog = self._parent_agent._agent_catalog
        child._agent_catalog_list = list(self._parent_agent._agent_catalog_list)
        child.replacement_state = clone_replacement_state(self._parent_agent.replacement_state)

    def _bind_child_tools(self, child: Agent) -> None:
        load_skill = child.registry.get("LoadSkill")
        if load_skill is not None and hasattr(load_skill, "set_agent"):
            load_skill.set_agent(child)
        if child.registry.get("Agent") is not None:
            child.registry.register(AgentTool(
                self._agent_loader, self._task_manager, self._trace_manager, child,
                enable_fork=self._enable_fork, provider_config=self._provider_config,
                worktree_manager=self._worktree_manager, team_manager=self._team_manager,
            ))

    def _validate_plan_subagent(self, params: AgentToolParams) -> ToolResult | None:
        if self._parent_agent.permission_mode != PermissionMode.PLAN:
            return None
        if params.team_name or params.isolation or not params.subagent_type:
            return self._plan_subagent_rejection()

        definition = self._agent_loader.get(params.subagent_type)
        if (
            definition is None
            or definition.source != "builtin"
            or definition.agent_type.strip().lower() not in {"explore", "plan"}
            or bool(definition.isolation)
        ):
            return self._plan_subagent_rejection()
        return None

    @staticmethod
    def _plan_subagent_rejection() -> ToolResult:
        return ToolResult(
            output=(
                "Plan mode only allows built-in Explore or Plan sub-agents "
                "without team or worktree isolation."
            ),
            is_error=True,
        )

    async def _execute_as_teammate(self, p: AgentToolParams) -> ToolResult:
        if self._team_manager is None:
            return ToolResult(output="TeamManager not configured.", is_error=True)
        if self._worktree_manager is None:
            return ToolResult(output="WorktreeManager not configured for team spawn.", is_error=True)

        from likecc.agents.fork import ForkError, build_forked_messages
        from likecc.agents.parser import AgentDef
        from likecc.agents.tool_filter import build_teammate_tools
        from likecc.agent import Agent as AgentClass
        from likecc.conversation import ConversationManager
        from likecc.permissions import (
            DangerousCommandDetector,
            PathSandbox,
            PermissionChecker,
            PermissionMode,
            RuleEngine,
        )
        from likecc.teams.models import BackendType, TeammateInfo
        from likecc.teams.registry import AgentNameRegistry

        team = self._team_manager.get_team(p.team_name)
        if team is None:
            return ToolResult(output=f"Team '{p.team_name}' not found. Create it first with TeamCreate.", is_error=True)

        base_name = p.name or p.subagent_type or "worker"
        existing_names = {m.name for m in team.members}
        teammate_name = base_name
        if teammate_name in existing_names:
            counter = 2
            while f"{base_name}-{counter}" in existing_names:
                counter += 1
            teammate_name = f"{base_name}-{counter}"

        # 1. 加载 agent 定义
        definition: AgentDef
        conversation: ConversationManager | None = None
        is_fork = False

        if p.subagent_type:
            defn = self._agent_loader.get(p.subagent_type)
            if defn is None:
                return ToolResult(
                    output=f"Unknown agent type: '{p.subagent_type}'. "
                    f"Available: {', '.join(t for t, _ in self._agent_loader.list_agents())}",
                    is_error=True,
                )
            definition = defn
        else:
            if self._enable_fork:
                try:
                    parent_conv = getattr(self._parent_agent, '_current_conversation', None)
                    if parent_conv is None:
                        return ToolResult(output="Cannot fork: no active conversation.", is_error=True)
                    conversation = build_forked_messages(parent_conv, p.prompt)
                    is_fork = True
                except ForkError as e:
                    return ToolResult(output=str(e), is_error=True)

            definition = AgentDef(
                agent_type="teammate",
                when_to_use="Team member",
                system_prompt="",
                disallowed_tools=[],
                model="inherit",
                max_turns=self._parent_agent.max_iterations,
                permission_mode="dontAsk",
                source="builtin",
            )

        try:
            client = self._select_llm(p, definition)
        except ValueError as exc:
            return ToolResult(output=str(exc), is_error=True)

        # 2. 创建 worktree
        wt_name = f"team-{p.team_name}/{teammate_name}"
        try:
            wt = await self._worktree_manager.create(wt_name, "HEAD")
        except Exception as e:
            return ToolResult(output=f"Failed to create worktree for teammate: {e}", is_error=True)

        # 4. 检测后端类型
        backend = self._team_manager.detect_backend()

        # 5. 构建队友的工具集
        trace_node = self._trace_manager.create(
            agent_type=definition.agent_type,
            parent_id=self._parent_agent.agent_id,
            trace_id=self._parent_agent.trace_id or self._parent_agent.agent_id,
        )
        agent_id = trace_node.agent_id

        _has_full = getattr(self._parent_agent, '_full_registry', None) is not None
        full_registry = getattr(self._parent_agent, '_full_registry', None) or self._parent_agent.registry
        _full_tools = [t.name for t in full_registry.list_tools()]
        log.info(
            "[teammate] has_full_registry=%s full_tools=%d names=%s backend=%s def_tools=%s def_disallowed=%s",
            _has_full, len(_full_tools), _full_tools,
            backend.value,
            getattr(definition, 'tools', []),
            getattr(definition, 'disallowed_tools', []),
        )
        teammate_registry = build_teammate_tools(
            parent_registry=full_registry,
            team_manager=self._team_manager,
            team_name=p.team_name,
            agent_id=agent_id,
            agent_name=teammate_name,
            backend_type=backend.value,
            definition=definition,
        )
        _tm_tools = [t.name for t in teammate_registry.list_tools()]
        log.info("[teammate] result_tools=%d names=%s", len(_tm_tools), _tm_tools)

        # 6. 创建子 agent 并附加队友专属指令
        instructions = (definition.system_prompt or "") + TEAMMATE_ADDENDUM

        teammate_mode = _bound_subagent_permission_mode(
            self._parent_agent.permission_mode,
            PermissionMode.DONT_ASK,
        )
        checker = self._make_permission_checker(wt.path, teammate_mode)

        sub_agent = AgentClass(
            client=client,
            registry=teammate_registry,
            protocol=self._parent_agent.protocol,
            work_dir=wt.path,
            max_iterations=definition.max_turns,
            permission_checker=checker,
            context_window=self._parent_agent.context_window,
            instructions_content=instructions,
            hook_engine=self._parent_agent.hook_engine,
        )
        sub_agent.parent_id = self._parent_agent.agent_id
        sub_agent.trace_id = self._parent_agent.trace_id or self._parent_agent.agent_id
        sub_agent.agent_id = agent_id
        sub_agent.team_name = p.team_name
        sub_agent._team_manager = self._team_manager
        if is_fork:
            self._inherit_fork_context(sub_agent)
            sub_agent._fork_conversation = conversation
        else:
            sub_agent.query_source = "subagent"
        self._bind_child_tools(sub_agent)

        # 7. 注册名称和成员信息
        AgentNameRegistry.instance().register(teammate_name, agent_id)

        member = TeammateInfo(
            name=teammate_name,
            agent_id=agent_id,
            agent_type=definition.agent_type,
            model=p.model or definition.model,
            worktree_path=wt.path,
            backend_type=backend.value,
            is_active=True,
        )
        self._team_manager.register_member(p.team_name, member)

        # 8. 按后端类型启动队友
        if backend in (BackendType.TMUX, BackendType.ITERM2):
            return self._spawn_pane_teammate(
                p, team, member, backend, wt, agent_id, teammate_name
            )

        # 进程内模式：直接用 task_manager 执行并通知结果
        task_id = self._task_manager.launch(
            agent=sub_agent,
            task="" if is_fork else p.prompt,
            name=teammate_name,
            fork_conversation=conversation if is_fork else None,
        )

        return ToolResult(
            output=(
                f"Teammate '{teammate_name}' spawned in team '{p.team_name}'.\n"
                f"Agent ID: {agent_id}\n"
                f"Backend: {backend.value}\n"
                f"Worktree: {wt.path}\n"
                f"Task ID: {task_id}\n"
                f"The system will notify when it completes."
            )
        )


    def _spawn_pane_teammate(
        self, p: Any, team: Any, member: Any, backend: Any, wt: Any,
        agent_id: str, teammate_name: str,
    ) -> ToolResult:
        from likecc.teams.models import BackendType

        mailbox = self._team_manager.get_mailbox(p.team_name)
        mailbox_dir = str(mailbox._base_dir) if mailbox else ""

        try:
            if backend == BackendType.TMUX:
                from likecc.teams.spawn_tmux import spawn_tmux_teammate
                pane_info = spawn_tmux_teammate(
                    team_name=p.team_name,
                    teammate_name=teammate_name,
                    worktree_path=wt.path,
                    prompt=p.prompt,
                    agent_type=p.subagent_type or "",
                    model=p.model or "",
                    mailbox_dir=mailbox_dir,
                )
                self._team_manager.register_pane_id(agent_id, pane_info.pane_id)
            elif backend == BackendType.ITERM2:
                from likecc.teams.spawn_iterm2 import spawn_iterm2_teammate
                pane_info = spawn_iterm2_teammate(
                    team_name=p.team_name,
                    teammate_name=teammate_name,
                    worktree_path=wt.path,
                    prompt=p.prompt,
                    agent_type=p.subagent_type or "",
                    model=p.model or "",
                    mailbox_dir=mailbox_dir,
                )
        except Exception as e:
            log.warning("Pane spawn failed, falling back to in-process: %s", e)
            return ToolResult(
                output=f"Pane spawn failed ({e}), teammate not started. Retry or set teammate_mode to in-process.",
                is_error=True,
            )

        return ToolResult(
            output=(
                f"Teammate '{teammate_name}' spawned in team '{p.team_name}'.\n"
                f"Agent ID: {agent_id}\n"
                f"Backend: {backend.value} (pane)\n"
                f"Worktree: {wt.path}\n"
                f"The teammate is running in an independent process."
            )
        )


    def _select_llm(
        self,
        params: AgentToolParams,
        definition: AgentDef,
    ) -> LLMClient:
        from likecc.agents.parser import AgentDef

        model_override = params.model or (
            definition.model if definition.model != "inherit" else None
        )

        if model_override and model_override != "inherit":
            client = self._create_client_for_model(model_override)
            if client is not None:
                return client
            raise ValueError(
                f"Cannot use requested model '{model_override}': provider configuration "
                "is missing or client creation failed."
            )

        return self._parent_agent.client


    async def _execute_with_worktree(self, p: AgentToolParams) -> ToolResult:
        if self._worktree_manager is None:
            return ToolResult(
                output="Worktree isolation is not available: WorktreeManager not configured.",
                is_error=True,
            )
        return await self._execute_subagent(p, isolated=True)

    def _create_client_for_model(self, model_alias: str) -> LLMClient | None:
        if self._provider_config is None:
            return None

        from likecc.client import create_client
        from likecc.config import ProviderConfig

        model_map = {
            "haiku": "claude-haiku-4-5-20251001",
            "sonnet": "claude-sonnet-4-6-20250514",
            "opus": "claude-opus-4-6-20250514",
        }
        model_id = model_map.get(model_alias, model_alias)

        config = ProviderConfig(
            name=f"sub-{model_alias}",
            protocol=self._provider_config.protocol,
            base_url=self._provider_config.base_url,
            model=model_id,
            api_key=self._provider_config.api_key,
            context_window=self._provider_config.context_window,
        )
        try:
            return create_client(config)
        except Exception:
            return None
