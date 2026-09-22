from __future__ import annotations

import logging
import os
import asyncio
from contextlib import AsyncExitStack
from typing import Any

import httpx
from mcp import ClientSession, types
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client

from eviforge.config import MCPServerConfig, build_child_env, resolve_env_vars

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


    @property
    def is_alive(self) -> bool:
        return self._alive


    async def connect(self) -> None:
        if self._alive:
            return

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
            await session.initialize()
            self._session = session
            self._alive = True
            logger.info("MCP server '%s' connected", self.name)
        except Exception:
            await self._cleanup_stack()
            raise


    async def _connect_stdio(self) -> tuple[Any, Any]:
        assert self._stack is not None
        assert self.config.command is not None

        params = StdioServerParameters(
            command=self.config.command,
            args=self.config.args,
            env=build_child_env(self.config.env),
        )
        devnull = open(os.devnull, "w")
        self._stack.callback(devnull.close)
        read, write = await self._stack.enter_async_context(
            stdio_client(params, errlog=devnull)
        )
        return read, write

    async def _connect_http(self) -> tuple[Any, Any]:
        assert self._stack is not None
        assert self.config.url is not None

        resolved_headers = {
            k: resolve_env_vars(v) for k, v in self.config.headers.items()
        }
        http_client = httpx.AsyncClient(
            headers=resolved_headers,
            follow_redirects=True,
        )
        await self._stack.enter_async_context(http_client)

        result = await self._stack.enter_async_context(
            streamable_http_client(self.config.url, http_client=http_client)
        )
        read, write = result[0], result[1]
        return read, write


    async def list_tools(self) -> list[types.Tool]:
        assert self._session is not None
        result = await self._session.list_tools()
        return list(result.tools)


    async def call_tool(
        self, name: str, arguments: dict[str, Any]
    ) -> types.CallToolResult:
        assert self._session is not None
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
