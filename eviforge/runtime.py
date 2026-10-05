"""Shared service assembly used by the Textual and automation entry points."""
from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any, Callable

from eviforge.config import AppConfig, ProviderConfig
from eviforge.lifecycle import Lifecycle, close_resource
from eviforge.permissions import PermissionMode


class RuntimeServices:
    @classmethod
    def create(
        cls, config: AppConfig, provider: ProviderConfig, *, client: Any,
        work_dir: str, permission_mode: PermissionMode = PermissionMode.DEFAULT,
        interactive: bool = False, registry: Any = None, task_manager: Any = None,
        trace_manager: Any = None, hook_engine: Any = None,
        on_work_dir_change: Callable[[str], None] | None = None,
        resume_session: str | None = None,
    ) -> RuntimeServices:
        from eviforge.agent import Agent
        from eviforge.agents.loader import AgentLoader
        from eviforge.agents.task_manager import TaskManager
        from eviforge.agents.trace import TraceManager
        from eviforge.conversation import ConversationManager
        from eviforge.filehistory import FileHistory
        from eviforge.governance import GovernanceService
        from eviforge.memory import MemoryManager, SessionManager, load_instructions
        from eviforge.memory.recorder import SessionRecorder
        from eviforge.mcp import MCPManager
        from eviforge.permissions import PermissionChecker, DangerousCommandDetector, PathSandbox, RuleEngine
        from eviforge.planning import PlanService, PlanState
        from eviforge.skills.executor import SkillExecutor
        from eviforge.skills.loader import SkillLoader
        from eviforge.teams.manager import TeamManager
        from eviforge.tools import create_default_registry
        from eviforge.tools.agent_tool import AgentTool
        from eviforge.tools.ask_user import AskUserTool
        from eviforge.tools.enter_worktree import EnterWorktreeTool
        from eviforge.tools.exit_worktree import ExitWorktreeTool
        from eviforge.tools.exit_plan_mode import ExitPlanModeTool
        from eviforge.tools.http_request import HttpRequest
        from eviforge.tools.impl.tool_search import ToolSearchTool
        from eviforge.tools.load_skill import LoadSkill
        from eviforge.tools.synthetic_output import SyntheticOutputTool
        from eviforge.tools.team_create import TeamCreateTool
        from eviforge.tools.team_delete import TeamDeleteTool
        from eviforge.worktree import WorktreeManager

        self = cls()
        self.lifecycle = Lifecycle()
        self.closed = False
        self._close_lock = asyncio.Lock()
        self.config = config
        self.provider = provider
        self.client = client
        self.work_dir = str(Path(work_dir).resolve())
        self.interactive = interactive
        self.hook_engine = hook_engine
        self.registry = registry if registry is not None else create_default_registry()
        self.task_manager = task_manager if task_manager is not None else TaskManager()
        self.trace_manager = trace_manager if trace_manager is not None else TraceManager()
        self.governance = GovernanceService(self.work_dir)
        self.memory_manager = MemoryManager(self.work_dir, governance=self.governance)
        self.session_manager = SessionManager(self.work_dir)
        self.conversation = ConversationManager()
        if resume_session:
            import re
            if not re.fullmatch(r"[A-Za-z0-9_-]+", resume_session):
                raise ValueError("Invalid session identifier")
            restored = self.session_manager.resume(resume_session)
            if restored is None:
                raise ValueError(f"Session not found: {resume_session}")
            self.session = restored.session
            self.conversation.replace_history(restored.messages)
        else:
            self.session = self.session_manager.create()
        self.session_recorder = SessionRecorder(self.session, self.conversation.history)
        self.file_history = FileHistory(self.work_dir, self.session.session_id)
        home = Path.home()
        self.checker = PermissionChecker(
            detector=DangerousCommandDetector(), sandbox=PathSandbox(self.work_dir),
            rule_engine=RuleEngine(
                user_rules_path=home / ".eviforge" / "permissions.yaml",
                project_rules_path=Path(self.work_dir) / ".eviforge" / "permissions.yaml",
                local_rules_path=Path(self.work_dir) / ".eviforge" / "permissions.local.yaml",
            ), mode=permission_mode,
        )
        self.instructions = load_instructions(self.work_dir)
        self.agent = Agent(client=client, registry=self.registry, protocol=provider.protocol,
                           work_dir=self.work_dir, permission_checker=self.checker,
                           context_window=provider.get_context_window(),
                           instructions_content=self.instructions,
                           memory_manager=self.memory_manager, hook_engine=hook_engine)
        self.agent.session_id = self.session.session_id
        self.agent.trace_id = uuid.uuid4().hex
        self.agent.file_history = self.file_history
        self.agent.runtime = self
        self.plan_service = PlanService(self.work_dir, tool_resolver=self.registry.get)
        self.plan_service.bind_agent(self.agent)
        for tool in self.registry.list_tools():
            if hasattr(tool, "file_history"):
                tool.file_history = self.file_history
        self.load_skill_tool = LoadSkill()
        self.registry.register(self.load_skill_tool)
        self.registry.register(ToolSearchTool(self.registry, protocol=provider.protocol))
        self.registry.register(AskUserTool())
        self.registry.register(HttpRequest())
        if not interactive:
            self.registry.disable("AskUserQuestion")
        self.exit_plan_tool = ExitPlanModeTool()
        self.exit_plan_tool._is_plan_mode = lambda: self.agent.plan_mode
        self.exit_plan_tool._plan_exists = lambda: self.agent._get_plan_path().exists()
        def submit_plan(actions=None):
            draft_path = self.agent._get_plan_path()
            content = draft_path.read_text(encoding="utf-8")
            plan = self.plan_service.current_plan(self.agent)
            if plan is None:
                plan = self.plan_service.create(self.agent.session_id, self.agent.turn_id,
                    content, actions=actions, plan_path=draft_path)
                self.plan_service.bind_draft(self.agent, plan.plan_id)
            elif plan.state != PlanState.DRAFT:
                raise ValueError("Only the current draft can be submitted")
            return self.plan_service.submit(plan.plan_id, content=content, actions=actions)
        self.exit_plan_tool._submit_plan = submit_plan
        self.registry.register(self.exit_plan_tool)
        self.skill_loader = SkillLoader(self.work_dir, governance=self.governance)
        self.skill_loader.load_all()
        self.load_skill_tool.set_loader(self.skill_loader)
        self.load_skill_tool.set_agent(self.agent)
        self.agent.skill_loader = self.skill_loader
        self.skill_executor = SkillExecutor(agent=self.agent, client=client, protocol=provider.protocol)
        self.refresh_skill_catalog()

        def sync_work_dir(path: str) -> None:
            plan = self.plan_service.current_plan(self.agent)
            if plan is not None:
                self.plan_service.invalidate(plan.plan_id, "work directory changed")
            self.registry.clear_file_caches()
            self.agent.work_dir = path
            self.checker.sandbox.set_project_root(path)
            if on_work_dir_change:
                on_work_dir_change(path)

        self.worktree_manager = WorktreeManager(
            repo_root=self.work_dir, symlink_directories=config.worktree.symlink_directories,
            on_work_dir_change=sync_work_dir,
        )
        self.worktree_manager.restore_session()
        self.registry.register(EnterWorktreeTool(worktree_manager=self.worktree_manager))
        self.registry.register(ExitWorktreeTool(worktree_manager=self.worktree_manager))
        self.agent_loader = AgentLoader(self.work_dir, enable_verification=config.enable_verification_agent)
        self.agent_loader.load_all()
        self.team_manager = TeamManager(worktree_manager=self.worktree_manager, trace_manager=self.trace_manager)
        self.registry.register(AgentTool(
            agent_loader=self.agent_loader, task_manager=self.task_manager,
            trace_manager=self.trace_manager, parent_agent=self.agent,
            enable_fork=config.enable_fork, provider_config=provider,
            worktree_manager=self.worktree_manager, team_manager=self.team_manager,
        ))
        self.registry.register(TeamCreateTool(
            team_manager=self.team_manager, parent_agent=self.agent,
            teammate_mode=config.teammate_mode if interactive else "in-process",
            is_interactive=interactive, enable_coordinator_mode=config.enable_coordinator_mode,
        ))
        self.registry.register(TeamDeleteTool(team_manager=self.team_manager, parent_agent=self.agent))
        self.registry.register(SyntheticOutputTool())
        self.agent._team_manager = self.team_manager
        catalog = self.agent_loader.list_agents()
        self.agent.set_agent_catalog(
            "Available Agent types:\n" + "\n".join(f"- {name}: {description}" for name, description in catalog)
            + "\nDefined Agents run synchronously unless background is requested. Forks run in the background; completion notifications are delivered automatically.",
            catalog_list=catalog,
        )
        self.agent.notification_fn = self.drain_notifications
        self.mcp_manager = MCPManager()
        self.mcp_manager.load_configs(config.mcp_servers)
        self._mcp_started = False
        if hook_engine is not None:
            hook_engine.agent_executor = self.execute_hook_agent
        return self

    def begin_turn(self, *, continue_plan: bool = False) -> str:
        from eviforge.planning import PlanState
        turn = self.plan_service.begin_turn(self.agent, continue_plan=continue_plan)
        if not self.agent.plan_mode and not continue_plan:
            self.plan_service.clear_agent(self.agent)
        if self.agent.plan_mode and not continue_plan:
            old = self.plan_service.current_plan(self.agent)
            if old is not None:
                self.plan_service.clear_agent(self.agent)
            path = self.agent._get_plan_path()
            draft = self.plan_service.create(self.agent.session_id, turn,
                path.read_text(encoding="utf-8") if path.exists() else "", plan_path=path)
            self.plan_service.bind_draft(self.agent, draft.plan_id)
            self.checker.plan_file_path = str(path)
        return turn

    async def execute_hook_agent(self, action: Any, context: Any) -> Any:
        from eviforge.agent import Agent
        from eviforge.hooks.models import ActionResult
        from eviforge.tools import create_default_registry
        from eviforge.permissions import PermissionChecker, DangerousCommandDetector, PathSandbox, RuleEngine
        registry = create_default_registry()
        for tool in registry.list_tools():
            if tool.name not in {"ReadFile", "Glob", "Grep"}:
                registry.disable(tool.name)
        checker = PermissionChecker(detector=DangerousCommandDetector(),
            sandbox=PathSandbox(self.agent.work_dir), rule_engine=RuleEngine(), mode=PermissionMode.DONT_ASK)
        child = Agent(self.client, registry, self.provider.protocol, work_dir=self.agent.work_dir,
            permission_checker=checker, max_iterations=10, context_window=self.agent.context_window)
        self.agent.bind_child(child)
        try:
            async with asyncio.timeout(action.timeout):
                output = await child.run_to_completion(context.expand(action.prompt))
            return ActionResult(output=output, success=child.last_run_status == "success")
        finally:
            await child.cancel_background()

    def refresh_skill_catalog(self) -> None:
        catalog = self.skill_loader.get_catalog()
        self.agent.set_skill_catalog(
            "Available Skills:\n" + "\n".join(f"- {name}: {description}" for name, description in catalog)
            + "\nCall LoadSkill to activate a matching Skill."
        )

    async def start_mcp(self) -> list[str]:
        if self._mcp_started:
            return []
        errors = await self.mcp_manager.register_all_tools(self.registry)
        self._mcp_started = True
        return errors

    def drain_notifications(self) -> list[str]:
        notes = [
            f"<task-notification>\n<task_id>{task.id}</task_id>\n<status>{task.status}</status>\n<result>{task.result}</result>\n</task-notification>"
            for task in self.task_manager.poll_completed()
        ]
        notes.extend(self.team_manager.drain_lead_mailbox())
        return notes

    def pending_workers(self) -> tuple[asyncio.Task, ...]:
        return self.task_manager.pending_tasks() + self.team_manager.pending_tasks()

    async def wait(self, timeout: float | None = None) -> None:
        async with asyncio.timeout(timeout):
            pending = self.pending_workers()
            if pending:
                await asyncio.wait(pending)
            await self.agent.wait_background()
            await self.lifecycle.wait()
            if self.hook_engine is not None:
                await self.hook_engine.wait_for_background_hooks()

    async def cancel(self) -> None:
        errors = []
        for cancel in (self.task_manager.cancel_all, self.team_manager.cancel_all,
                       self.agent.cancel_background, self.lifecycle.cancel):
            try:
                await cancel()
            except Exception as error:
                errors.append(error)
        if errors:
            raise errors[0]

    async def close(self) -> None:
        async with self._close_lock:
            await self._close_owned()

    async def _close_owned(self) -> None:
        if self.closed:
            return
        try:
            await self.cancel()
        finally:
            try:
                await self.mcp_manager.shutdown()
            finally:
                try:
                    clients = {id(self.client): self.client}
                    def collect(agent):
                        clients[id(agent.client)] = agent.client
                        for child in agent.children:
                            collect(child)
                    collect(self.agent)
                    outcomes = await asyncio.gather(*(close_resource(client) for client in clients.values()), return_exceptions=True)
                    for outcome in outcomes:
                        if isinstance(outcome, BaseException):
                            raise outcome
                finally:
                    self.session.close()
                    await close_resource(self.governance)
                    self.closed = True
