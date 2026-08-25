from __future__ import annotations

import os
import platform
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from mewcode.config import AppConfig
from mewcode.runtime.paths import resolve_control_root
from mewcode.validator import ConfigError, validate_env_references


@dataclass(frozen=True, slots=True)
class DiagnosticItem:
    check_id: str
    status: str
    message: str
    remediation: str = ""
    details: dict[str, object] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "id": self.check_id,
            "status": self.status,
            "message": self.message,
        }
        if self.remediation:
            result["remediation"] = self.remediation
        if self.details:
            result["details"] = self.details
        return result


@dataclass(frozen=True, slots=True)
class DiagnosticReport:
    items: tuple[DiagnosticItem, ...]

    @property
    def ok(self) -> bool:
        return all(item.status != "error" for item in self.items)

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "ok": self.ok,
            "items": [item.as_dict() for item in self.items],
        }


def _placeholder_model(value: str) -> bool:
    lowered = value.strip().casefold()
    return not lowered or any(
        marker in lowered
        for marker in ("your-model", "model-name", "replace-me", "<model>")
    )


def check_config(config: AppConfig) -> DiagnosticReport:
    items: list[DiagnosticItem] = []
    items.append(
        DiagnosticItem(
            "config.sources",
            "ok",
            f"Loaded {len(config.config_sources)} config source(s)",
            details={"sources": [str(path) for path in config.config_sources]},
        )
    )
    try:
        validate_env_references(
            config.env_reference_payload(), path="config.integrations"
        )
    except ConfigError as exc:
        items.append(
            DiagnosticItem(
                "config.environment",
                "error",
                str(exc),
                "Set the named environment variable in this shell, then rerun `eviforge config check`.",
            )
        )
    else:
        items.append(
            DiagnosticItem(
                "config.environment",
                "ok",
                "All ${NAME} environment references are available",
            )
        )

    trust_summary = config.integration_trust_summary()
    trust_required = bool(trust_summary["requires_trust"])
    items.append(
        DiagnosticItem(
            "config.integration_trust",
            "warning" if trust_required else "ok",
            (
                "Inherited user-level executable integrations require explicit trust"
                if trust_required
                else "No untrusted inherited executable integrations are active"
            ),
            "Review the trust summary and pass `--trust-config` only for an execution you approve."
            if trust_required
            else "",
            details=trust_summary,
        )
    )
    for provider in config.providers:
        prefix = f"provider.{provider.name}"
        if _placeholder_model(provider.model):
            items.append(
                DiagnosticItem(
                    f"{prefix}.model",
                    "error",
                    "Provider model is empty or still a placeholder",
                    "Set an exact model identifier supported by this endpoint.",
                )
            )
        else:
            items.append(
                DiagnosticItem(f"{prefix}.model", "ok", f"Model: {provider.model}")
            )
        credential_source = provider.credential_source()
        if provider.api_key:
            items.append(
                DiagnosticItem(
                    f"{prefix}.credential",
                    "warning",
                    "Credential is stored inline in YAML (value redacted)",
                    "Move it to an environment variable and set api_key_env.",
                    {"source": "inline (deprecated)"},
                )
            )
        elif provider.auth != "none" and not provider.resolve_api_key():
            items.append(
                DiagnosticItem(
                    f"{prefix}.credential",
                    "error",
                    f"Credential is unavailable ({credential_source})",
                    "Set the named environment variable or configure api_key_env explicitly.",
                    {"source": credential_source},
                )
            )
        else:
            items.append(
                DiagnosticItem(
                    f"{prefix}.credential",
                    "ok",
                    f"Credential source: {credential_source}",
                    details={"source": credential_source},
                )
            )
    for server in config.mcp_servers:
        if server.is_stdio:
            command = server.command or ""
            discoverable = bool(
                command
                and (
                    Path(command).expanduser().is_file()
                    or shutil.which(command) is not None
                )
            )
            items.append(
                DiagnosticItem(
                    f"mcp.{server.name}.command",
                    "ok" if discoverable else "error",
                    (
                        f"MCP command is discoverable: {command}"
                        if discoverable
                        else f"MCP command is not discoverable: {command}"
                    ),
                    "Install the executable in this environment or use an absolute path."
                    if not discoverable
                    else "",
                )
            )
        else:
            items.append(
                DiagnosticItem(
                    f"mcp.{server.name}.url",
                    "ok",
                    f"MCP HTTP endpoint configured for {server.name}",
                )
            )
    if config.raw_hooks:
        items.append(
            DiagnosticItem(
                "hooks.review",
                "warning",
                f"{len(config.raw_hooks)} Hook(s) will be loaded",
                "Review command/http actions and their config source before execution.",
                {"hook_ids": [str(item.get("id", "")) for item in config.raw_hooks]},
            )
        )
    return DiagnosticReport(tuple(items))


