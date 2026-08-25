from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Any

from mewcode.config import ConfigError, ConfigTrustError, load_config
from mewcode.hooks import HookConfigError, HookEngine, load_hooks
from mewcode.permissions import PermissionMode
from mewcode.exit_codes import BudgetExceededError, ExitCode
from mewcode.version import __version__


OUTPUT_CHOICES = ("text", "json", "jsonl")


def _add_output_argument(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--output",
        choices=OUTPUT_CHOICES,
        default=argparse.SUPPRESS,
        help="Output contract for this command",
    )
    group.add_argument(
        "--json", action="store_const", const="json", dest="output",
        default=argparse.SUPPRESS,
    )
    group.add_argument(
        "--jsonl", action="store_const", const="jsonl", dest="output",
        default=argparse.SUPPRESS,
    )


def _add_dag_budget_arguments(
    parser: argparse.ArgumentParser, *, default_concurrency: int | None = 4
) -> None:
    parser.add_argument(
        "--max-concurrency", type=int, default=default_concurrency, metavar="N"
    )
    parser.add_argument("--total-tokens", type=int, default=None, metavar="N")
    parser.add_argument("--wall-time", type=float, default=None, metavar="SECONDS")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="eviforge",
        description="EviForge evidence-driven AI coding agent",
    )
    parser.add_argument("--version", action="version", version=f"EviForge {__version__}")
    parser.add_argument("--config", type=Path, default=None, help="Use exactly this config file")
    parser.add_argument(
        "--trust-config",
        action="store_true",
        help="Trust executable Hook/MCP integrations inherited from user-level config",
    )
    parser.add_argument("--provider", default=None, help="Select a configured Provider by name")
    parser.add_argument(
        "--mode",
        choices=[mode.value for mode in PermissionMode],
        default=None,
        help="Permission mode (overrides config.yaml)",
    )
    output_group = parser.add_mutually_exclusive_group()
    output_group.add_argument("--output", choices=OUTPUT_CHOICES, default="text")
    output_group.add_argument("--json", action="store_const", const="json", dest="output")
    output_group.add_argument("--jsonl", action="store_const", const="jsonl", dest="output")
    parser.add_argument("--log-level", default="INFO")

    execution = parser.add_mutually_exclusive_group()
    execution.add_argument("-p", metavar="PROMPT", default=None, help="Run one prompt non-interactively")
    execution.add_argument(
        "--dag",
        type=Path,
        default=None,
        metavar="GRAPH_JSON",
        help="Legacy shorthand for `dag run GRAPH_JSON`",
    )
    parser.add_argument("--contract", type=Path, default=None)
    parser.add_argument("--grant-manifest", type=Path, default=None)
    parser.add_argument("--resume", "--session", dest="resume", default=None, metavar="SESSION_ID")
    parser.add_argument("--allow-session-drift", action="store_true")
    parser.add_argument("--allow-recovery", action="store_true")
    parser.add_argument(
        "--background-policy",
        choices=("wait", "cancel"),
        default="wait",
        help="How headless mode handles child Agents at process exit",
    )
    parser.add_argument("--background-timeout", type=float, default=30.0, metavar="SECONDS")
    parser.add_argument("--dag-max-concurrency", type=int, default=4, metavar="N")
    parser.add_argument("--dag-total-tokens", type=int, default=None, metavar="N")
    parser.add_argument("--dag-wall-time", type=float, default=None, metavar="SECONDS")

    commands = parser.add_subparsers(dest="command")
    commands.add_parser("init", help="Create .mewcode/config.yaml without overwriting")

    config_parser = commands.add_parser("config", help="Validate or explain merged config")
    config_actions = config_parser.add_subparsers(dest="config_action", required=True)
    for name in ("check", "explain"):
        leaf = config_actions.add_parser(name)
        _add_output_argument(leaf)

    doctor = commands.add_parser("doctor", help="Inspect local runtime and config")
    doctor.add_argument("--skip-config", action="store_true")
    _add_output_argument(doctor)

    provider_parser = commands.add_parser("provider", help="Provider diagnostics")
    provider_actions = provider_parser.add_subparsers(dest="provider_action", required=True)
    provider_test = provider_actions.add_parser("test", help="Run a bounded minimal completion")
    provider_test.add_argument("name", nargs="?", default=None)
    provider_test.add_argument("--provider", default=argparse.SUPPRESS)
    provider_test.add_argument("--timeout", type=float, default=20.0)
    _add_output_argument(provider_test)

    mcp_parser = commands.add_parser("mcp", help="Inspect configured MCP servers headlessly")
    mcp_actions = mcp_parser.add_subparsers(dest="mcp_action", required=True)
    for operation in ("status", "list", "test", "reconnect"):
        leaf = mcp_actions.add_parser(operation)
        leaf.add_argument("name", nargs="?", default=None)
        leaf.add_argument("--timeout", type=float, default=15.0)
        _add_output_argument(leaf)

    dag_parser = commands.add_parser(
        "dag", help="Validate, run, inspect or safely resume a typed TaskGraph"
    )
    dag_actions = dag_parser.add_subparsers(dest="dag_action", required=True)
    for action, help_text in (
        ("validate", "Pure offline validation and schedule preview"),
        ("plan", "Alias for offline validation with schedule preview"),
    ):
        leaf = dag_actions.add_parser(action, help=help_text)
        leaf.add_argument("graph", type=Path)
        _add_dag_budget_arguments(leaf)
        _add_output_argument(leaf)
    dag_run = dag_actions.add_parser("run", help="Execute a validated TaskGraph")
    dag_run.add_argument("graph", type=Path)
    _add_dag_budget_arguments(dag_run)
    dag_run.add_argument("--provider", default=argparse.SUPPRESS)
    dag_run.add_argument("--allow-recovery", action="store_true", default=argparse.SUPPRESS)
    _add_output_argument(dag_run)
    dag_status = dag_actions.add_parser("status", help="Inspect a durable DAG run")
    dag_status.add_argument("run_id")
    _add_output_argument(dag_status)
    dag_resume = dag_actions.add_parser(
        "resume", help="Resume only nodes proven safe by persisted evidence"
    )
    dag_resume.add_argument("graph", type=Path)
    dag_resume.add_argument("run_id")
    _add_dag_budget_arguments(dag_resume, default_concurrency=None)
    dag_resume.add_argument("--provider", default=argparse.SUPPRESS)
    dag_resume.add_argument(
        "--allow-recovery", action="store_true", default=argparse.SUPPRESS
    )
    _add_output_argument(dag_resume)

    recovery_parser = commands.add_parser("recovery", help="Inspect durable interrupted actions")
    recovery_actions = recovery_parser.add_subparsers(dest="recovery_action", required=True)
    recovery_status = recovery_actions.add_parser("status")
    _add_output_argument(recovery_status)
    recovery_inspect = recovery_actions.add_parser("inspect")
    recovery_inspect.add_argument("action_id")
    _add_output_argument(recovery_inspect)
    recovery_ack = recovery_actions.add_parser("ack")
    recovery_ack.add_argument("action_id")
    recovery_ack.add_argument("--note", required=True)
    recovery_ack.add_argument("--confirm", action="store_true")
    _add_output_argument(recovery_ack)
    recovery_retry = recovery_actions.add_parser("retry")
    recovery_retry.add_argument("action_id")
    recovery_retry.add_argument("--confirm", action="store_true")
    _add_output_argument(recovery_retry)

    session_parser = commands.add_parser("session", help="List, inspect, export or delete sessions")
    session_actions = session_parser.add_subparsers(dest="session_action", required=True)
    session_list = session_actions.add_parser("list")
    _add_output_argument(session_list)
    session_inspect = session_actions.add_parser("inspect")
    session_inspect.add_argument("session_id")
    _add_output_argument(session_inspect)
    session_export = session_actions.add_parser("export")
    session_export.add_argument("session_id")
    session_export.add_argument("--format", choices=("json", "markdown"), default="json")
    session_export.add_argument("--destination", type=Path, default=None)
    _add_output_argument(session_export)
    session_resume = session_actions.add_parser(
        "resume", help="Continue a session through the governed headless runtime"
    )
    session_resume.add_argument("session_id")
    session_resume.add_argument("-p", "--prompt", dest="p", required=True)
    session_resume.add_argument(
        "--allow-session-drift", action="store_true", default=argparse.SUPPRESS
    )
    session_resume.add_argument(
        "--allow-recovery", action="store_true", default=argparse.SUPPRESS
    )
    session_resume.add_argument(
        "--background-policy",
        choices=("wait", "cancel"),
        default=argparse.SUPPRESS,
    )
    session_resume.add_argument(
        "--background-timeout", type=float, default=argparse.SUPPRESS, metavar="SECONDS"
    )
    session_resume.add_argument("--provider", default=argparse.SUPPRESS)
    _add_output_argument(session_resume)
    session_delete = session_actions.add_parser("delete")
    session_delete.add_argument("session_id")
    session_delete.add_argument("--confirm", action="store_true")
    _add_output_argument(session_delete)

    data_parser = commands.add_parser("data", help="Inspect/export/prune host-owned runtime data")
    data_actions = data_parser.add_subparsers(dest="data_action", required=True)
    data_path = data_actions.add_parser("path")
    _add_output_argument(data_path)
    data_stats = data_actions.add_parser("stats")
    _add_output_argument(data_stats)
    data_export = data_actions.add_parser("export")
    data_export.add_argument("destination", type=Path)
    data_export.add_argument("--include-databases", action="store_true")
    data_export.add_argument("--force", action="store_true")
    _add_output_argument(data_export)
    data_prune = data_actions.add_parser("prune")
    data_prune.add_argument("--older-than-days", type=int, required=True)
    data_prune.add_argument("--apply", action="store_true")
    data_prune.add_argument("--dry-run", action="store_true")
    data_prune.add_argument("--include-databases", action="store_true")
    data_prune.add_argument("--confirm", action="store_true")
    _add_output_argument(data_prune)

    capabilities = commands.add_parser("capabilities", help="Show profile parity and limitations")
    _add_output_argument(capabilities)
    return parser


