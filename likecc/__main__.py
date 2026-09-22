
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from likecc.config import ConfigError, load_config
from likecc.hooks import HookConfigError, HookContext, HookEngine, load_hooks
from likecc.permissions import PermissionMode


HEADLESS_BACKGROUND_TIMEOUT = 180.0
HEADLESS_NOTIFICATION_INTERVAL = 0.1


def main() -> None:
    # 先确保 .likecc/ 目录存在，否则下面写 debug.log 会因目录不存在而崩溃
    Path(".likecc").mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(message)s",
        filename=".likecc/debug.log",
        filemode="w",
    )

    parser = argparse.ArgumentParser(prog="likecc", description="LikeCC AI coding assistant")
    parser.add_argument(
        "--mode",
        choices=[m.value for m in PermissionMode],
        default=None,
        help="Permission mode (overrides config.yaml)",
    )
    parser.add_argument(
        "-p",
        metavar="PROMPT",
        default=None,
        help="Run non-interactively: execute the prompt and print the result to stdout",
    )
    args = parser.parse_args()

    try:
        config = load_config()
    except ConfigError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    mode_str = args.mode if args.mode else config.permission_mode
    permission_mode = PermissionMode(mode_str)

    try:
        hooks = load_hooks(config.raw_hooks)
    except HookConfigError as e:
        print(f"Hook config error: {e}", file=sys.stderr)
        sys.exit(1)

    hook_engine = HookEngine(hooks) if hooks else None

    if args.p is not None:
        asyncio.run(
            _run_prompt_with_hook_cleanup(
                config, permission_mode, hook_engine, args.p
            )
        )
        return

    from likecc.app import LikeCCApp
    from likecc.driver import NoAltScreenDriver

    app = LikeCCApp(
        providers=config.providers,
        permission_mode=permission_mode,
        mcp_servers=config.mcp_servers,
        hook_engine=hook_engine,
        enable_fork=config.enable_fork,
        enable_verification_agent=config.enable_verification_agent,
        worktree_config=config.worktree,
        teammate_mode=config.teammate_mode,
        enable_coordinator_mode=config.enable_coordinator_mode,
        driver_class=NoAltScreenDriver,
    )
    app.run()


async def _shutdown_hook_engine(hook_engine: HookEngine | None) -> None:
    if hook_engine is None:
        return

    async def _run_shutdown_hooks() -> None:
        await hook_engine.run_hooks(
            "shutdown", HookContext(event_name="shutdown", work_dir=os.getcwd())
        )
        await hook_engine.wait_for_background_hooks()

    try:
        await asyncio.wait_for(_run_shutdown_hooks(), timeout=3.0)
    except asyncio.TimeoutError:
        logging.warning("Timed out while shutting down hooks")
    except Exception:
        logging.exception("Hook shutdown failed")
    finally:
        await hook_engine.cancel_background_hooks()


async def _run_prompt_with_hook_cleanup(
    config, permission_mode, hook_engine: HookEngine | None, prompt: str
) -> None:
    try:
        if hook_engine is not None:
            await hook_engine.run_hooks(
                "startup", HookContext(event_name="startup", work_dir=os.getcwd())
            )
        await _run_prompt(config, permission_mode, hook_engine, prompt)
    finally:
        await _shutdown_hook_engine(hook_engine)


