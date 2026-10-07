from __future__ import annotations

import argparse
import asyncio
import inspect
import logging
import os
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from eviforge.automation import EventJournal, RunResult, write_result, EXIT_CODES
from eviforge.config import ConfigError, load_config
from eviforge.hooks import HookConfigError, HookContext, HookEngine, create_hook_engine
from eviforge.permissions import PermissionMode

HEADLESS_BACKGROUND_TIMEOUT = 180.0
HEADLESS_NOTIFICATION_INTERVAL = 0.1


@dataclass
class AutomationOptions:
    output_format: str = "text"
    resume_session: str | None = None
    provider_timeout: float = 120.0
    max_attempts: int = 3
    result: RunResult | None = None
    approved_plan: str | None = None
    plan_hash: str | None = None
    approval_ttl: float = 300
    emitted_final: bool = False


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="eviforge", description="EviForge: verifiable, recoverable terminal coding agent")
    parser.add_argument("--mode", choices=[mode.value for mode in PermissionMode], default=None)
    parser.add_argument("-p", metavar="PROMPT", default=None, help="Run a prompt non-interactively")
    parser.add_argument("--output", choices=["text", "json", "jsonl"], default="text", help="Headless output format")
    parser.add_argument("--resume-session", help="Resume a saved conversation")
    parser.add_argument("--provider-timeout", type=float, default=120, help="Total seconds allowed for one Provider request, including retries")
    parser.add_argument("--max-attempts", type=int, default=3, help="Maximum Provider attempts before any partial output")
    parser.add_argument("--approved-plan", help="Explicitly authorize this reviewed plan in this process")
    parser.add_argument("--plan-hash", help="Exact reviewed plan content hash; required with --approved-plan")
    parser.add_argument("--approval-ttl", type=float, default=300, help="Approved action lifetime in seconds")
    subparsers = parser.add_subparsers(dest="command")
    schema_parser = subparsers.add_parser("schema", help="Print the versioned RunResult JSON Schema")
    schema_parser.set_defaults(_schema=True)
    from eviforge.governance.cli import register_parser as governance_parser
    from eviforge.planning.cli import register_parser as planning_parser
    from eviforge.dag.cli import register_parser as dag_parser
    governance_parser(subparsers)
    planning_parser(subparsers)
    dag_parser(subparsers)
    from eviforge.mcp.cli import register_parser as mcp_parser
    mcp_parser(subparsers)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if getattr(args, "_schema", False):
        import json
        print(json.dumps(RunResult.model_json_schema(), indent=2))
        return
    if args.command:
        handler = getattr(args, "handler", None) or getattr(args, "func", None)
        if handler is None:
            modules = {"governance": "governance", "memory": "governance", "experience": "governance", "plan": "planning", "dag": "dag"}
            import importlib
            handler = importlib.import_module(f"eviforge.{modules[args.command]}.cli").handle
        try:
            outcome = handler(args)
            if inspect.isawaitable(outcome):
                outcome = asyncio.run(outcome)
        except KeyboardInterrupt:
            _print_final(RunResult.failure("cancelled", "cancelled", "Run interrupted"), "json")
            raise SystemExit(EXIT_CODES["cancelled"])
        except Exception as exc:
            result = RunResult.failure("failed", type(exc).__name__, str(exc))
            if isinstance(exc, (ConfigError, ValueError)):
                result.exit_code = EXIT_CODES["config_error"]
            _print_final(result, "json")
            raise SystemExit(result.exit_code)
        if outcome:
            raise SystemExit(outcome)
        return

    options = AutomationOptions(args.output, args.resume_session, args.provider_timeout, args.max_attempts)
    options.approved_plan, options.plan_hash, options.approval_ttl = args.approved_plan, args.plan_hash, args.approval_ttl
    try:
        from eviforge.reliability import RetryPolicy
        RetryPolicy(max_attempts=args.max_attempts, max_elapsed=args.provider_timeout)
        if bool(args.approved_plan) != bool(args.plan_hash):
            raise ValueError("--approved-plan and --plan-hash must be provided together")
        if args.approved_plan and (not args.resume_session or args.p is None or args.mode == "plan"):
            raise ValueError("Approved execution requires -p, --resume-session and an execution mode")
        config = load_config()
        mode = PermissionMode(args.mode or config.permission_mode)
        hook_engine = create_hook_engine(config)
    except (ConfigError, HookConfigError, ValueError) as exc:
        result = RunResult.failure("failed", "config_error", str(exc))
        result.exit_code = EXIT_CODES["config_error"]
        _print_final(result, args.output)
        raise SystemExit(result.exit_code)

    Path(".eviforge").mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s",
                        filename=".eviforge/debug.log", filemode="a")
    if args.p is not None:
        try:
            asyncio.run(_run_prompt_with_hook_cleanup(config, mode, hook_engine, args.p, options=options))
        except KeyboardInterrupt:
            options.result = options.result or RunResult.failure("cancelled", "cancelled", "Run interrupted")
        except Exception as exc:
            options.result = options.result or RunResult.failure("failed", type(exc).__name__, str(exc))
        result = options.result or RunResult.failure("failed", "missing_result", "Run did not produce a result")
        if args.output == "json":
            print(result.model_dump_json())
        elif args.output == "jsonl" and not options.emitted_final:
            _print_final(result, "jsonl")
        elif args.output == "text" and result.status != "success":
            print("; ".join(error.message for error in result.errors), file=sys.stderr)
        if result.exit_code:
            raise SystemExit(result.exit_code)
        return

    from eviforge.app import EviForgeApp
    from eviforge.driver import NoAltScreenDriver
    app = EviForgeApp(providers=config.providers, permission_mode=mode,
        mcp_servers=config.mcp_servers, hook_engine=hook_engine,
        enable_fork=config.enable_fork, enable_verification_agent=config.enable_verification_agent,
        worktree_config=config.worktree, teammate_mode=config.teammate_mode,
        enable_coordinator_mode=config.enable_coordinator_mode, driver_class=NoAltScreenDriver)
    app.run()


