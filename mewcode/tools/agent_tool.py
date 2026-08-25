from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from mewcode.tools.base import Tool, ToolResult

if TYPE_CHECKING:
    from mewcode.agent import Agent
    from mewcode.agents.loader import AgentLoader
    from mewcode.agents.task_manager import TaskManager
    from mewcode.agents.trace import TraceManager
    from mewcode.client import LLMClient

log = logging.getLogger(__name__)


_PLAN_SUBAGENT_ALLOWED_TOOLS = frozenset(
    {"ReadFile", "Glob", "Grep", "Bash", "ToolSearch"}
)


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
            "communicate with each other via SendMessage. Without team_name the agent "
            "runs as a one-shot sub-agent that blocks and returns inline."
        ),
    )


PERMISSION_MODE_MAP = {
    "default": "DEFAULT",
    "acceptEdits": "ACCEPT_EDITS",
    "dontAsk": "DONT_ASK",
}


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

    def _child_runtime_kwargs(self, checker: Any, work_dir: str) -> dict[str, Any]:
        """Propagate the parent's *host-owned* execution boundary.

        A sub-agent is an execution detail of the same reviewed task.  Giving
        it a fresh default Agent used to drop the plan manifest, durable trace,
        and action journal, which made delegation a policy bypass.  Child
        completion is intentionally not given the task-level Evidence Gate;
        only the parent may claim that the whole RequirementContract passed.
        """

        from pathlib import Path

        from mewcode.execution import ExecutionContext, ExecutionGateway, RiskEngine

        parent_gateway = getattr(self._parent_agent, "execution_gateway", None)
        parent_context = getattr(self._parent_agent, "execution_context", None)
        active_context = getattr(self._parent_agent, "_active_execution_context", None)
        if callable(active_context):
            parent_context = active_context()

        child_context = parent_context
        if parent_context is not None:
            child_root = str(Path(work_dir).expanduser().resolve(strict=False))
            child_context = ExecutionContext(
                task_id=parent_context.task_id,
                cwd=child_root,
                plan_hash=parent_context.plan_hash,
                expected_pre_state_hash="delegated-workspace-not-attested",
                workspace_root=child_root,
                write_set=parent_context.write_set,
                commands=parent_context.commands,
                network_hosts=parent_context.network_hosts,
            )

        if isinstance(parent_gateway, ExecutionGateway):
            # A child-local checker preserves its PathSandbox while the trace
            # hook and journal remain the authoritative parent task stores.
            child_gateway = ExecutionGateway(
                permission_checker=checker,
                risk_engine=RiskEngine(
                    workspace_root=work_dir,
                    # Preserve every parent control-plane/reserved root and add
                    # the child's own .git/.eviforge defaults.
                    control_plane_roots=getattr(parent_gateway.risk_engine, "_reserved_paths", ()),
                    enforce_workspace_boundary=getattr(
                        parent_gateway.risk_engine, "enforce_workspace_boundary", False
                    ),
                    detector=getattr(parent_gateway.risk_engine, "detector", None),
                ),
                trace_hook=parent_gateway.trace_hook,
                approval_resolver=parent_gateway.approval_resolver,
                reject_unexpected_arguments=parent_gateway.reject_unexpected_arguments,
                execution_context=child_context,
                action_journal=parent_gateway.action_journal,
            )
        else:
            # Compatibility for tests/custom embedders.  A real production
            # Agent always owns ExecutionGateway via RuntimeBuilder.
            child_gateway = parent_gateway

        return {
            "execution_gateway": child_gateway,
            "execution_context": child_context,
            # Do not let a child advance or complete the task-level FSM/Gate.
            "task_runtime": None,
            "requirement_contract": None,
            "evolution_adapter": None,
        }

    def _planned_execution_active(self) -> bool:
        context = getattr(self._parent_agent, "execution_context", None)
        return context is not None and getattr(context, "plan_hash", "unplanned") != "unplanned"

    def _plan_mode_active(self) -> bool:
        return bool(getattr(self._parent_agent, "plan_mode", False))

    @staticmethod
    def _restrict_plan_subagent_tools(registry: Any) -> Any:
        """Return the host-owned read-only capability set for Plan delegates."""

        from mewcode.tools import ToolRegistry

        restricted = ToolRegistry()
        for tool in registry.list_tools():
            if tool.name in _PLAN_SUBAGENT_ALLOWED_TOOLS:
                restricted.register(tool)
        return restricted

    def _subagent_permission_mode(self, requested_mode: str) -> Any:
        from mewcode.permissions import PermissionMode

        if self._plan_mode_active():
            return PermissionMode.PLAN
        return getattr(
            PermissionMode,
            PERMISSION_MODE_MAP.get(requested_mode, "DEFAULT"),
            PermissionMode.DEFAULT,
        )

    @staticmethod
    async def _close_owned_agent_client(agent: Any) -> None:
        namespace = getattr(agent, "__dict__", None)
        client = namespace.get("_owned_llm_client") if isinstance(namespace, dict) else None
        if client is None:
            return
        namespace["_owned_llm_client"] = None
        try:
            from mewcode.client import aclose_client

            await aclose_client(client)
        except BaseException as exc:
            log.warning("Sub-agent client did not close cleanly: %s", exc)

    def _background_trace_callback(self, agent_id: str):
        """Return a terminal callback shared by Agent and Team launches."""

        def finalize_trace(background: Any) -> None:
            self._trace_manager.update(
                agent_id,
                input_tokens=background.progress.input_tokens,
                output_tokens=background.progress.output_tokens,
            )
            self._trace_manager.complete(agent_id, background.status)

        return finalize_trace

    async def execute(self, params: BaseModel) -> ToolResult:
        p: AgentToolParams = params  # type: ignore[assignment]
        plan_mode_active = self._plan_mode_active()

        if p.team_name:
            if plan_mode_active or self._planned_execution_active():
                return ToolResult(
                    output=(
                        "Persistent teammates are disabled during Plan or reviewed execution: "
                        "an external or independently scheduled teammate cannot "
                        "inherit the invocation-bound gateway safely. Use a regular "
                        "sub-agent or the typed DAG runner."
                    ),
                    is_error=True,
                )
            return await self._execute_as_teammate(p)

        if plan_mode_active and not p.subagent_type:
            return ToolResult(
                output=(
                    "Conversation forks are disabled in Plan mode. "
                    "Select a named sub-agent; it will be forced into a host-owned "
                    "read-only capability set."
                ),
                is_error=True,
            )

        isolation = ""
        if p.subagent_type:
            defn = self._agent_loader.get(p.subagent_type)
            if defn and defn.isolation:
                isolation = defn.isolation

        if plan_mode_active and isolation == "worktree":
            return ToolResult(
                output="Worktree-isolated sub-agents are disabled in Plan mode.",
                is_error=True,
            )
        if isolation == "worktree":
            return await self._execute_with_worktree(p)

        from mewcode.agents.fork import ForkError, build_forked_messages
        from mewcode.agents.parser import AgentDef
        from mewcode.agents.tool_filter import resolve_agent_tools
        from mewcode.agent import Agent as AgentClass
        from mewcode.conversation import ConversationManager
        from mewcode.permissions import (
            DangerousCommandDetector,
            PathSandbox,
            PermissionChecker,
            RuleEngine,
        )

        definition: AgentDef | None = None
        conversation: ConversationManager

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

        # 判断是否后台运行
        is_background = p.run_in_background or definition.background
        if self._enable_fork:
            is_background = True

        # 过滤工具（coordinator 模式可能缩减了注册表，这里用完整注册表）
        _base_registry = getattr(self._parent_agent, '_full_registry', None) or self._parent_agent.registry
        filtered_registry = resolve_agent_tools(
            _base_registry, definition, is_background
        )
        if plan_mode_active:
            filtered_registry = self._restrict_plan_subagent_tools(filtered_registry)

        # 为子 agent 创建权限检查器
        # Never trust a project/user Agent definition's permissionMode while
        # the parent is planning. PLAN denies writes and unsafe commands even
        # if the definition requested dontAsk.
        pm_enum = self._subagent_permission_mode(definition.permission_mode)
        checker = PermissionChecker(
            detector=DangerousCommandDetector(),
            sandbox=PathSandbox(self._parent_agent.work_dir),
            rule_engine=RuleEngine(),
            mode=pm_enum,
        )

        # A model override creates a child-owned transport. Inherited clients
        # remain parent-owned and are never closed by child cleanup.
        client, owns_client = self._select_llm_with_ownership(p, definition)
        try:
            sub_agent = AgentClass(
                client=client,
                registry=filtered_registry,
                protocol=self._parent_agent.protocol,
                work_dir=self._parent_agent.work_dir,
                max_iterations=definition.max_turns,
                permission_checker=checker,
                context_window=self._parent_agent.context_window,
                instructions_content=definition.system_prompt,
                hook_engine=self._parent_agent.hook_engine,
                **self._child_runtime_kwargs(checker, self._parent_agent.work_dir),
            )
        except BaseException:
            if owns_client:
                from mewcode.client import aclose_client

                await aclose_client(client)
            raise
        sub_agent._owned_llm_client = client if owns_client else None
        sub_agent.parent_id = self._parent_agent.agent_id
        sub_agent.trace_id = self._parent_agent.trace_id or self._parent_agent.agent_id

        # fork 子 agent 继承父 agent 的替换状态，确保共享的 tool_use_id 做出一致的
        # 决策——这样父子共享的 prompt cache 前缀才能保持字节级一致
        if p.subagent_type is None:
            from mewcode.context import clone_replacement_state
            sub_agent.replacement_state = clone_replacement_state(
                self._parent_agent.replacement_state
            )

        # 注册追踪节点
        trace_node = self._trace_manager.create(
            agent_type=definition.agent_type,
            parent_id=self._parent_agent.agent_id,
            trace_id=sub_agent.trace_id,
        )
        sub_agent.agent_id = trace_node.agent_id

        agent_name = p.name or p.subagent_type or f"agent-{trace_node.agent_id}"
        is_fork = p.subagent_type is None

        if is_background:
            if is_fork:
                sub_agent._fork_conversation = conversation

            try:
                task_id = self._task_manager.launch(
                    agent=sub_agent,
                    task="" if is_fork else p.prompt,
                    name=agent_name,
                    fork_conversation=conversation if is_fork else None,
                    on_terminal=self._background_trace_callback(
                        trace_node.agent_id
                    ),
                )
            except BaseException:
                await self._close_owned_agent_client(sub_agent)
                raise
            return ToolResult(
                output=f"Sub-agent launched in background.\n"
                f"Task ID: {task_id}\n"
                f"Agent: {agent_name}\n"
                f"Type: {definition.agent_type}\n"
                f"The system will notify automatically when it completes.\n"
                f"Do NOT wait, sleep, or poll. Report the task ID to the user and move on.",
            )

        # 前台同步执行
        try:
            try:
                if is_fork:
                    result_text = await sub_agent.run_to_completion("", conversation)
                else:
                    result_text = await sub_agent.run_to_completion(p.prompt)
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

            return ToolResult(output=result_text or "(sub-agent returned no output)")
        finally:
            await self._close_owned_agent_client(sub_agent)

    async def _execute_as_teammate(self, p: AgentToolParams) -> ToolResult:
        if self._team_manager is None:
            return ToolResult(output="TeamManager not configured.", is_error=True)
        if self._worktree_manager is None:
            return ToolResult(output="WorktreeManager not configured for team spawn.", is_error=True)

        from mewcode.agents.fork import ForkError, build_forked_messages
        from mewcode.agents.parser import AgentDef
        from mewcode.agents.tool_filter import build_teammate_tools
        from mewcode.agent import Agent as AgentClass
        from mewcode.conversation import ConversationManager
        from mewcode.permissions import (
            DangerousCommandDetector,
            PathSandbox,
            PermissionChecker,
            PermissionMode,
            RuleEngine,
        )
        from mewcode.teams.models import BackendType, TeammateInfo
        from mewcode.teams.registry import AgentNameRegistry

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

        # 2. 创建 worktree
        wt_name = f"team-{p.team_name}/{teammate_name}"
        try:
            wt = await self._worktree_manager.create(wt_name, "HEAD")
        except Exception as e:
            return ToolResult(output=f"Failed to create worktree for teammate: {e}", is_error=True)

        # 3. 选择 LLM
        client, owns_client = self._select_llm_with_ownership(p, definition)

        try:
            # 4. 检测后端类型
            backend = self._team_manager.detect_backend()

            # 5. 构建队友的工具集
            trace_node = self._trace_manager.create(
                agent_type=definition.agent_type,
                parent_id=self._parent_agent.agent_id,
                trace_id=self._parent_agent.trace_id or self._parent_agent.agent_id,
            )
        except BaseException:
            if owns_client:
                from mewcode.client import aclose_client

                await aclose_client(client)
            raise
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

        checker = PermissionChecker(
            detector=DangerousCommandDetector(),
            sandbox=PathSandbox(wt.path),
            rule_engine=RuleEngine(),
            mode=PermissionMode.DONT_ASK,
        )

        try:
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
                **self._child_runtime_kwargs(checker, wt.path),
            )
        except BaseException:
            if owns_client:
                from mewcode.client import aclose_client

                await aclose_client(client)
            raise
        sub_agent.parent_id = self._parent_agent.agent_id
        sub_agent.trace_id = self._parent_agent.trace_id or self._parent_agent.agent_id
        sub_agent.agent_id = agent_id
        sub_agent.team_name = p.team_name
        sub_agent._team_manager = self._team_manager
        sub_agent._owned_llm_client = client if owns_client else None

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
            try:
                return self._spawn_pane_teammate(
                    p, team, member, backend, wt, agent_id, teammate_name
                )
            finally:
                # The pane uses its own process/config. The in-process client
                # selected above is unused and remains this tool's resource.
                await self._close_owned_agent_client(sub_agent)

        # 进程内模式：直接用 task_manager 执行并通知结果
        try:
            task_id = self._task_manager.launch(
                agent=sub_agent,
                task="" if is_fork else p.prompt,
                name=teammate_name,
                fork_conversation=conversation if is_fork else None,
                on_terminal=self._background_trace_callback(agent_id),
            )
        except BaseException:
            await self._close_owned_agent_client(sub_agent)
            raise

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
        from mewcode.teams.models import BackendType

        mailbox = self._team_manager.get_mailbox(p.team_name)
        mailbox_dir = str(mailbox._base_dir) if mailbox else ""

        try:
            if backend == BackendType.TMUX:
                from mewcode.teams.spawn_tmux import spawn_tmux_teammate
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
                from mewcode.teams.spawn_iterm2 import spawn_iterm2_teammate
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
        client, _owns_client = self._select_llm_with_ownership(params, definition)
        return client

    def _select_llm_with_ownership(
        self,
        params: AgentToolParams,
        definition: AgentDef,
    ) -> tuple[LLMClient, bool]:
        from mewcode.agents.parser import AgentDef

        model_override = params.model or (
            definition.model if definition.model != "inherit" else None
        )

        if model_override and model_override != "inherit":
            client = self._create_client_for_model(model_override)
            if client is not None:
                return client, True

        return self._parent_agent.client, False


    async def _execute_with_worktree(self, p: AgentToolParams) -> ToolResult:
        if self._worktree_manager is None:
            return ToolResult(
                output="Worktree isolation is not available: WorktreeManager not configured.",
                is_error=True,
            )

        from mewcode.agents.parser import AgentDef
        from mewcode.agents.tool_filter import resolve_agent_tools
        from mewcode.agent import Agent as AgentClass
        from mewcode.conversation import ConversationManager
        from mewcode.permissions import (
            DangerousCommandDetector,
            PathSandbox,
            PermissionChecker,
            PermissionMode,
            RuleEngine,
        )
        from mewcode.worktree.integration import (
            build_worktree_notice,
            generate_worktree_name,
        )

        definition: AgentDef | None = None
        if p.subagent_type:
            definition = self._agent_loader.get(p.subagent_type)
            if definition is None:
                return ToolResult(
                    output=f"Unknown agent type: '{p.subagent_type}'. "
                    f"Available types: {', '.join(t for t, _ in self._agent_loader.list_agents())}",
                    is_error=True,
                )
        else:
            definition = AgentDef(
                agent_type="worktree-agent",
                when_to_use="Isolated worktree agent",
                system_prompt="",
                disallowed_tools=[],
                model="inherit",
                max_turns=self._parent_agent.max_iterations,
                permission_mode="dontAsk",
                source="builtin",
            )

        wt_name = generate_worktree_name()
        try:
            wt = await self._worktree_manager.create(wt_name, "HEAD")
        except Exception as e:
            return ToolResult(
                output=f"Failed to create worktree: {e}",
                is_error=True,
            )

        notice = build_worktree_notice(self._parent_agent.work_dir, wt.path)
        task = notice + "\n\n" + p.prompt

        client, owns_client = self._select_llm_with_ownership(p, definition)

        _base_registry = getattr(self._parent_agent, '_full_registry', None) or self._parent_agent.registry
        filtered_registry = resolve_agent_tools(
            _base_registry, definition, False
        )

        pm_str = definition.permission_mode
        pm_enum = getattr(
            PermissionMode,
            PERMISSION_MODE_MAP.get(pm_str, "DEFAULT"),
            PermissionMode.DEFAULT,
        )
        checker = PermissionChecker(
            detector=DangerousCommandDetector(),
            sandbox=PathSandbox(wt.path),
            rule_engine=RuleEngine(),
            mode=pm_enum,
        )

        try:
            sub_agent = AgentClass(
                client=client,
                registry=filtered_registry,
                protocol=self._parent_agent.protocol,
                work_dir=wt.path,
                max_iterations=definition.max_turns,
                permission_checker=checker,
                context_window=self._parent_agent.context_window,
                instructions_content=definition.system_prompt,
                hook_engine=self._parent_agent.hook_engine,
                **self._child_runtime_kwargs(checker, wt.path),
            )
        except BaseException:
            if owns_client:
                from mewcode.client import aclose_client

                await aclose_client(client)
            raise
        sub_agent._owned_llm_client = client if owns_client else None
        sub_agent.parent_id = self._parent_agent.agent_id
        sub_agent.trace_id = self._parent_agent.trace_id or self._parent_agent.agent_id

        trace_node = self._trace_manager.create(
            agent_type=definition.agent_type,
            parent_id=self._parent_agent.agent_id,
            trace_id=sub_agent.trace_id,
        )
        sub_agent.agent_id = trace_node.agent_id

        try:
            try:
                result_text = await sub_agent.run_to_completion(task)
            except Exception as e:
                self._trace_manager.complete(trace_node.agent_id, "failed")
                return ToolResult(
                    output=f"Sub-agent in worktree failed: {e}",
                    is_error=True,
                )

            self._trace_manager.update(
                trace_node.agent_id,
                input_tokens=sub_agent.total_input_tokens,
                output_tokens=sub_agent.total_output_tokens,
            )
            self._trace_manager.complete(trace_node.agent_id, "completed")

            cleanup = await self._worktree_manager.auto_cleanup(wt_name, wt.head_commit)
            if cleanup.kept:
                result_text = (result_text or "") + (
                    f"\n[Worktree preserved at {cleanup.path}, branch {cleanup.branch}]"
                )

            return ToolResult(output=result_text or "(sub-agent returned no output)")
        finally:
            await self._close_owned_agent_client(sub_agent)


    def _create_client_for_model(self, model_alias: str) -> LLMClient | None:
        if self._provider_config is None:
            return None

        from mewcode.client import create_client
        from mewcode.config import ProviderConfig

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
            api_key_env=self._provider_config.api_key_env,
            auth=self._provider_config.auth,
            thinking=self._provider_config.thinking,
            context_window=self._provider_config.context_window,
            max_output_tokens=self._provider_config.max_output_tokens,
        )
        try:
            return create_client(config)
        except Exception:
            return None
