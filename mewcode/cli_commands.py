"""Operator-facing CLI helpers with stable, redacted output contracts."""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import asdict, is_dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, TextIO

from mewcode.config import AppConfig, ConfigError, ProviderConfig


INIT_CONFIG = """# EviForge project configuration.
# Keep credentials out of this file; name an environment variable instead.
providers:
  - name: primary
    protocol: openai
    base_url: https://api.openai.com/v1
    model: replace-with-your-model-id
    api_key_env: OPENAI_API_KEY
    auth: required
    thinking: false
    context_window: 0
    max_output_tokens: 0

permission_mode: default
enable_fork: false
enable_verification_agent: true
teammate_mode: in-process
enable_coordinator_mode: false

worktree:
  symlink_directories: [node_modules, vendor]
  stale_cleanup_interval: 3600
  stale_cutoff_hours: 24

mcp_servers: []
hooks: []
"""


def jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return jsonable(asdict(value))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [jsonable(item) for item in value]
    return value


def write_payload(
    payload: Any,
    *,
    output: str = "text",
    stream: TextIO,
    text: str | None = None,
) -> None:
    safe = jsonable(payload)
    if output == "text":
        print(text if text is not None else json.dumps(safe, ensure_ascii=False, indent=2), file=stream)
    elif output == "json":
        print(json.dumps(safe, ensure_ascii=False, sort_keys=True), file=stream)
    elif output == "jsonl":
        if isinstance(safe, list):
            for item in safe:
                print(json.dumps(item, ensure_ascii=False, sort_keys=True), file=stream)
        else:
            print(json.dumps(safe, ensure_ascii=False, sort_keys=True), file=stream)
    else:  # pragma: no cover - argparse owns the public boundary
        raise ValueError(f"unsupported output format: {output}")


def select_provider(config: AppConfig, name: str | None) -> ProviderConfig:
    if name is None:
        if not config.providers:
            raise ConfigError("No providers are configured")
        return config.providers[0]
    for provider in config.providers:
        if provider.name == name:
            return provider
    available = ", ".join(provider.name for provider in config.providers) or "(none)"
    raise ConfigError(f"Unknown provider '{name}'. Available providers: {available}")


