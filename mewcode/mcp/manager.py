from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from mewcode.config import MCPServerConfig
from mewcode.mcp.client import MCPClient
from mewcode.mcp.redaction import redact_config_secrets
from mewcode.mcp.tool_wrapper import MCPToolWrapper
from mewcode.tools import ToolRegistry

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class MCPServerStatus:
    name: str
    connected: bool
    tool_names: tuple[str, ...] = ()
    error: str = ""
    transport: str = "unknown"

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "connected": self.connected,
            "tool_names": list(self.tool_names),
            "tool_count": len(self.tool_names),
            "error": self.error,
            "transport": self.transport,
        }


class MCPManager:
    """Own MCP transports and expose a stable status/lifecycle API."""

    def __init__(self, *, connect_timeout: float | None = None) -> None:
        if connect_timeout is not None and connect_timeout <= 0:
            raise ValueError("MCP connect timeout must be positive")
        self.connect_timeout = connect_timeout
        self._configs: dict[str, MCPServerConfig] = {}
        self._clients: dict[str, MCPClient] = {}
        self._statuses: dict[str, MCPServerStatus] = {}

    def load_configs(self, configs: list[MCPServerConfig]) -> None:
        self._configs = {cfg.name: cfg for cfg in configs}
        self._statuses = {
            name: MCPServerStatus(
                name=name,
                connected=False,
                transport="stdio" if cfg.is_stdio else "http",
            )
            for name, cfg in self._configs.items()
        }

    def statuses(self) -> tuple[MCPServerStatus, ...]:
        return tuple(self._statuses[name] for name in sorted(self._statuses))

    def connected_count(self) -> int:
        return sum(status.connected for status in self._statuses.values())

    def tool_names_for(self, server_name: str) -> tuple[str, ...]:
        status = self._statuses.get(server_name)
        return () if status is None else status.tool_names

    def _timeout_for(self, config: MCPServerConfig) -> float:
        """Return the total connect/initialize/list budget for one server."""

        if self.connect_timeout is not None:
            return self.connect_timeout
        return float(config.connect_timeout)

    @staticmethod
    def _redact_error(config: MCPServerConfig, error: object) -> str:
        """Remove configured header/environment values from operator-visible errors."""

        return redact_config_secrets(error, config)

    async def register_all_tools(self, registry: ToolRegistry) -> list[str]:
        if not self._configs:
            return []
        results = await asyncio.gather(
            *(
                self._connect_and_register(name, config, registry)
                for name, config in self._configs.items()
            )
        )
        return [error for error in results if error]

    async def _connect_and_register(
        self,
        name: str,
        config: MCPServerConfig,
        registry: ToolRegistry,
    ) -> str:
        client = MCPClient(config)
        timeout = self._timeout_for(config)
        transport = "stdio" if config.is_stdio else "http"
        try:
            async def initialize_and_list():
                await client.connect()
                return await client.list_tools()

            # One deadline covers transport connection, protocol initialization,
            # and tool discovery.  Two independent deadlines would permit an
            # unhealthy server to consume almost twice its configured budget.
            tools = await asyncio.wait_for(initialize_and_list(), timeout=timeout)
            wrappers: dict[str, MCPToolWrapper] = {}
            for tool_def in tools:
                wrapper = MCPToolWrapper(name, tool_def, client)
                wrappers[wrapper.name] = wrapper
            for wrapper in wrappers.values():
                registry.register(wrapper)
                logger.info("Registered MCP tool: %s", wrapper.name)
            self._clients[name] = client
            self._statuses[name] = MCPServerStatus(
                name=name,
                connected=True,
                tool_names=tuple(sorted(wrappers)),
                transport=transport,
            )
            return ""
        except asyncio.TimeoutError:
            error = f"MCP server '{name}': initialization timed out after {timeout:g}s"
        except asyncio.CancelledError:
            try:
                await client.close()
            except Exception as exc:
                logger.debug(
                    "MCP server '%s' cleanup failed during cancellation: %s",
                    name,
                    self._redact_error(config, exc),
                )
            raise
        except Exception as exc:
            safe_error = self._redact_error(config, exc)
            error = f"MCP server '{name}': {safe_error}"
        try:
            await client.close()
        except Exception as exc:
            cleanup_error = self._redact_error(config, exc)
            error = f"{error}; transport cleanup failed: {cleanup_error}"
        finally:
            self._clients.pop(name, None)
            self._statuses[name] = MCPServerStatus(
                name=name,
                connected=False,
                error=error,
                transport=transport,
            )
        logger.warning(error)
        return error

    async def get_client(self, name: str) -> MCPClient | None:
        client = self._clients.get(name)
        if client is None:
            config = self._configs.get(name)
            if config is None:
                return None
            client = MCPClient(config)
            timeout = self._timeout_for(config)
            try:
                await asyncio.wait_for(client.connect(), timeout=timeout)
            except BaseException:
                await client.close()
                raise
            self._clients[name] = client
            return client

        if not client.is_alive:
            logger.info("Reconnecting MCP server '%s'", name)
            await client.close()
            config = self._configs[name]
            client = MCPClient(config)
            timeout = self._timeout_for(config)
            try:
                await asyncio.wait_for(client.connect(), timeout=timeout)
            except BaseException:
                await client.close()
                raise
            self._clients[name] = client

        return client

    async def reconnect(self, name: str, registry: ToolRegistry) -> MCPServerStatus:
        config = self._configs.get(name)
        if config is None:
            raise KeyError(f"unknown MCP server: {name}")
        old = self._clients.pop(name, None)
        if old is not None:
            await old.close()
        for tool_name in self.tool_names_for(name):
            registry.unregister(tool_name)
        await self._connect_and_register(name, config, registry)
        return self._statuses[name]

    def set_enabled(self, name: str, registry: ToolRegistry, enabled: bool) -> None:
        if name not in self._configs:
            raise KeyError(f"unknown MCP server: {name}")
        for tool_name in self.tool_names_for(name):
            if enabled:
                registry.enable(tool_name)
            else:
                registry.disable(tool_name)

    async def shutdown(self) -> tuple[str, ...]:
        """Close every owned transport and return redacted cleanup diagnostics."""

        clients = tuple(self._clients.items())
        self._clients.clear()
        if not clients:
            return ()
        results = await asyncio.gather(
            *(client.close() for _, client in clients),
            return_exceptions=True,
        )
        errors: list[str] = []
        for (name, _), result in zip(clients, results, strict=True):
            if isinstance(result, BaseException):
                config = self._configs[name]
                safe_error = self._redact_error(config, result)
                message = f"MCP server '{name}' cleanup failed: {safe_error}"
                errors.append(message)
                logger.debug(message)
            else:
                logger.info("MCP server '%s' closed", name)
        for name, status in tuple(self._statuses.items()):
            self._statuses[name] = MCPServerStatus(
                name=name,
                connected=False,
                tool_names=status.tool_names,
                transport=status.transport,
            )
        return tuple(errors)


__all__ = ["MCPManager", "MCPServerStatus"]
