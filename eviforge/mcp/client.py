from __future__ import annotations

import logging
import os
import asyncio
from pathlib import Path
from contextlib import AsyncExitStack
from typing import Any

import httpx
from mcp import ClientSession, types
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client

from eviforge.config import MCPServerConfig, ConfigError, build_child_env, resolve_env_vars
from eviforge.mcp.diagnostics import resolve_required

logger = logging.getLogger(__name__)


class MCPClient:
    def __init__(self, config: MCPServerConfig) -> None:
        self.config = config
        self.name = config.name
        self._session: ClientSession | None = None
        self._stack: AsyncExitStack | None = None
        self._alive = False
        self._owner_task: asyncio.Task | None = None
        self._stop: asyncio.Event | None = None
        self.server_info: dict[str, Any] = {}
        self._connect_lock = asyncio.Lock()
        self.generation = 0


    @property
    def is_alive(self) -> bool:
        return self._alive


    async def connect(self) -> None:
        async with self._connect_lock:
            await self._connect_serial()

    async def _connect_serial(self) -> None:
        if self._alive:
            return
        if self.config.integration == "playwright" and not self.config.is_stdio:
            raise ConfigError("Managed Playwright requires the locally guarded stdio transport")

        if self._owner_task is not None:
            await self.close()
        self._stop = asyncio.Event()
        ready = asyncio.get_running_loop().create_future()

        async def own_connection() -> None:
            try:
                await self._connect_owned()
                if not ready.done():
                    ready.set_result(None)
                await self._stop.wait()
            except BaseException as exc:
                if not ready.done():
                    ready.set_exception(exc)
                else:
                    raise
            finally:
                self._alive = False
                self._session = None
                await self._cleanup_stack()

        self._owner_task = asyncio.create_task(own_connection(), name=f"mcp-{self.name}")
        try:
            async with asyncio.timeout(self.config.startup_timeout_seconds):
                await asyncio.shield(ready)
        except BaseException:
            if not ready.done():
                ready.cancel()
            self._owner_task.cancel()
            await asyncio.gather(self._owner_task, return_exceptions=True)
            self._owner_task = None
            raise

    async def _connect_owned(self) -> None:
        """Enter and exit all AnyIO transport scopes from the same owner task."""

        self._stack = AsyncExitStack()
        await self._stack.__aenter__()

        try:
            if self.config.is_stdio:
                read, write = await self._connect_stdio()
            else:
                read, write = await self._connect_http()

            session = await self._stack.enter_async_context(
                ClientSession(read, write)
            )
            initialized = await session.initialize()
            self.server_info = {"name": initialized.serverInfo.name, "version": initialized.serverInfo.version, "protocol_version": initialized.protocolVersion}
            self._session = session
            self._alive = True
            self.generation += 1
            logger.info("MCP server '%s' connected", self.name)
        except Exception:
            await self._cleanup_stack()
            raise


    async def _connect_stdio(self) -> tuple[Any, Any]:
        assert self._stack is not None
        assert self.config.command is not None

        args = [resolve_required(value) for value in self.config.args]
        cwd = resolve_required(self.config.cwd) if self.config.cwd else None
        if self.config.integration == "playwright":
            from eviforge.mcp.browser_guard import guarded_args
            args = guarded_args(self.config, args, Path(cwd or Path.cwd()))
        params = StdioServerParameters(
            command=resolve_required(self.config.command),
            args=args,
            env=self._child_env(),
            cwd=cwd,
        )
        devnull = open(os.devnull, "w")
        self._stack.callback(devnull.close)
        read, write = await self._stack.enter_async_context(
            stdio_client(params, errlog=devnull)
        )
        return read, write

    def _child_env(self) -> dict[str, str]:
        declared = {key: resolve_required(value) for key, value in self.config.env.items()}
        env = build_child_env(declared)
        for key in ("SYSTEMROOT", "SystemRoot", "SystemDrive", "COMSPEC", "PATHEXT", "TEMP", "TMP", "HOME", "USERPROFILE", "LANG", "LOCALAPPDATA", "APPDATA", "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "ALLUSERSPROFILE"):
            if key in os.environ and key not in env:
                env[key] = os.environ[key]
        return env

    async def _connect_http(self) -> tuple[Any, Any]:
        assert self._stack is not None
        assert self.config.url is not None

        resolved_headers = {
            k: resolve_required(v) for k, v in self.config.headers.items()
        }
        http_client = httpx.AsyncClient(
            headers=resolved_headers,
            follow_redirects=False,
            timeout=self.config.call_timeout_seconds,
        )
        await self._stack.enter_async_context(http_client)

        result = await self._stack.enter_async_context(
            streamable_http_client(resolve_required(self.config.url), http_client=http_client)
        )
        read, write = result[0], result[1]
        return read, write


    async def list_tools(self) -> list[types.Tool]:
        assert self._session is not None
        tools: list[types.Tool] = []
        cursors: set[str] = set()
        cursor = None
        async with asyncio.timeout(self.config.startup_timeout_seconds):
            for _ in range(100):
                result = await self._session.list_tools(cursor=cursor) if cursor else await self._session.list_tools()
                tools.extend(result.tools)
                cursor = result.nextCursor
                if not cursor:
                    return tools
                if cursor in cursors:
                    raise ValueError("Repeated MCP tool pagination cursor")
                cursors.add(cursor)
        raise ValueError("MCP tool pagination limit exceeded")


    async def call_tool(
        self, name: str, arguments: dict[str, Any]
    ) -> types.CallToolResult:
        assert self._session is not None
        async with asyncio.timeout(self.config.call_timeout_seconds):
            return await self._session.call_tool(name, arguments)

    async def close(self) -> None:
        owner = self._owner_task
        if owner is not None:
            if self._stop is not None:
                self._stop.set()
            try:
                await asyncio.shield(owner)
            except asyncio.CancelledError:
                owner.cancel()
                await asyncio.gather(owner, return_exceptions=True)
                raise
            finally:
                self._owner_task = None
        self._alive = False
        self._session = None
        await self._cleanup_stack()

    async def _cleanup_stack(self) -> None:
        if self._stack is not None:
            try:
                await self._stack.__aexit__(None, None, None)
            except RuntimeError as e:
                if "cancel scope" in str(e):
                    logger.debug("Cancel scope cleanup (expected during shutdown): %s", e)
                else:
                    raise
            except Exception:
                logger.debug("Error closing stack for '%s'", self.name, exc_info=True)
            self._stack = None
