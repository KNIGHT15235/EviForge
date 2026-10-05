from __future__ import annotations

import asyncio
from typing import Any

from eviforge.config import MCPServerConfig
from eviforge.mcp.client import MCPClient
from eviforge.mcp.diagnostics import safe_error
from eviforge.mcp.policy import tool_kind
from eviforge.mcp.tool_wrapper import MCPToolWrapper
from eviforge.tools import ToolRegistry


class MCPManager:
    def __init__(self) -> None:
        self._configs: dict[str, MCPServerConfig] = {}
        self._clients: dict[str, MCPClient] = {}
        self._states: dict[str, dict[str, Any]] = {}
        self._registered: dict[str, list[str]] = {}
        self._registry: ToolRegistry | None = None
        self._locks: dict[str, asyncio.Lock] = {}

    def load_configs(self, configs: list[MCPServerConfig]) -> None:
        names = [config.name for config in configs]
        if len(set(names)) != len(names):
            raise ValueError('Duplicate MCP server names')
        self._configs = {config.name: config for config in configs}

    def status(self) -> list[dict[str, Any]]:
        return [{'name': name, 'integration': config.integration, 'required': config.required, **self._states.get(name, {'state': 'configured' if config.enabled else 'disabled', 'tools': []})} for name, config in self._configs.items()]

    def tool_names(self, server: str) -> list[str]:
        return list(self._registered.get(server, []))

    @property
    def required_failures(self) -> list[str]:
        return [item['name'] for item in self.status() if self._configs[item['name']].enabled and item['required'] and item['state'] != 'connected']

    async def register_all_tools(self, registry: ToolRegistry) -> list[str]:
        self._registry = registry
        for name in set(self._registered) | set(self._clients):
            if name not in self._configs:
                for old in self._registered.pop(name, []):
                    registry.unregister(old)
                client = self._clients.pop(name, None)
                if client is not None:
                    await client.close()
                self._states.pop(name, None)
        errors = []
        semaphore = asyncio.Semaphore(3)

        async def register(name: str, config: MCPServerConfig) -> None:
            async with semaphore:
                for old in self._registered.pop(name, []):
                    registry.unregister(old)
                old_client = self._clients.pop(name, None)
                if old_client is not None:
                    await old_client.close()
                if not config.enabled:
                    self._states[name] = {'state': 'disabled', 'tools': []}
                    return
                client = MCPClient(config)
                stage = 'connect'
                try:
                    async with asyncio.timeout(config.startup_timeout_seconds):
                        await client.connect()
                        stage = 'discovery'
                        definitions = await client.list_tools()
                        stage = 'schema_validation'
                        wrappers = [MCPToolWrapper(name, definition, client, config=config) for definition in definitions if tool_kind(config, definition.name)]
                        names = [wrapper.name for wrapper in wrappers]
                        if len(set(names)) != len(names) or any(registry.get(n) is not None for n in names):
                            raise ValueError('MCP tool identity collision')
                        missing = set(config.allowed_tools) - {definition.name for definition in definitions}
                        stage = 'required_tools'
                        if config.required and (missing or not wrappers):
                            raise ValueError('Required MCP tools are missing')
                        for wrapper in wrappers:
                            registry.register(wrapper)
                        self._clients[name] = client
                        self._registered[name] = names
                        info = getattr(client, 'server_info', {})
                        self._states[name] = {'state': 'connected', 'tools': names, 'server_info': info if isinstance(info, dict) else {}, 'available_tools': len(definitions), 'filtered_tools': len(definitions) - len(wrappers), 'missing_tools': sorted(missing)}
                except asyncio.CancelledError:
                    await client.close()
                    raise
                except Exception as exc:
                    await client.close()
                    state = safe_error(exc, config)
                    self._states[name] = {'state': 'auth_required' if state.startswith('auth_required') else 'failed', 'tools': [], 'error': state, 'stage':stage}
                    errors.append(f"MCP server '{name}': {state}")
        await asyncio.gather(*(register(name, config) for name, config in self._configs.items()))
        return errors

    async def get_client(self, name: str) -> MCPClient | None:
        async with self._locks.setdefault(name, asyncio.Lock()):
            config = self._configs.get(name)
            if config is None or not config.enabled:
                return None
            client = self._clients.get(name)
            if client is None:
                client = MCPClient(config)
                self._clients[name] = client
            if not client.is_alive:
                await client.connect()
            return client

    async def shutdown(self) -> None:
        outcomes = await asyncio.gather(*(client.close() for client in self._clients.values()), return_exceptions=True)
        self._clients.clear()
        for state in self._states.values():
            if state['state'] == 'connected':
                state['state'] = 'closed'
        if any(isinstance(outcome, BaseException) for outcome in outcomes):
            raise RuntimeError('MCP cleanup failed')