def run_doctor(
    config: AppConfig | None = None,
    *,
    work_dir: str | os.PathLike[str] | None = None,
) -> DiagnosticReport:
    root = Path(work_dir or Path.cwd()).resolve(strict=False)
    items: list[DiagnosticItem] = [
        DiagnosticItem(
            "runtime.python",
            "ok" if sys.version_info >= (3, 11) else "error",
            f"Python {platform.python_version()} at {sys.executable}",
            "Use Python 3.11 or newer." if sys.version_info < (3, 11) else "",
        ),
        DiagnosticItem(
            "runtime.uv",
            "ok" if shutil.which("uv") else "warning",
            "uv is available" if shutil.which("uv") else "uv is not on PATH",
            "Install uv or activate the project environment." if not shutil.which("uv") else "",
        ),
        DiagnosticItem(
            "runtime.git",
            "ok" if shutil.which("git") else "error",
            "git is available" if shutil.which("git") else "git is not on PATH",
            "Install Git inside the same Windows/WSL environment." if not shutil.which("git") else "",
        ),
        DiagnosticItem(
            "workspace.root",
            "ok" if root.is_dir() else "error",
            f"Workspace: {root}",
        ),
        DiagnosticItem(
            "workspace.writable",
            "ok" if root.is_dir() and os.access(root, os.W_OK) else "warning",
            (
                "Workspace is writable"
                if root.is_dir() and os.access(root, os.W_OK)
                else "Workspace is read-only for this process"
            ),
            "Use a writable checkout for Agent execution; offline checks remain available."
            if not (root.is_dir() and os.access(root, os.W_OK))
            else "",
        ),
        DiagnosticItem(
            "runtime.control_root",
            "ok",
            f"Control root: {resolve_control_root()}",
        ),
    ]
    is_wsl = "microsoft" in platform.release().casefold()
    windows_python_in_wsl = is_wsl and ("\\" in sys.executable or sys.executable.casefold().endswith(".exe"))
    items.append(
        DiagnosticItem(
            "runtime.wsl_interpreter",
            "error" if windows_python_in_wsl else "ok",
            (
                "Windows Python is being used from WSL"
                if windows_python_in_wsl
                else ("WSL interpreter boundary is valid" if is_wsl else "Not running in WSL")
            ),
            "Create and select a Linux virtual environment inside WSL."
            if windows_python_in_wsl
            else "",
        )
    )
    try:
        from mewcode.runtime import ControlPlanePaths, RuntimeDataManager, workspace_id_for

        data_manager = RuntimeDataManager()
        stats = data_manager.stats()
        items.append(
            DiagnosticItem(
                "runtime.data_usage",
                "ok",
                f"Runtime data: {stats.file_count} file(s), {stats.total_bytes} bytes",
                details=stats.as_dict(),
            )
        )
        total_quota = stats.limits.get("runtime_total_quota_bytes")
        items.append(
            DiagnosticItem(
                "runtime.data_limits",
                "ok" if total_quota is not None else "warning",
                (
                    f"Runtime total quota: {total_quota} bytes"
                    if total_quota is not None
                    else "Runtime total quota: not enforced; logs remain rotation-bounded"
                ),
                (
                    "Use `eviforge data stats` to monitor usage and run "
                    "`eviforge data prune --older-than-days N` (dry-run by default)."
                    if total_quota is None
                    else ""
                ),
                {
                    "limits": stats.limits,
                    "retention_policy": stats.retention_policy,
                },
            )
        )
        recent_error_code = data_manager.recent_error_code()
        items.append(
            DiagnosticItem(
                "runtime.last_error_code",
                "ok" if recent_error_code is not None else "warning",
                (
                    f"Most recent structured error code: {recent_error_code}"
                    if recent_error_code is not None
                    else "Most recent structured error code: unknown"
                ),
                (
                    "No structured error_code field was safely readable from bounded runtime logs."
                    if recent_error_code is None
                    else ""
                ),
                {
                    "error_code": recent_error_code,
                    "source": (
                        "bounded_runtime_log_scan"
                        if recent_error_code is not None
                        else "unknown"
                    ),
                    "inferred": False,
                },
            )
        )
        database = ControlPlanePaths.build(
            workspace_id=workspace_id_for(root)
        ).database
        if database.is_file():
            from mewcode.recovery import RecoveryStore

            with RecoveryStore(database_path=database) as recovery:
                report = recovery.scan_recovery()
            items.append(
                DiagnosticItem(
                    "runtime.recovery",
                    "warning" if report.items else "ok",
                    f"Unresolved recovery actions: {len(report.items)}",
                    "Run `eviforge recovery status` before executing new work."
                    if report.items
                    else "",
                    {
                        "action_ids": [item.action.action_id for item in report.items],
                        "recommendations": [item.recommendation for item in report.items],
                    },
                )
            )
        else:
            items.append(
                DiagnosticItem(
                    "runtime.recovery",
                    "ok",
                    "No runtime database exists for this workspace",
                )
            )
    except Exception as exc:
        items.append(
            DiagnosticItem(
                "runtime.data_inspection",
                "warning",
                f"Runtime data inspection failed: {type(exc).__name__}",
                "Run `eviforge data stats` and `eviforge recovery status` separately.",
            )
        )
    if config is not None:
        items.extend(check_config(config).items)
    return DiagnosticReport(tuple(items))


def merge_reports(reports: Iterable[DiagnosticReport]) -> DiagnosticReport:
    return DiagnosticReport(tuple(item for report in reports for item in report.items))


__all__ = [
    "DiagnosticItem",
    "DiagnosticReport",
    "check_config",
    "merge_reports",
    "run_doctor",
]