def init_project(*, work_dir: str | os.PathLike[str] | None = None) -> Path:
    root = Path(work_dir or Path.cwd()).resolve(strict=False)
    target = root / ".mewcode" / "config.yaml"
    if target.exists():
        raise ConfigError(f"Refusing to overwrite existing config: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(INIT_CONFIG, encoding="utf-8")
    return target


def diagnostic_text(report: Any) -> str:
    markers = {"ok": "OK", "warning": "WARN", "error": "ERROR"}
    lines = []
    for item in report.items:
        lines.append(f"[{markers.get(item.status, item.status.upper())}] {item.check_id}: {item.message}")
        if item.remediation:
            lines.append(f"  fix: {item.remediation}")
    return "\n".join(lines)


async def test_provider(provider: ProviderConfig, *, timeout: float = 20.0) -> dict[str, Any]:
    """Perform one explicit, bounded minimal completion against a Provider."""

    from mewcode.client import aclose_client, create_client
    from mewcode.conversation import ConversationManager
    from mewcode.tools.base import StreamEnd, TextDelta

    client = create_client(provider)
    conversation = ConversationManager()
    conversation.add_user_message("Reply with OK only.")
    text_parts: list[str] = []
    usage = {"input_tokens": 0, "output_tokens": 0}

    async def consume() -> None:
        async for event in client.stream(conversation, system="Connection test. Do not call tools."):
            if isinstance(event, TextDelta):
                text_parts.append(event.text)
            elif isinstance(event, StreamEnd):
                usage["input_tokens"] = event.input_tokens
                usage["output_tokens"] = event.output_tokens

    started = asyncio.get_running_loop().time()
    try:
        await asyncio.wait_for(consume(), timeout=timeout)
    finally:
        await aclose_client(client)
    elapsed_ms = round((asyncio.get_running_loop().time() - started) * 1000, 2)
    return {
        "schema_version": 1,
        "status": "ok",
        "provider": provider.name,
        "protocol": provider.protocol,
        "model": provider.model,
        "credential_source": provider.credential_source(),
        "elapsed_ms": elapsed_ms,
        "usage": usage,
        "response_preview": "".join(text_parts).strip()[:80],
    }


def _workspace_runtime_database(work_dir: str | os.PathLike[str] | None = None) -> Path:
    from mewcode.runtime import ControlPlanePaths, workspace_id_for

    root = Path(work_dir or Path.cwd()).resolve(strict=False)
    return ControlPlanePaths.build(workspace_id=workspace_id_for(root)).database


def recovery_status(*, work_dir: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    from mewcode.recovery import RecoveryStore

    database = _workspace_runtime_database(work_dir)
    if not database.is_file():
        return {
            "schema_version": 1,
            "database": str(database),
            "items": [],
            "reconciled_action_ids": [],
            "expired_ticket_ids": [],
        }
    with RecoveryStore(database_path=database) as store:
        report = store.scan_recovery()
    return {
        "schema_version": 1,
        "database": str(database),
        "scanned_at": report.scanned_at,
        "items": [
            {
                "action_id": item.action.action_id,
                "task_id": item.action.task_id,
                "action_type": item.action.action_type,
                "state": item.action.state,
                "effect_kind": item.action.effect_kind,
                "recommendation": item.recommendation,
                "reason": item.reason,
            }
            for item in report.items
        ],
        "reconciled_action_ids": report.reconciled_action_ids,
        "expired_ticket_ids": report.expired_ticket_ids,
    }


def recovery_inspect(
    action_id: str, *, work_dir: str | os.PathLike[str] | None = None
) -> dict[str, Any] | None:
    from mewcode.recovery import RecoveryStore

    database = _workspace_runtime_database(work_dir)
    if not database.is_file():
        return None
    with RecoveryStore(database_path=database) as store:
        action = store.get_action(action_id)
        if action is None:
            return None
        journal = store.list_journal(action_id=action_id)
        checkpoints = [
            checkpoint
            for checkpoint in store.list_checkpoints(task_id=action.task_id)
            if checkpoint.action_id == action_id
        ]
    return {
        "schema_version": 1,
        "database": str(database),
        "action": action,
        "journal": journal,
        "checkpoints": checkpoints,
    }


def recovery_acknowledge(
    action_id: str,
    *,
    note: str,
    work_dir: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    from mewcode.recovery import ActionState, RecoveryStore

    if not note.strip():
        raise ValueError("--note must explain the human review outcome")
    database = _workspace_runtime_database(work_dir)
    if not database.is_file():
        raise KeyError(f"unknown recovery action: {action_id}")
    with RecoveryStore(database_path=database) as store:
        action = store.get_action(action_id)
        if action is None:
            raise KeyError(f"unknown recovery action: {action_id}")
        if action.state is not ActionState.UNCERTAIN:
            raise ValueError("only an uncertain action can receive a human acknowledgement")
        checkpoint = store.create_checkpoint(
            task_id=action.task_id,
            action_id=action.action_id,
            label="human_acknowledged_uncertain",
            payload={"note": note.strip(), "automatic_retry": False},
        )
    return {
        "schema_version": 1,
        "status": "acknowledged",
        "action_id": action_id,
        "checkpoint": checkpoint,
        "automatic_retry": False,
    }


def recovery_retry(
    action_id: str, *, work_dir: str | os.PathLike[str] | None = None
) -> dict[str, Any]:
    """Retry only a staged file replacement whose hashes still prove safety."""

    from mewcode.recovery import (
        ActionState,
        AttemptLease,
        EffectKind,
        RecoveryStore,
    )

    database = _workspace_runtime_database(work_dir)
    if not database.is_file():
        raise KeyError(f"unknown recovery action: {action_id}")
    with RecoveryStore(database_path=database) as store:
        action = store.get_action(action_id)
        if action is None:
            raise KeyError(f"unknown recovery action: {action_id}")
        if action.state is ActionState.UNCERTAIN:
            raise ValueError("uncertain actions are never retried automatically")
        if action.state is not ActionState.STARTED:
            raise ValueError(f"action is not retryable from state {action.state.value}")
        if action.effect_kind is not EffectKind.FILE_REPLACE:
            raise ValueError(
                "only hash-verified staged file replacements support CLI retry"
            )
        lease = AttemptLease(
            action.action_id,
            action.attempt_id or "",
            action.fencing_generation,
        )
        fenced = store.supersede_attempt(
            lease,
            reason="operator_confirmed_safe_file_retry",
        )
        completed = store.execute_file_replace(fenced)
    return {
        "schema_version": 1,
        "status": completed.state,
        "action_id": completed.action_id,
        "fencing_generation": completed.fencing_generation,
        "reconciled": completed.reconciled,
    }


def session_list(work_dir: str | os.PathLike[str] | None = None) -> list[dict[str, Any]]:
    from mewcode.memory.session import SessionManager

    manager = SessionManager(str(Path(work_dir or Path.cwd()).resolve(strict=False)))
    return [jsonable(meta) for meta in manager.list()]


def session_inspect(
    session_id: str, *, work_dir: str | os.PathLike[str] | None = None
) -> dict[str, Any] | None:
    for meta in session_list(work_dir):
        if meta["id"] == session_id:
            return {"schema_version": 1, "metadata": meta}
    return None


def session_export(
    session_id: str,
    *,
    format: str,
    work_dir: str | os.PathLike[str] | None = None,
) -> str | None:
    from mewcode.memory.session import SessionManager

    return SessionManager(str(Path(work_dir or Path.cwd()).resolve(strict=False))).export(
        session_id, format=format
    )


def session_delete(
    session_id: str, *, work_dir: str | os.PathLike[str] | None = None
) -> bool:
    from mewcode.memory.session import SessionManager

    return SessionManager(str(Path(work_dir or Path.cwd()).resolve(strict=False))).delete(
        session_id
    )


def capabilities() -> dict[str, Any]:
    """Declare runtime differences instead of implying false parity."""

    return {
        "schema_version": 1,
        "profiles": {
            "interactive": {
                "available": [
                    "tui", "provider_selection", "memory_review", "sessions",
                    "skills", "subagents", "teams", "mcp", "plan_review",
                    "hooks_interactive", "evidence_gate", "experience_workflow",
                    "recovery_notice",
                ],
                "limitations": [],
            },
            "headless": {
                "available": [
                    "provider_selection", "structured_output", "memory_review",
                    "sessions", "skills", "subagents", "teams", "mcp",
                    "hooks_headless", "evidence_gate", "automation_manifest",
                    "background_shutdown_policy", "recovery_notice",
                ],
                "limitations": ["interactive approvals are unavailable"],
            },
            "dag": {
                "available": [
                    "offline_validation", "typed_roles", "dependency_scheduling",
                    "write_conflict_serialisation", "artifact_evidence",
                    "structured_progress", "hooks_dag",
                ],
                "limitations": [
                    "no interactive approvals", "no session resume",
                    "MCP and inline Skills are not injected into DAG nodes",
                    "token limits are reservation plus completion reconciliation, not in-flight hard caps",
                ],
            },
        },
    }


__all__ = [
    "capabilities",
    "diagnostic_text",
    "init_project",
    "jsonable",
    "recovery_acknowledge",
    "recovery_inspect",
    "recovery_retry",
    "recovery_status",
    "select_provider",
    "session_delete",
    "session_export",
    "session_inspect",
    "session_list",
    "test_provider",
    "write_payload",
]
