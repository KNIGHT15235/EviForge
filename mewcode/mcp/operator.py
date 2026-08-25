from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal

from mewcode.config import MCPServerConfig
from mewcode.mcp.manager import MCPManager, MCPServerStatus
from mewcode.mcp.redaction import config_secret_values, redact_secret_values
from mewcode.tools import ToolRegistry

MCPInspectOperation = Literal["status", "list", "test", "reconnect"]


@dataclass(frozen=True, slots=True)
class MCPDiagnostic:
    code: str
    message: str
    severity: Literal["info", "warning", "error"] = "error"

    def as_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
        }


@dataclass(frozen=True, slots=True)
class MCPServerInspection:
    name: str
    transport: str
    state: Literal["healthy", "unavailable", "cleanup_failed"]
    reachable: bool
    tool_names: tuple[str, ...] = ()
    elapsed_ms: int = 0
    diagnostics: tuple[MCPDiagnostic, ...] = ()

    @property
    def healthy(self) -> bool:
        return self.state == "healthy"

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "transport": self.transport,
            "state": self.state,
            "healthy": self.healthy,
            "reachable": self.reachable,
            "tool_names": list(self.tool_names),
            "tool_count": len(self.tool_names),
            "elapsed_ms": self.elapsed_ms,
            "diagnostics": [item.as_dict() for item in self.diagnostics],
        }


@dataclass(frozen=True, slots=True)
class MCPInspectionResult:
    operation: str
    requested_server: str | None
    servers: tuple[MCPServerInspection, ...] = ()
    diagnostics: tuple[MCPDiagnostic, ...] = ()
    elapsed_ms: int = 0
    schema_version: int = 1

    @property
    def ok(self) -> bool:
        return not any(item.severity == "error" for item in self.diagnostics) and all(
            server.healthy for server in self.servers
        )

    def as_dict(self) -> dict[str, object]:
        healthy_count = sum(server.healthy for server in self.servers)
        return {
            "schema_version": self.schema_version,
            "operation": self.operation,
            "requested_server": self.requested_server,
            "ok": self.ok,
            "partial": 0 < healthy_count < len(self.servers),
            "server_count": len(self.servers),
            "healthy_count": healthy_count,
            "failed_count": len(self.servers) - healthy_count,
            "elapsed_ms": self.elapsed_ms,
            "servers": [server.as_dict() for server in self.servers],
            "diagnostics": [item.as_dict() for item in self.diagnostics],
        }


def _redact(value: object, secrets: frozenset[str]) -> str:
    return redact_secret_values(value, secrets)


async def _inspect_one(
    config: MCPServerConfig,
    *,
    total_timeout: float | None,
    manager_factory: Callable[..., MCPManager],
) -> MCPServerInspection:
    started = time.monotonic()
    secrets = config_secret_values(config)
    manager = manager_factory(connect_timeout=total_timeout)
    manager.load_configs([config])
    status: MCPServerStatus | None = None
    diagnostics: list[MCPDiagnostic] = []
    cleanup_errors: tuple[str, ...] = ()
    try:
        errors = await manager.register_all_tools(ToolRegistry())
        statuses = manager.statuses()
        status = statuses[0] if statuses else None
        for error in errors:
            diagnostics.append(
                MCPDiagnostic("mcp_initialization_failed", _redact(error, secrets))
            )
        if status is None:
            diagnostics.append(
                MCPDiagnostic(
                    "mcp_status_missing",
                    f"MCP server '{config.name}' did not publish a status",
                )
            )
        elif status.error and not errors:
            diagnostics.append(
                MCPDiagnostic(
                    "mcp_initialization_failed",
                    _redact(status.error, secrets),
                )
            )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        diagnostics.append(
            MCPDiagnostic(
                "mcp_operator_failed",
                f"MCP server '{config.name}' inspection failed: {_redact(exc, secrets)}",
            )
        )
    finally:
        try:
            cleanup_errors = await manager.shutdown()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            cleanup_errors = (
                f"MCP server '{config.name}' cleanup failed: {_redact(exc, secrets)}",
            )

    for error in cleanup_errors:
        diagnostics.append(MCPDiagnostic("mcp_cleanup_failed", _redact(error, secrets)))

    reachable = bool(status and status.connected)
    if cleanup_errors:
        state: Literal["healthy", "unavailable", "cleanup_failed"] = "cleanup_failed"
    elif reachable and not diagnostics:
        state = "healthy"
    else:
        state = "unavailable"
    tool_names = () if status is None else tuple(
        _redact(name, secrets) for name in status.tool_names
    )
    return MCPServerInspection(
        name=config.name,
        transport="stdio" if config.is_stdio else "http",
        state=state,
        reachable=reachable,
        tool_names=tool_names,
        elapsed_ms=max(0, round((time.monotonic() - started) * 1000)),
        diagnostics=tuple(diagnostics),
    )