async def _run_prompt(config, permission_mode, hook_engine, prompt: str) -> None:
    from likecc.agent import Agent
    from likecc.client import create_client, resolve_context_window
    from likecc.conversation import ConversationManager
    from likecc.memory.instructions import load_instructions
    from likecc.permissions import (
        DangerousCommandDetector,
        PathSandbox,
        PermissionChecker,
        RuleEngine,
    )
    from likecc.tools import create_default_registry
    from likecc.agents.loader import AgentLoader
    from likecc.agents.task_manager import TaskManager
    from likecc.agents.trace import TraceManager
    from likecc.tools.agent_tool import AgentTool
    from likecc.tools.enter_worktree import EnterWorktreeTool
    from likecc.tools.exit_worktree import ExitWorktreeTool
    from likecc.tools.impl.tool_search import ToolSearchTool
    from likecc.teams.manager import TeamManager
    from likecc.tools.team_create import TeamCreateTool
    from likecc.tools.team_delete import TeamDeleteTool
    from likecc.worktree import WorktreeManager
    from likecc.config import WorktreeConfig

    provider = config.providers[0]
    client = create_client(provider)
    # 第 2 层：尽力从 provider 自动拉取模型的 context window（缓存在 provider 上）。
    # 不会抛异常或阻塞启动；失败则退化到映射表。
    await resolve_context_window(provider)
    work_dir = os.getcwd()
    home = Path.home()

    checker = PermissionChecker(
        detector=DangerousCommandDetector(),
        sandbox=PathSandbox(work_dir),
        rule_engine=RuleEngine(
            user_rules_path=home / ".likecc" / "permissions.yaml",
            project_rules_path=Path(work_dir) / ".likecc" / "permissions.yaml",
            local_rules_path=Path(work_dir) / ".likecc" / "permissions.local.yaml",
        ),
        mode=permission_mode,
    )

    instructions = load_instructions(work_dir)
    registry = create_default_registry()
    registry.register(ToolSearchTool(registry, protocol=provider.protocol))

    agent = Agent(
        client=client,
        registry=registry,
        protocol=provider.protocol,
        work_dir=work_dir,
        permission_checker=checker,
        context_window=provider.get_context_window(),
        instructions_content=instructions,
        hook_engine=hook_engine,
    )

    def sync_worktree_context(path: str) -> None:
        registry.clear_file_caches()
        agent.work_dir = path
        checker.sandbox.set_project_root(path)

    wt_cfg = config.worktree or WorktreeConfig()
    wt_manager = WorktreeManager(
        repo_root=work_dir,
        symlink_directories=wt_cfg.symlink_directories,
        on_work_dir_change=sync_worktree_context,
    )
    wt_manager.restore_session()
    registry.register(EnterWorktreeTool(worktree_manager=wt_manager))
    registry.register(ExitWorktreeTool(worktree_manager=wt_manager))
    trace_manager = TraceManager()
    task_manager = TaskManager()
    agent_loader = AgentLoader(work_dir, enable_verification=config.enable_verification_agent)
    agent_loader.load_all()
    team_manager = TeamManager(worktree_manager=wt_manager, trace_manager=trace_manager)

    agent_tool = AgentTool(
        agent_loader=agent_loader,
        task_manager=task_manager,
        trace_manager=trace_manager,
        parent_agent=agent,
        enable_fork=config.enable_fork,
        provider_config=provider,
        worktree_manager=wt_manager,
        team_manager=team_manager,
    )
    registry.register(agent_tool)
    registry.register(TeamCreateTool(
        team_manager=team_manager,
        parent_agent=agent,
        teammate_mode="in-process",
        is_interactive=False,
        enable_coordinator_mode=config.enable_coordinator_mode,
    ))
    registry.register(TeamDeleteTool(team_manager=team_manager, parent_agent=agent))

    def drain_notifications() -> list[str]:
        notes: list[str] = []
        for t in task_manager.poll_completed():
            notes.append(
                f"<task-notification>\n<task_id>{t.id}</task_id>\n"
                f"<status>{t.status}</status>\n<result>{t.result}</result>\n"
                f"</task-notification>"
            )
        notes.extend(team_manager.drain_lead_mailbox())
        return notes

    agent.notification_fn = drain_notifications

    try:
        conv = ConversationManager()
        last_result = await agent.run_to_completion(prompt, conv)
        print(last_result, flush=True)

        # Ordinary background sub-agents need the same delivery loop as team
        # members. Bound the whole follow-up phase, including new model turns.
        async with asyncio.timeout(HEADLESS_BACKGROUND_TIMEOUT):
            while True:
                notes = drain_notifications()
                if notes:
                    for note in notes:
                        conv.add_system_reminder(note)
                    last_result = await agent.run_to_completion(
                        "Background agent notifications received. Process them and continue.",
                        conv,
                    )
                    print(last_result, flush=True)
                    await asyncio.sleep(0)
                    continue

                pending = [
                    task for task in task_manager._async_tasks.values()
                    if not task.done()
                ]
                if not pending:
                    break
                await asyncio.wait(
                    pending,
                    timeout=HEADLESS_NOTIFICATION_INTERVAL,
                    return_when=asyncio.FIRST_COMPLETED,
                )
    finally:
        # Keep cancellation and exception paths in this event loop so a CLI
        # return cannot leave workers running or lose their finalizers.
        pending = list(task_manager._async_tasks.items())
        for _, task in pending:
            if not task.done():
                task.cancel()
        if pending:
            await asyncio.gather(*(task for _, task in pending), return_exceptions=True)
        for task_id, task in pending:
            # A task cancelled before its first scheduling never enters the
            # worker's try/finally, so finish that bookkeeping here.
            if task.cancelled() and task_id in task_manager._async_tasks:
                task_manager._async_tasks.pop(task_id, None)
                background = task_manager.get(task_id)
                if background is not None:
                    background.status = "cancelled"
                    background.result = "Task was cancelled"
                    background.end_time = asyncio.get_running_loop().time()


if __name__ == "__main__":
    main()