def _validate_cli_arguments(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    is_session_resume = (
        args.command == "session" and args.session_action == "resume"
    )
    if args.command and (args.p is not None or args.dag is not None) and not is_session_resume:
        parser.error("management subcommands cannot be combined with -p or --dag")
    if args.contract is not None and args.p is None:
        parser.error("--contract requires -p")
    if args.grant_manifest is not None and args.p is None:
        parser.error("--grant-manifest requires -p")
    if args.resume is not None and args.p is None:
        parser.error("--resume requires -p")
    if args.allow_session_drift and args.p is None:
        parser.error("--allow-session-drift requires a headless prompt")
    recovery_execution = (
        args.p is not None
        or args.dag is not None
        or (
            args.command == "dag"
            and args.dag_action in {"run", "resume"}
        )
    )
    if args.allow_recovery and not recovery_execution:
        parser.error("--allow-recovery requires headless, DAG run, or DAG resume")
    dag_options = (
        args.dag_max_concurrency != 4,
        args.dag_total_tokens is not None,
        args.dag_wall_time is not None,
    )
    if any(dag_options) and args.dag is None:
        parser.error("--dag-* options require --dag")
    numeric_values = []
    if args.dag is not None:
        numeric_values.append((args.dag_max_concurrency, args.dag_total_tokens, args.dag_wall_time))
    if args.command == "dag" and args.dag_action in {
        "validate", "plan", "run", "resume"
    }:
        numeric_values.append((args.max_concurrency, args.total_tokens, args.wall_time))
    for concurrency, tokens, wall_time in numeric_values:
        if concurrency is not None and concurrency < 1:
            parser.error("DAG max concurrency must be at least 1")
        if tokens is not None and tokens < 1:
            parser.error("DAG token budget must be at least 1")
        if wall_time is not None and wall_time <= 0:
            parser.error("DAG wall time must be greater than 0")
    if args.background_timeout < 0:
        parser.error("--background-timeout must be non-negative")
    if args.command in {"provider", "mcp"} and args.timeout <= 0:
        parser.error("--timeout must be greater than 0")
    if args.command == "data" and args.data_action == "prune":
        if args.apply and args.dry_run:
            parser.error("--apply and --dry-run are mutually exclusive")


def _load_app_config(path: Path | None):
    return load_config() if path is None else load_config(path)


def _execution_uses_config_integrations(args: argparse.Namespace) -> bool:
    """Return whether this invocation can dispatch Hooks or start MCP clients."""

    if args.command is None:
        return True  # TUI, ``-p`` and legacy ``--dag`` all share this branch.
    if args.command == "dag" and args.dag_action in {"run", "resume"}:
        return True
    return args.command == "session" and args.session_action == "resume"


def _require_trusted_execution_config(
    config: Any, *, trust_config: bool
) -> None:
    summarize = getattr(config, "integration_trust_summary", None)
    # Compatibility for embedding/tests that provide the historical minimal
    # config protocol rather than a concrete ``AppConfig``. Real configs loaded
    # from disk always expose the provenance-aware trust API.
    if not callable(summarize):
        return
    summary = summarize(trust_config=trust_config)
    if not summary["requires_trust"]:
        return
    raise ConfigTrustError(summary)


def _hook_engine(config: Any, runtime: str) -> HookEngine | None:
    try:
        hooks = load_hooks(config.raw_hooks, runtime=runtime)
    except TypeError as exc:
        if "runtime" not in str(exc) and "keyword" not in str(exc):
            raise
        hooks = load_hooks(config.raw_hooks)
    return HookEngine(hooks) if hooks else None


def _configure_logging(level: str) -> str | None:
    try:
        from mewcode.logging_config import configure_logging

        configure_logging(level=level)
        return None
    except Exception as exc:
        # The caller decides whether a human warning is compatible with the
        # selected output contract.  Structured modes never leak this to stderr.
        return f"Logging warning: {type(exc).__name__}: {exc}"


def _render_headless(result: Any, output: str) -> int:
    if output == "json":
        print(result.to_json(), flush=True)
    elif output == "jsonl":
        print(result.to_jsonl(), flush=True)
    else:
        if result.result:
            print(result.result, flush=True)
        if result.error:
            print(
                f"Execution {result.status}: {result.error.get('type', 'Error')}: "
                f"{result.error.get('message', '')}",
                file=sys.stderr,
            )
        if result.recovery:
            print(
                f"Recovery notice: {len(result.recovery)} interrupted action(s) need review; "
                "run `eviforge recovery status`.",
                file=sys.stderr,
            )
    return int(result.exit_code)


def _headless_error_result(exc: Exception):
    """Map bootstrap/runtime failures to the stable headless result contract."""

    from mewcode.agent import CompletionBlockedError
    from mewcode.automation_manifest import AutomationManifestError
    from mewcode.client import (
        AmbiguousStreamError,
        AuthenticationError,
        NetworkError,
        RateLimitError,
    )
    from mewcode.run_result import RunResult
    from mewcode.runtime.data_manager import redact_text

    error_code = str(getattr(exc, "error_code", ""))
    recommendation = str(getattr(exc, "recommendation", ""))
    retryable = False
    details: dict[str, object] = {}

    trust_summary = getattr(exc, "trust_summary", None)
    if isinstance(trust_summary, dict):
        details["trust_summary"] = dict(trust_summary)

    if isinstance(exc, AmbiguousStreamError):
        status = "ambiguous"
        exit_code = ExitCode.NETWORK
        error_code = error_code or "provider.partial_stream_ambiguous"
        recommendation = recommendation or (
            "Inspect the partial transcript and Provider request state before "
            "resuming explicitly; automatic replay is disabled."
        )
        details["emitted_events"] = exc.emitted_events
    elif isinstance(exc, (ConfigError, HookConfigError, AutomationManifestError, ValueError, KeyError)):
        status = "config_error"
        exit_code = ExitCode.CONFIGURATION
        if isinstance(exc, HookConfigError):
            error_code = error_code or "hook.invalid_config"
        elif "Unknown provider" in str(exc):
            error_code = error_code or "provider.unknown"
        elif "not found" in str(exc).lower():
            error_code = error_code or "config.not_found"
        else:
            error_code = error_code or "config.invalid"
    elif isinstance(exc, AuthenticationError):
        status = "authentication_failed"
        exit_code = ExitCode.AUTHENTICATION
        error_code = error_code or "provider.authentication_failed"
        recommendation = recommendation or "Review the selected Provider credential source."
    elif isinstance(exc, (NetworkError, RateLimitError, asyncio.TimeoutError, ConnectionError)):
        status = "network_failed"
        exit_code = ExitCode.NETWORK
        retryable = isinstance(exc, (RateLimitError, asyncio.TimeoutError, ConnectionError))
        error_code = error_code or (
            "provider.rate_limited" if isinstance(exc, RateLimitError) else "provider.network_failed"
        )
    elif isinstance(exc, PermissionError):
        status = "permission_denied"
        exit_code = ExitCode.PERMISSION
        error_code = error_code or "execution.permission_denied"
    elif isinstance(exc, CompletionBlockedError):
        status = "blocked"
        exit_code = ExitCode.EVIDENCE_GATE
        error_code = error_code or "evidence.gate_blocked"
        details["reasons"] = list(exc.event.reasons)
    elif isinstance(exc, BudgetExceededError):
        status = "budget_exceeded"
        exit_code = ExitCode.BUDGET
        error_code = error_code or "budget.exceeded"
    elif isinstance(exc, OSError):
        status = "runtime_failed"
        exit_code = ExitCode.RUNTIME_FAILURE
        error_code = error_code or "runtime.io_failed"
    else:
        status = "internal_error"
        exit_code = ExitCode.INTERNAL
        error_code = error_code or "internal.unexpected"

    events = [
        dict(event)
        for event in getattr(exc, "_eviforge_diagnostic_events", ())
        if isinstance(event, dict)
    ]
    if isinstance(exc, AmbiguousStreamError) and not any(
        event.get("error_code") == error_code for event in events
    ):
        events.append(
            {
                "type": "provider_retry_decision",
                "decision": "blocked_partial_stream",
                "emitted_events": exc.emitted_events,
                "error_code": error_code,
            }
        )
    error: dict[str, object] = {
        "type": type(exc).__name__,
        "code": error_code,
        "message": redact_text(str(exc)),
        "retryable": retryable,
    }
    if recommendation:
        error["recommendation"] = redact_text(recommendation)
    if details:
        error["details"] = details
    result = RunResult(
        status=status,
        exit_code=int(exit_code),
        provider=str(getattr(exc, "_eviforge_provider_name", "")),
        model=str(getattr(exc, "_eviforge_model_name", "")),
        events=events,
        error=error,
    )
    if isinstance(exc, CompletionBlockedError):
        result.verdict = exc.event.verdict
        result.evidence_bundle_ref = exc.event.bundle_ref
        result.evidence = {
            "verdict": exc.event.verdict,
            "bundle_ref": exc.event.bundle_ref,
        }
    return result


def _drain_client_diagnostic_events(client: Any) -> list[dict[str, Any]]:
    drain = getattr(client, "drain_diagnostic_events", None)
    if not callable(drain):
        return []
    try:
        drained = drain()
    except Exception:
        return []
    # AsyncMock and third-party clients can synthesize an awaitable attribute
    # even though this optional diagnostic hook is deliberately synchronous.
    # Do not turn cleanup into a second failure or leak an un-awaited coroutine.
    import inspect

    if inspect.isawaitable(drained):
        close = getattr(drained, "close", None)
        if callable(close):
            close()
        return []
    return [dict(event) for event in drained if isinstance(event, dict)]


def _render_top_level_error(args: argparse.Namespace, exc: Exception) -> int:
    """Render an uncaught CLI failure without breaking its output contract."""

    result = _headless_error_result(exc)
    if args.output in {"json", "jsonl"}:
        is_dag = args.dag is not None or args.command == "dag"
        if is_dag:
            # DAG owns a separate, already-public "1.0" machine protocol.  Do
            # not disguise its bootstrap errors as a prompt RunResult.
            from mewcode.cli_commands import write_payload

            payload = {
                "schema_version": "1.0",
                "kind": "dag_error",
                "status": result.status,
                "exit_code": result.exit_code,
                "error": result.error,
            }
            write_payload(payload, output=args.output, stream=sys.stdout)
            return result.exit_code
        return _render_headless(result, args.output)

    from mewcode.runtime.data_manager import redact_text

    prefix = "Error" if result.exit_code == int(ExitCode.CONFIGURATION) else "Execution failed"
    print(f"{prefix}: {type(exc).__name__}: {redact_text(str(exc))}", file=sys.stderr)
    return result.exit_code


def _dispatch_local_command(args: argparse.Namespace) -> int | None:
    from mewcode.cli_commands import (
        capabilities,
        init_project,
        recovery_acknowledge,
        recovery_inspect,
        recovery_retry,
        recovery_status,
        session_delete,
        session_export,
        session_inspect,
        session_list,
        write_payload,
    )

    if args.command == "init":
        target = init_project()
        print(f"Created {target}\nNext: set the configured API-key environment variable and run `eviforge doctor`.")
        return 0
    if args.command == "capabilities":
        write_payload(capabilities(), output=args.output, stream=sys.stdout)
        return 0
    if args.command == "recovery":
        if args.recovery_action == "status":
            write_payload(recovery_status(), output=args.output, stream=sys.stdout)
            return 0
        if args.recovery_action == "inspect":
            payload = recovery_inspect(args.action_id)
            if payload is None:
                raise ConfigError(f"Recovery action not found: {args.action_id}")
            write_payload(payload, output=args.output, stream=sys.stdout)
            return 0
        if args.recovery_action == "ack":
            if not args.confirm:
                raise PermissionError("Recovery acknowledgement requires --confirm")
            payload = recovery_acknowledge(args.action_id, note=args.note)
        else:
            if not args.confirm:
                raise PermissionError("Recovery retry requires --confirm")
            payload = recovery_retry(args.action_id)
        write_payload(payload, output=args.output, stream=sys.stdout)
        return 0
    if args.command == "session":
        # Resume needs Provider/runtime composition and is dispatched after
        # configuration loading in ``main``.  Other session operations remain
        # offline and side-effect bounded here.
        if args.session_action == "resume":
            return None
        if args.session_action == "list":
            write_payload(session_list(), output=args.output, stream=sys.stdout)
            return 0
        if args.session_action == "inspect":
            payload = session_inspect(args.session_id)
            if payload is None:
                raise ConfigError(f"Session not found: {args.session_id}")
            write_payload(payload, output=args.output, stream=sys.stdout)
            return 0
        if args.session_action == "export":
            content = session_export(args.session_id, format=args.format)
            if content is None:
                raise ConfigError(f"Session not found: {args.session_id}")
            if args.destination is not None:
                destination = args.destination.expanduser().resolve(strict=False)
                if destination.exists():
                    raise ConfigError(f"Refusing to overwrite: {destination}")
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(content, encoding="utf-8")
                write_payload(
                    {"schema_version": 1, "status": "exported", "path": str(destination)},
                    output=args.output,
                    stream=sys.stdout,
                )
            else:
                print(content)
            return 0
        if not args.confirm:
            raise PermissionError("Session deletion requires --confirm")
        if not session_delete(args.session_id):
            raise ConfigError(f"Session not found: {args.session_id}")
        write_payload(
            {"schema_version": 1, "status": "deleted", "session_id": args.session_id},
            output=args.output,
            stream=sys.stdout,
        )
        return 0
    if args.command == "data":
        from mewcode.runtime import RuntimeDataManager

        manager = RuntimeDataManager()
        if args.data_action == "path":
            write_payload(
                {"schema_version": 1, "path": str(manager.root)},
                output=args.output,
                stream=sys.stdout,
            )
            return 0
        if args.data_action == "stats":
            write_payload(manager.stats().as_dict(), output=args.output, stream=sys.stdout)
            return 0
        if args.data_action == "export":
            destination = args.destination.expanduser().resolve(strict=False)
            if destination.exists() and not args.force:
                raise ConfigError(
                    f"Refusing to overwrite: {destination}; pass --force to replace it"
                )
            target = manager.export_zip(destination, include_databases=args.include_databases)
            write_payload(
                {"schema_version": 1, "status": "exported", "path": str(target)},
                output=args.output,
                stream=sys.stdout,
            )
            return 0
        if args.apply and not args.confirm:
            raise PermissionError("Data pruning with --apply requires --confirm")
        candidates = manager.prune(
            older_than_days=args.older_than_days,
            dry_run=not args.apply,
            include_databases=args.include_databases,
        )
        write_payload(
            {
                "schema_version": 1,
                "status": "pruned" if args.apply else "dry_run",
                "older_than_days": args.older_than_days,
                "files": [str(path) for path in candidates],
            },
            output=args.output,
            stream=sys.stdout,
        )
        return 0
    return None


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    _validate_cli_arguments(parser, args)
    needs_runtime_log = (
        args.command is None
        or args.p is not None
        or args.dag is not None
        or args.command in {"provider", "mcp"}
        or (args.command == "dag" and args.dag_action in {"run", "resume"})
        or (args.command == "session" and args.session_action == "resume")
    )
    if needs_runtime_log:
        logging_warning = _configure_logging(args.log_level)
        if logging_warning and args.output == "text":
            print(logging_warning, file=sys.stderr)

    try:
        local_result = _dispatch_local_command(args)
        if local_result is not None:
            return local_result

        if args.command == "dag" and args.dag_action in {"validate", "plan"}:
            from mewcode.cli_commands import write_payload
            from mewcode.orchestration import validate_dag_file

            report = validate_dag_file(
                args.graph,
                max_concurrency=args.max_concurrency,
                total_tokens=args.total_tokens,
                wall_time_seconds=args.wall_time,
            )
            write_payload(report.machine_readable(), output=args.output, stream=sys.stdout)
            return 0 if report.valid else 2

        if args.command == "dag" and args.dag_action == "status":
            from mewcode.cli_commands import write_payload
            from mewcode.orchestration import dag_run_status
            from mewcode.orchestration.persistence import DAGPersistenceError

            try:
                report = dag_run_status(args.run_id, workspace=os.getcwd())
            except DAGPersistenceError as exc:
                write_payload(
                    {
                        "schema_version": "1.0",
                        "kind": "dag_status",
                        "status": "not_found",
                        "run_id": args.run_id,
                        "error": str(exc),
                    },
                    output=args.output,
                    stream=sys.stdout,
                )
                return int(ExitCode.CONFIGURATION)
            write_payload(
                report.machine_readable(), output=args.output, stream=sys.stdout
            )
            return 0

        if args.command == "doctor":
            from mewcode.cli_commands import diagnostic_text, write_payload
            from mewcode.diagnostics import DiagnosticItem, DiagnosticReport, merge_reports, run_doctor

            config = None
            config_failure = None
            if not args.skip_config:
                try:
                    config = _load_app_config(args.config)
                except ConfigError as exc:
                    config_failure = DiagnosticReport(
                        (DiagnosticItem("config.load", "error", str(exc), "Run `eviforge init` or pass --config."),)
                    )
            report = run_doctor(config)
            if config_failure is not None:
                report = merge_reports((report, config_failure))
            write_payload(
                report.as_dict(),
                output=args.output,
                stream=sys.stdout,
                text=diagnostic_text(report),
            )
            return 0 if report.ok else 1

        config = _load_app_config(args.config)
        mode_str = args.mode if args.mode else config.permission_mode
        permission_mode = PermissionMode(mode_str)

        if _execution_uses_config_integrations(args):
            _require_trusted_execution_config(
                config, trust_config=args.trust_config
            )

        if args.command == "config":
            from mewcode.cli_commands import diagnostic_text, write_payload
            from mewcode.diagnostics import check_config

            if args.config_action == "explain":
                payload = config.explain_safe() if hasattr(config, "explain_safe") else config.to_safe_dict()
                write_payload(payload, output=args.output, stream=sys.stdout)
                return 0
            _hook_engine(config, "interactive")
            report = check_config(config)
            write_payload(
                report.as_dict(), output=args.output, stream=sys.stdout, text=diagnostic_text(report)
            )
            return 0 if report.ok else 2

        if args.command == "provider":
            from mewcode.cli_commands import select_provider, test_provider, write_payload

            provider = select_provider(config, args.name or args.provider)
            try:
                payload = asyncio.run(test_provider(provider, timeout=args.timeout))
                exit_code = int(ExitCode.OK)
            except Exception as exc:
                failure = _headless_error_result(exc)
                failure.provider = provider.name
                failure.model = provider.model
                return _render_headless(failure, args.output)
            write_payload(payload, output=args.output, stream=sys.stdout)
            return exit_code

        if args.command == "mcp":
            from mewcode.cli_commands import write_payload
            from mewcode.mcp import inspect_mcp_servers

            result = asyncio.run(
                inspect_mcp_servers(
                    config.mcp_servers,
                    operation=args.mcp_action,
                    server_name=args.name,
                    total_timeout=args.timeout,
                )
            )
            write_payload(result.as_dict(), output=args.output, stream=sys.stdout)
            return 0 if result.ok else int(ExitCode.RUNTIME_FAILURE)

        if args.command == "dag" and args.dag_action in {"run", "resume"}:
            from mewcode.cli_commands import select_provider
            from mewcode.orchestration.cli import resume_dag_file, run_dag_file

            selected = select_provider(config, args.provider)
            original = config.providers
            config.providers = [selected]
            try:
                hook_engine = _hook_engine(config, "dag")
                runner = resume_dag_file if args.dag_action == "resume" else run_dag_file
                positional = (
                    (args.graph, args.run_id)
                    if args.dag_action == "resume"
                    else (args.graph,)
                )
                return asyncio.run(
                    runner(
                        config,
                        permission_mode,
                        hook_engine,
                        *positional,
                        max_concurrency=args.max_concurrency,
                        total_tokens=args.total_tokens,
                        wall_time_seconds=args.wall_time,
                        progress_jsonl=sys.stdout if args.output == "jsonl" else None,
                        allow_recovery=args.allow_recovery,
                    )
                )
            finally:
                config.providers = original

        if args.dag is not None:
            from mewcode.cli_commands import select_provider
            from mewcode.orchestration.cli import run_dag_file

            selected = select_provider(config, args.provider)
            original = config.providers
            config.providers = [selected]
            try:
                hook_engine = _hook_engine(config, "dag")
                return asyncio.run(
                    run_dag_file(
                        config,
                        permission_mode,
                        hook_engine,
                        args.dag,
                        max_concurrency=args.dag_max_concurrency,
                        total_tokens=args.dag_total_tokens,
                        wall_time_seconds=args.dag_wall_time,
                        progress_jsonl=sys.stdout if args.output == "jsonl" else None,
                        allow_recovery=args.allow_recovery,
                    )
                )
            finally:
                config.providers = original

        if args.command == "session" and args.session_action == "resume":
            hook_engine = _hook_engine(config, "headless")
            try:
                result = asyncio.run(
                    _run_prompt(
                        config,
                        permission_mode,
                        hook_engine,
                        args.p,
                        provider_name=args.provider,
                        resume_session_id=args.session_id,
                        allow_session_drift=args.allow_session_drift,
                        allow_recovery=args.allow_recovery,
                        background_policy=args.background_policy,
                        background_timeout=args.background_timeout,
                    )
                )
            except Exception as exc:
                result = _headless_error_result(exc)
            return _render_headless(result, args.output)

        if args.p is not None:
            hook_engine = _hook_engine(config, "headless")
            try:
                result = asyncio.run(
                    _run_prompt(
                        config,
                        permission_mode,
                        hook_engine,
                        args.p,
                        contract_path=args.contract,
                        provider_name=args.provider,
                        automation_manifest_path=args.grant_manifest,
                        resume_session_id=args.resume,
                        allow_session_drift=args.allow_session_drift,
                        allow_recovery=args.allow_recovery,
                        background_policy=args.background_policy,
                        background_timeout=args.background_timeout,
                    )
                )
            except Exception as exc:
                result = _headless_error_result(exc)
            return _render_headless(result, args.output)

        from mewcode.app import EviForgeApp
        from mewcode.cli_commands import select_provider
        from mewcode.driver import NoAltScreenDriver

        providers = config.providers
        if args.provider:
            providers = [select_provider(config, args.provider)]
        hook_engine = _hook_engine(config, "interactive")
        app = EviForgeApp(
            providers=providers,
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
        return 0
    except Exception as exc:
        return _render_top_level_error(args, exc)


def _load_requirement_contract(path: str | os.PathLike[str] | None):
    if path is None:
        return None
    from mewcode.evidence import RequirementContract

    source = Path(path).expanduser().resolve(strict=True)
    try:
        return RequirementContract.model_validate_json(source.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ConfigError(f"Invalid requirement contract {source}: {exc}") from exc


def _headless_recovery_items() -> list[dict[str, object]]:
    """Scan the host-owned journal without constructing a Provider client."""

    from mewcode.cli_commands import jsonable, recovery_status

    payload = recovery_status(work_dir=os.getcwd())
    return [jsonable(item) for item in payload.get("items", [])]


def _recovery_blocked_result(provider: Any, items: list[dict[str, object]]):
    from mewcode.run_result import RunResult

    return RunResult(
        status="recovery_blocked",
        exit_code=int(ExitCode.RUNTIME_FAILURE),
        provider=getattr(provider, "name", ""),
        model=getattr(provider, "model", ""),
        capabilities=("recovery_notice",),
        recovery=items,
        error={
            "type": "RecoveryBlocked",
            "code": "recovery.review_required",
            "message": (
                "Interrupted actions require review. Run `eviforge recovery status`; "
                "continue only with --allow-recovery after review."
            ),
        },
    )


async def _run_prompt(
    config: Any,
    permission_mode: Any,
    hook_engine: HookEngine | None,
    prompt: str,
    *,
    contract_path: str | os.PathLike[str] | None = None,
    provider_name: str | None = None,
    automation_manifest_path: str | os.PathLike[str] | None = None,
    resume_session_id: str | None = None,
    allow_session_drift: bool = False,
    allow_recovery: bool = False,
    background_policy: str = "wait",
    background_timeout: float = 30.0,
):
    from mewcode.cli_commands import select_provider
    from mewcode.client import aclose_client, create_client

    requirement_contract = _load_requirement_contract(contract_path)
    provider = select_provider(config, provider_name)
    startup_recovery = _headless_recovery_items()
    if startup_recovery and not allow_recovery:
        # The recovery boundary precedes Provider construction so an unresolved
        # external effect cannot be followed by an implicit metadata/API call.
        return _recovery_blocked_result(provider, startup_recovery)
    try:
        client = create_client(provider)
    except Exception as exc:
        setattr(exc, "_eviforge_provider_name", str(getattr(provider, "name", "")))
        setattr(exc, "_eviforge_model_name", str(getattr(provider, "model", "")))
        raise
    try:
        try:
            return await _run_prompt_with_client(
                config,
                permission_mode,
                hook_engine,
                prompt,
                contract_path=None,
                requirement_contract=requirement_contract,
                provider=provider,
                client=client,
                automation_manifest_path=automation_manifest_path,
                resume_session_id=resume_session_id,
                allow_session_drift=allow_session_drift,
                allow_recovery=allow_recovery,
                background_policy=background_policy,
                background_timeout=background_timeout,
            )
        except Exception as exc:
            # Preserve retry/fail-closed decisions across resource cleanup so
            # the outer structured error mapper can include them in RunResult.
            diagnostic_events = _drain_client_diagnostic_events(client)
            if diagnostic_events:
                setattr(exc, "_eviforge_diagnostic_events", diagnostic_events)
            setattr(exc, "_eviforge_provider_name", str(getattr(provider, "name", "")))
            setattr(exc, "_eviforge_model_name", str(getattr(provider, "model", "")))
            raise
    finally:
        if hook_engine is not None:
            await hook_engine.shutdown()
        await aclose_client(client)


async def _run_prompt_with_client(
    config: Any,
    permission_mode: Any,
    hook_engine: HookEngine | None,
    prompt: str,
    *,
    contract_path: str | os.PathLike[str] | None,
    requirement_contract: Any = None,
    provider: Any = None,
    client: Any,
    automation_manifest_path: str | os.PathLike[str] | None = None,
    resume_session_id: str | None = None,
    allow_session_drift: bool = False,
    allow_recovery: bool = False,
    background_policy: str = "wait",
    background_timeout: float = 30.0,
):
    """Compose the same governed capabilities used by the interactive app."""

    from mewcode.agent import Agent, CompletionBlockedError
    from mewcode.agents.loader import AgentLoader
    from mewcode.agents.task_manager import TaskManager
    from mewcode.agents.trace import TraceManager
    from mewcode.automation_manifest import load_automation_manifest
    from mewcode.client import resolve_context_window
    from mewcode.config import WorktreeConfig
    from mewcode.conversation import ConversationManager, Message
    from mewcode.execution import ExecutionContext
    from mewcode.memory import MemoryManager
    from mewcode.memory.instructions import load_instructions
    from mewcode.memory.session import (
        SessionManager,
        build_time_gap_message,
        make_compact_boundary,
        workspace_fingerprint,
    )
    from mewcode.mcp import MCPManager
    from mewcode.permissions import (
        DangerousCommandDetector,
        PathSandbox,
        PermissionChecker,
        RuleEngine,
    )
    from mewcode.run_result import RunResult
    from mewcode.runtime import RuntimeBuilder
    from mewcode.skills import SkillLoader
    from mewcode.teams.manager import TeamManager
    from mewcode.tools import create_default_registry
    from mewcode.tools.agent_tool import AgentTool
    from mewcode.tools.impl.tool_search import ToolSearchTool
    from mewcode.tools.load_skill import LoadSkill
    from mewcode.tools.team_create import TeamCreateTool
    from mewcode.tools.team_delete import TeamDeleteTool
    from mewcode.worktree import WorktreeManager

    provider = provider or config.providers[0]
    if requirement_contract is None:
        requirement_contract = _load_requirement_contract(contract_path)
    # Parse and validate the least-authority grant before opening any session,
    # runtime database or MCP transport.  A malformed manifest is a pure
    # configuration failure and must not leave durable bootstrap artifacts.
    automation_manifest = (
        load_automation_manifest(automation_manifest_path)
        if automation_manifest_path is not None
        else None
    )
    preflight_recovery = _headless_recovery_items()
    if preflight_recovery and not allow_recovery:
        return _recovery_blocked_result(provider, preflight_recovery)
    if requirement_contract is not None:
        missing = [
            criterion.criterion_id
            for criterion in requirement_contract.criteria
            if criterion.required and not criterion.verifier_ids
        ]
        if missing:
            raise ConfigError(
                "Required criteria must bind deterministic verifiers: "
                + ", ".join(missing)
            )
    work_dir = os.getcwd()
    home = Path.home()
    checker = PermissionChecker(
        detector=DangerousCommandDetector(),
        sandbox=PathSandbox(work_dir),
        rule_engine=RuleEngine(
            user_rules_path=home / ".mewcode" / "permissions.yaml",
            project_rules_path=Path(work_dir) / ".mewcode" / "permissions.yaml",
            local_rules_path=Path(work_dir) / ".mewcode" / "permissions.local.yaml",
        ),
        mode=permission_mode,
    )

    capabilities = (
        "structured_output",
        "memory_review",
        "sessions",
        "skills",
        "subagents",
        "teams",
        "mcp",
        "hooks_headless",
        "evidence_gate",
        "recovery_notice",
    )
    memory_manager = MemoryManager(work_dir)
    session_manager = SessionManager(work_dir)
    session_manager.cleanup()
    conversation = ConversationManager()
    current_workspace_fingerprint = workspace_fingerprint(work_dir)
    safe_provider = getattr(provider, "to_safe_dict", None)
    provider_values = (
        safe_provider()
        if callable(safe_provider)
        else {
            key: getattr(provider, key)
            for key in (
                "name",
                "protocol",
                "base_url",
                "model",
                "auth",
                "thinking",
                "context_window",
                "max_output_tokens",
            )
            if hasattr(provider, key)
        }
    )
    current_provider_profile = {
        key: value
        for key, value in provider_values.items()
        if key
        in {
            "name",
            "protocol",
            "base_url",
            "model",
            "auth",
            "credential_source",
            "thinking",
            "context_window",
            "max_output_tokens",
        }
    }
    if resume_session_id:
        resume_meta = session_manager.get_meta(resume_session_id)
        if resume_meta is None:
            raise ConfigError(f"Session not found: {resume_session_id}")
        drift_reasons: list[str] = []
        recorded_fingerprint = resume_meta.workspace_fingerprint
        if recorded_fingerprint and recorded_fingerprint != current_workspace_fingerprint:
            drift_reasons.append("workspace fingerprint changed")
        if (
            resume_meta.provider_profile
            and resume_meta.provider_profile != current_provider_profile
        ):
            drift_reasons.append("provider profile changed")
        elif resume_meta.provider_name and resume_meta.provider_name != provider.name:
            drift_reasons.append(
                f"provider changed ({resume_meta.provider_name} -> {provider.name})"
            )
        if resume_meta.capabilities and set(resume_meta.capabilities) != set(capabilities):
            drift_reasons.append("capability profile changed")
        if drift_reasons and not allow_session_drift:
            error = ConfigError(
                "Session runtime profile drift detected: "
                + "; ".join(drift_reasons)
                + ". Review the change, then pass --allow-session-drift "
                "to resume explicitly."
            )
            setattr(error, "error_code", "session.profile_drift")
            raise error

    # Network metadata is intentionally after the Session profile check: a
    # drifted resume must fail closed without contacting a newly selected
    # endpoint first.
    await resolve_context_window(provider)

    registry = create_default_registry()
    runtime_components = RuntimeBuilder(
        work_dir,
        permission_checker=checker,
        protection_mode="policy_only",
    ).build(
        task_id=(
            requirement_contract.task_id
            if requirement_contract is not None
            else None
        )
    )
    startup_recovery = [
        {
            "action_id": item.action.action_id,
            "state": item.action.state.value,
            "recommendation": item.recommendation,
            "reason": item.reason,
        }
        for item in runtime_components.startup_recovery.items
    ]
    if startup_recovery and not allow_recovery:
        runtime_components.close()
        result = _recovery_blocked_result(provider, startup_recovery)
        result.capabilities = capabilities
        return result
    if resume_session_id:
        resumed = session_manager.resume(resume_session_id)
        if resumed is None:  # Profile/transcript changed between the two reads.
            runtime_components.close()
            raise ConfigError(f"Session not found: {resume_session_id}")
        session = resumed.session
        conversation.history = resumed.messages
        gap = build_time_gap_message(resumed.last_active)
        if gap is not None:
            conversation.history.append(gap)
    else:
        session = session_manager.create(
            workspace_fingerprint=current_workspace_fingerprint,
            provider_name=provider.name,
            provider_profile=current_provider_profile,
            capabilities=capabilities,
        )
    if automation_manifest is not None:
        context = ExecutionContext.from_manifest(
            automation_manifest,
            task_id=runtime_components.task.task_id,
            cwd=work_dir,
            workspace_root=work_dir,
        )
        runtime_components.execution_context = context
        runtime_components.gateway.execution_context = context

    agent = Agent(
        client=client,
        registry=registry,
        protocol=provider.protocol,
        work_dir=work_dir,
        permission_checker=checker,
        context_window=provider.get_context_window(),
        instructions_content=load_instructions(work_dir),
        memory_manager=memory_manager,
        hook_engine=hook_engine,
        execution_gateway=runtime_components.gateway,
        task_runtime=runtime_components.task,
        execution_context=runtime_components.execution_context,
        evolution_adapter=runtime_components.evolution,
    )
    agent.session_id = session.session_id
    if requirement_contract is not None:
        agent.set_requirement_contract(requirement_contract)
        agent.begin_contract_execution()

    from mewcode.filehistory import FileHistory

    agent.file_history = FileHistory(work_dir, session.session_id)
    for tool in registry.list_tools():
        if hasattr(tool, "file_history"):
            tool.file_history = agent.file_history

    skill_loader = SkillLoader(work_dir)
    skill_loader.load_all()
    load_skill = LoadSkill()
    load_skill.set_loader(skill_loader)
    load_skill.set_agent(agent)
    registry.register(load_skill)
    skill_catalog = skill_loader.get_catalog()
    if skill_catalog:
        agent.set_skill_catalog(
            "You can use these Skills via LoadSkill:\n"
            + "\n".join(
                f"- {name}: {description}"
                for name, description in skill_catalog
            )
        )

    mcp_manager = MCPManager()
    mcp_manager.load_configs(config.mcp_servers)
    mcp_errors = await mcp_manager.register_all_tools(registry)
    registry.register(ToolSearchTool(registry, protocol=provider.protocol))

    wt_cfg = config.worktree or WorktreeConfig()
    worktree_manager = WorktreeManager(
        repo_root=work_dir,
        symlink_directories=wt_cfg.symlink_directories,
    )
    trace_manager = TraceManager()
    task_manager = TaskManager()
    agent_loader = AgentLoader(
        work_dir,
        enable_verification=config.enable_verification_agent,
    )
    agent_loader.load_all()
    team_manager = TeamManager(
        worktree_manager=worktree_manager,
        trace_manager=trace_manager,
    )
    registry.register(
        AgentTool(
            agent_loader=agent_loader,
            task_manager=task_manager,
            trace_manager=trace_manager,
            parent_agent=agent,
            enable_fork=config.enable_fork,
            provider_config=provider,
            worktree_manager=worktree_manager,
            team_manager=team_manager,
        )
    )
    registry.register(
        TeamCreateTool(
            team_manager=team_manager,
            parent_agent=agent,
            teammate_mode="in-process",
            is_interactive=False,
            enable_coordinator_mode=config.enable_coordinator_mode,
        )
    )
    registry.register(
        TeamDeleteTool(team_manager=team_manager, parent_agent=agent)
    )
    agent_catalog = agent_loader.list_agents()
    if agent_catalog:
        agent.set_agent_catalog(
            "Available sub-Agent roles:\n"
            + "\n".join(
                f"- {name}: {description}"
                for name, description in agent_catalog
            ),
            catalog_list=agent_catalog,
        )
    agent.notification_fn = team_manager.drain_lead_mailbox

    # Inject host-owned runtime context before taking the persistence baseline.
    # Agent.run_to_completion is idempotent for these injections, so the
    # baseline lets us persist only the actual conversation delta rather than
    # leaking environment/memory scaffolding into an exported Session.
    from mewcode.prompts import build_environment_context

    conversation.inject_environment(
        build_environment_context(
            work_dir,
            agent.active_skills,
            agent._skill_catalog,
            agent._agent_catalog,
        )
    )
    conversation.inject_long_term_memory(
        agent.instructions_content,
        memory_manager.load(),
    )
    persisted_message_ids = {id(message) for message in conversation.history}
    persisted_compact_boundary_count = 0

    def persist_session_delta() -> None:
        """Append the recoverable conversation delta exactly once.

        This function is deliberately safe to call on both the success path
        and from ``finally`` after a Provider/tool/background failure.
        """

        nonlocal persisted_compact_boundary_count
        boundaries = agent._compact_boundaries
        for boundary in boundaries[persisted_compact_boundary_count:]:
            durable_keep = [
                message for message in boundary.keep if not message.transient
            ]
            session.append_record(
                make_compact_boundary(boundary.summary, durable_keep)
            )
            # The boundary already embeds its keep tail; do not append those
            # same Message objects a second time below.
            persisted_message_ids.update(id(message) for message in boundary.keep)
            persisted_compact_boundary_count += 1

        for message in conversation.history:
            message_id = id(message)
            if message_id in persisted_message_ids or message.transient:
                continue
            session.append(message)
            persisted_message_ids.add(message_id)

        session.meta.total_tokens = (
            agent.total_input_tokens + agent.total_output_tokens
        )
        session.meta.task_id = runtime_components.task.task_id
        session.meta.trace_id = runtime_components.task.run.trace_id
        session.meta.evidence_bundle_ref = agent._last_evidence_bundle_ref
        session.meta.workspace_fingerprint = workspace_fingerprint(work_dir)
        session.meta.provider_name = provider.name
        session.meta.provider_profile = current_provider_profile
        session.meta.capabilities = list(capabilities)
        session.save_metadata()

    events: list[dict[str, Any]] = [
        {"type": "mcp_warning", "message": error} for error in mcp_errors
    ]

    def record_event(event: dict[str, Any]) -> None:
        event_type = str(event.get("type", "event"))
        if event_type == "usage":
            events.append(
                {"type": "usage", "usage": dict(event.get("usage", {}))}
            )
        elif event_type == "tool_use":
            events.append(
                {
                    "type": "tool_use",
                    "tool_name": str(event.get("toolName", "")),
                }
            )
        elif event_type == "completion_blocked":
            events.append(
                {
                    "type": "completion_blocked",
                    "verdict": str(event.get("verdict", "")),
                    "bundle_ref": str(event.get("bundleRef", "")),
                }
            )

    async def collect_background_results() -> dict[str, object]:
        """Wait on public task states, close idle listeners, then synthesize."""

        if background_policy == "cancel":
            snapshot = await task_manager.shutdown(
                policy="cancel", timeout=max(0.1, background_timeout)
            )
            return snapshot.as_dict()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + background_timeout
        while True:
            while task_manager.snapshot().running_count:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                await asyncio.sleep(min(0.05, remaining))
            # Cancelling here only terminates optional team follow-up listeners;
            # TaskManager preserves work that already reached completed state.
            timed_out = bool(
                task_manager.snapshot().running_count and loop.time() >= deadline
            )
            snapshot = await task_manager.shutdown(
                policy="wait" if timed_out else "cancel",
                timeout=0.0 if timed_out else 1.0,
            )
            notifications: list[str] = []
            for task in task_manager.drain_events():
                notifications.append(
                    f"<task-notification><task_id>{task.id}</task_id>"
                    f"<status>{task.status}</status><result>{task.result}</result>"
                    "</task-notification>"
                )
            notifications.extend(team_manager.drain_lead_mailbox())
            if not notifications or loop.time() >= deadline:
                return snapshot.as_dict()
            for notification in notifications:
                conversation.add_system_reminder(notification)
            await agent.run_to_completion(
                "Background Agent results are ready. Synthesize the final answer.",
                conversation,
                event_callback=record_event,
            )
            if not task_manager.snapshot().running_count:
                return task_manager.snapshot().as_dict()

    last_result = ""
    blocked: CompletionBlockedError | None = None
    background_snapshot: dict[str, object] = {}
    try:
        try:
            last_result = await agent.run_to_completion(
                prompt,
                conversation,
                event_callback=record_event,
            )
        except CompletionBlockedError as exc:
            blocked = exc

        background_snapshot = await collect_background_results()
        # If synthesis produced a new assistant message, return that text.
        for message in reversed(conversation.history):
            if message.role == "assistant" and message.content:
                last_result = message.content
                break

        try:
            await asyncio.wait_for(
                agent._extract_memories(conversation),
                timeout=3.0,
            )
        except (asyncio.TimeoutError, Exception):
            pass

        # Persist the complete new chain, including assistant tool-use blocks
        # and their tool-result messages.  The previous implementation wrote
        # only prompt/final text, so resumed sessions silently lost the
        # evidence needed to understand or continue a tool-assisted turn.
        persist_session_delta()

        run = runtime_components.task.run
        recovery = startup_recovery
        if hook_engine is not None:
            await hook_engine.shutdown(timeout=1.0)
            from mewcode.runtime.data_manager import redact_text

            for notification in hook_engine.drain_notifications():
                events.append(
                    {
                        "type": "hook_result",
                        "hook_id": notification.hook_id,
                        "event": notification.event,
                        "status": (
                            notification.status.value
                            if notification.status is not None
                            else ("succeeded" if notification.success else "failed")
                        ),
                        "elapsed_ms": notification.elapsed_ms,
                        "error_code": notification.error_code,
                        "truncated": notification.truncated,
                        "output": redact_text(notification.output),
                    }
                )
        events.extend(_drain_client_diagnostic_events(client))
        common = {
            "provider": provider.name,
            "model": provider.model,
            "task_id": run.task_id,
            "trace_id": run.trace_id,
            "usage": {
                "input_tokens": agent.total_input_tokens,
                "output_tokens": agent.total_output_tokens,
            },
            "tools": tuple(
                sorted(
                    tool.name
                    for tool in registry.list_tools()
                    if registry.is_enabled(tool.name)
                )
            ),
            "evidence": {
                "verdict": (
                    blocked.event.verdict
                    if blocked is not None
                    else ("PASS" if requirement_contract is not None else None)
                ),
                "bundle_ref": (
                    blocked.event.bundle_ref
                    if blocked is not None
                    else agent._last_evidence_bundle_ref or None
                ),
            },
            "capabilities": capabilities,
            "background": background_snapshot,
            "recovery": recovery,
            "events": events,
        }
        if blocked is not None:
            event = blocked.event
            return RunResult(
                status="blocked",
                result=last_result,
                exit_code=int(ExitCode.EVIDENCE_GATE),
                verdict=event.verdict,
                evidence_bundle_ref=event.bundle_ref,
                error={
                    "type": type(blocked).__name__,
                    "code": "evidence.gate_blocked",
                    "message": "; ".join(event.reasons),
                },
                **common,
            )
        return RunResult(
            status="succeeded",
            result=last_result,
            exit_code=0,
            verdict="PASS" if requirement_contract is not None else "",
            evidence_bundle_ref=agent._last_evidence_bundle_ref,
            **common,
        )
    finally:
        try:
            persist_session_delta()
        except Exception:
            # Preserve the original execution failure.  A transcript write
            # failure is separately observable through missing/incomplete
            # Session metadata and must not mask a Provider/tool exception.
            pass
        try:
            await task_manager.shutdown(policy="cancel", timeout=1.0)
        except Exception:
            pass
        try:
            team_manager.shutdown()
        except Exception:
            pass
        try:
            await mcp_manager.shutdown()
        except Exception:
            pass
        session.close()
        try:
            runtime_components.close()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