async def inspect_mcp_servers(
    configs: Sequence[MCPServerConfig],
    *,
    operation: MCPInspectOperation = "status",
    server_name: str | None = None,
    total_timeout: float | None = None,
    manager_factory: Callable[..., MCPManager] = MCPManager,
) -> MCPInspectionResult:
    """Probe MCP servers for a one-shot headless command.

    ``status`` and ``list`` may inspect all configured servers or one selected
    server. ``test`` intentionally requires one server. ``reconnect`` is not
    performed: a one-shot process would immediately close the new transport,
    so the result reports that limitation instead of claiming a false success.

    Each selected server receives an independent manager and total
    connect/initialize/list deadline.  Probes execute concurrently and every
    manager is shut down before this function returns.
    """

    started = time.monotonic()
    requested_operation = str(operation)
    diagnostics: list[MCPDiagnostic] = []
    valid_operations = {"status", "list", "test", "reconnect"}
    if requested_operation not in valid_operations:
        diagnostics.append(
            MCPDiagnostic(
                "mcp_operation_invalid",
                f"Unsupported MCP operation: {requested_operation}",
            )
        )
    if total_timeout is not None and total_timeout <= 0:
        diagnostics.append(
            MCPDiagnostic(
                "mcp_timeout_invalid",
                "MCP total timeout must be positive",
            )
        )
    if requested_operation == "test" and not server_name:
        diagnostics.append(
            MCPDiagnostic(
                "mcp_server_required",
                "The MCP test operation requires an exact server name",
            )
        )

    by_name = {config.name: config for config in configs}
    if server_name and server_name not in by_name:
        diagnostics.append(
            MCPDiagnostic(
                "mcp_server_not_found",
                f"Unknown MCP server: {server_name}",
            )
        )

    if diagnostics:
        return MCPInspectionResult(
            operation=requested_operation,
            requested_server=server_name,
            diagnostics=tuple(diagnostics),
            elapsed_ms=max(0, round((time.monotonic() - started) * 1000)),
        )

    if requested_operation == "reconnect":
        return MCPInspectionResult(
            operation=requested_operation,
            requested_server=server_name,
            diagnostics=(
                MCPDiagnostic(
                    "mcp_reconnect_not_persistent",
                    (
                        "Reconnect is not meaningful in a one-shot headless process; "
                        "use 'test' to verify reachability or reconnect from an active session"
                    ),
                ),
            ),
            elapsed_ms=max(0, round((time.monotonic() - started) * 1000)),
        )

    selected = [by_name[server_name]] if server_name else list(configs)
    servers = await asyncio.gather(
        *(
            _inspect_one(
                config,
                total_timeout=total_timeout,
                manager_factory=manager_factory,
            )
            for config in selected
        )
    )
    return MCPInspectionResult(
        operation=requested_operation,
        requested_server=server_name,
        servers=tuple(sorted(servers, key=lambda item: item.name)),
        elapsed_ms=max(0, round((time.monotonic() - started) * 1000)),
    )


__all__ = [
    "MCPDiagnostic",
    "MCPInspectionResult",
    "MCPInspectOperation",
    "MCPServerInspection",
    "inspect_mcp_servers",
]