def _print_final(result: RunResult, output_format: str) -> None:
    if output_format == "json":
        print(result.model_dump_json())
    elif output_format == "jsonl":
        from eviforge.automation import RunEvent
        print(RunEvent(run_id=result.run_id, sequence=1, timestamp=time.time(), type="run_result", data=result.model_dump(mode="json")).model_dump_json())
    else:
        print("; ".join(error.message for error in result.errors), file=sys.stderr)


async def _shutdown_hook_engine(hook_engine: HookEngine | None) -> None:
    if hook_engine is None or getattr(hook_engine, "_headless_closed", False):
        return
    hook_engine._headless_closed = True
    async def shutdown() -> None:
        await hook_engine.run_hooks("shutdown", HookContext(event_name="shutdown", work_dir=os.getcwd()))
        await hook_engine.wait_for_background_hooks()
    try:
        await asyncio.wait_for(shutdown(), timeout=3.0)
    except asyncio.TimeoutError:
        logging.warning("Timed out while shutting down hooks")
    except Exception:
        logging.exception("Hook shutdown failed")
    finally:
        await hook_engine.cancel_background_hooks()


async def _run_prompt_with_hook_cleanup(config, permission_mode, hook_engine, prompt: str, **kwargs):
    try:
        if hook_engine is not None:
            if not any(h.action.type == "agent" and h.event == "startup" for h in hook_engine.hooks):
                await hook_engine.run_hooks("startup", HookContext(event_name="startup", work_dir=os.getcwd()))
                hook_engine._headless_started = True
        return await _run_prompt(config, permission_mode, hook_engine, prompt, **kwargs)
    finally:
        await _shutdown_hook_engine(hook_engine)


async def _run_prompt(config, permission_mode, hook_engine, prompt: str, *, options: AutomationOptions | None = None) -> RunResult:
    from eviforge.client import AmbiguousStreamError, create_client, resolve_context_window
    from eviforge.reliability import RetryPolicy
    from eviforge.runtime import RuntimeServices

    options = options or AutomationOptions()
    run_id = uuid.uuid4().hex
    started = time.monotonic()
    result = RunResult(run_id=run_id)
    options.result = result
    run_dir = Path.cwd() / ".eviforge" / "runs"
    journal = EventJournal(run_dir / f"{run_id}.events.jsonl", run_id,
                           sys.stdout if options.output_format == "jsonl" else None)
    result.events_path = str(journal.path)
    runtime = None
    client = None
    try:
        provider = config.providers[0]
        client = create_client(provider)
        await resolve_context_window(provider)
        runtime = RuntimeServices.create(config, provider, client=client, work_dir=os.getcwd(),
                    permission_mode=permission_mode, hook_engine=hook_engine,
                    resume_session=options.resume_session)
        agent = runtime.agent
        agent.retry_policy = RetryPolicy(max_attempts=options.max_attempts, max_elapsed=options.provider_timeout)
        conversation = runtime.conversation
        def persist_compact(boundary):
            runtime.session_recorder.compacted(boundary, conversation)
        agent._session_compact_callback = persist_compact
        result.session_id = agent.session_id
        result.trace_id = agent.trace_id or agent.agent_id
        runtime.begin_turn()
        for error in await runtime.start_mcp():
            journal.emit({"type": "mcp_warning", "message": error})
        result.metadata["mcp"] = runtime.mcp_manager.status()
        if runtime.mcp_manager.required_failures:
            raise ConfigError("Required MCP services unavailable: " + ", ".join(runtime.mcp_manager.required_failures))
        if options.approved_plan:
            snapshot = runtime.plan_service.get(options.approved_plan)
            approved = runtime.plan_service.approve(snapshot.plan_id, options.plan_hash,
                session_id=agent.session_id, source_turn_id=snapshot.source_turn_id,
                execution_turn_id=agent.turn_id, agent_id=agent.agent_id, ttl_seconds=options.approval_ttl)
            runtime.plan_service.activate(agent, approved.plan_id, approved.content_hash)
            conversation.add_system_reminder("Execute the approved plan and exact action manifest:\n" + str(approved.as_dict()))
        if hook_engine is not None and not getattr(hook_engine, "_headless_started", False):
            await hook_engine.run_hooks("startup", HookContext(event_name="startup", work_dir=os.getcwd()))
            hook_engine._headless_started = True
        journal.emit({"type": "run_started", "session_id": result.session_id, "trace_id": result.trace_id})

        async def complete(task: str) -> None:
            result.output = await agent.run_to_completion(task, conversation, event_callback=journal.emit)
            if options.output_format == "text":
                print(result.output, flush=True)

        await complete(prompt)
        async with asyncio.timeout(HEADLESS_BACKGROUND_TIMEOUT):
            while agent.last_run_status == "success":
                notes = runtime.drain_notifications()
                if notes:
                    for note in notes:
                        conversation.add_system_reminder(note)
                    await complete("Background agent notifications received. Process them and continue.")
                    await asyncio.sleep(0)
                    continue
                pending = runtime.pending_workers()
                if not pending:
                    break
                await asyncio.wait(pending, timeout=HEADLESS_NOTIFICATION_INTERVAL,
                                   return_when=asyncio.FIRST_COMPLETED)
        status = agent.last_run_status
        if status != "success":
            result.status = status if status in ("ambiguous", "blocked", "approval_required") else "failed"
            result.exit_code = EXIT_CODES[result.status]
            from eviforge.automation import RunError
            result.errors.append(RunError(code=status, message=agent.last_run_error or "Agent did not complete"))
    except BaseException as exc:
        status = "cancelled" if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt)) else "ambiguous" if isinstance(exc, AmbiguousStreamError) else "failed"
        result.status = status
        result.exit_code = EXIT_CODES[status]
        from eviforge.automation import RunError
        result.errors.append(RunError(code=type(exc).__name__, message=str(exc) or status))
        if isinstance(exc, AmbiguousStreamError):
            result.output = exc.partial_text
        raise
    finally:
        try:
            await _shutdown_hook_engine(hook_engine)
            if runtime is not None:
                try:
                    runtime.session_recorder.flush(runtime.conversation)
                    result.input_tokens = runtime.agent.total_input_tokens
                    result.output_tokens = runtime.agent.total_output_tokens
                    if runtime.agent.hook_engine is not None:
                        verification = runtime.agent.hook_engine.verification_summary(
                            runtime.agent._build_hook_context("session_end")
                        )
                        if verification:
                            result.metadata["hook_verification"] = verification
                    from eviforge.planning import PlanState
                    plan = runtime.plan_service.current_plan(runtime.agent)
                    if plan is not None:
                        if plan.state in {PlanState.APPROVED, PlanState.EXECUTING}:
                            plan = runtime.plan_service.finish(plan.plan_id, success=result.status == "success")
                        result.metadata["plan"] = plan.as_dict()
                finally:
                    await runtime.close()
                    result.metadata["background_tasks"] = [{"id": task.id, "status": task.status} for task in runtime.task_manager.list_tasks()]
            elif client is not None:
                from eviforge.lifecycle import close_resource
                await close_resource(client)
        except Exception as exc:
            from eviforge.automation import RunError
            result.errors.append(RunError(code="cleanup_failed", message=str(exc)))
            if result.status == "success":
                result.status, result.exit_code = "failed", EXIT_CODES["failed"]
        finally:
            result.elapsed_seconds = time.monotonic() - started
            try:
                write_result(result, run_dir / f"{run_id}.result.json")
                journal.emit({"type": "run_result", **result.model_dump(mode="json")})
                options.emitted_final = True
            finally:
                journal.close()
    return result


if __name__ == "__main__":
    main()
