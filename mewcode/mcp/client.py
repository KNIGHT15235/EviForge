from __future__ import annotations

import asyncio
import logging
import os
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlparse

import httpx
from mcp import ClientSession, types
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client

from mewcode.config import MCPServerConfig, build_child_env, resolve_env_vars

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class MCPTransportMetadata:
    """Immutable, host-derived identity of an MCP server transport.

    Tool schemas and annotations come from the MCP server and are therefore
    untrusted policy input.  Transport identity instead comes from the local
    configuration and is snapshotted before connecting so policy assessment
    and the connection cannot observe different destinations (TOCTOU).
    """

    kind: Literal["stdio", "http"]
    command_argv: tuple[str, ...] = ()
    endpoint_url: str | None = None
    destination_hosts: tuple[str, ...] = ()

    @property
    def category(self) -> Literal["command"]:
        return "command"

    @property
    def side_effect(self) -> Literal["process", "network"]:
        return "process" if self.kind == "stdio" else "network"

    @classmethod
    def from_config(cls, config: MCPServerConfig) -> "MCPTransportMetadata":
        if config.is_stdio:
            assert config.command is not None
            if not str(config.command).strip():
                raise ValueError(f"MCP server {config.name!r} has an empty stdio command")
            return cls(
                kind="stdio",
                command_argv=(str(config.command), *(str(arg) for arg in config.args)),
            )

        assert config.url is not None
        endpoint = str(config.url)
        parsed = urlparse(endpoint)
        if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
            raise ValueError(
                f"MCP server {config.name!r} requires an absolute HTTP(S) endpoint"
            )
        host = parsed.hostname.casefold().rstrip(".")
        return cls(
            kind="http",
            endpoint_url=endpoint,
            destination_hosts=(host,),
        )


class MCPClient:
    def __init__(self, config: MCPServerConfig) -> None:
        self.config = config
        self.name = config.name
        self.transport_metadata = MCPTransportMetadata.from_config(config)
        self._transport_env = dict(config.env)
        self._transport_headers = dict(config.headers)
        self._session: ClientSession | None = None
        self._stack: AsyncExitStack | None = None
        self._alive = False


    @property
    def is_alive(self) -> bool:
        return self._alive


    async def connect(self) -> None:
        if self._alive:
            return

        self._stack = AsyncExitStack()
        await self._stack.__aenter__()

        try:
            if self.transport_metadata.kind == "stdio":
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
        except BaseException:
            await self._cleanup_stack()
            raise


    async def _connect_stdio(self) -> tuple[Any, Any]:
        assert self._stack is not None
        argv = self.transport_metadata.command_argv
        assert argv

        params = StdioServerParameters(
            command=argv[0],
            args=list(argv[1:]),
            env=build_child_env(self._transport_env),
        )
        devnull = open(os.devnull, "w")
        self._stack.callback(devnull.close)
        read, write = await self._stack.enter_async_context(
            stdio_client(params, errlog=devnull)
        )
        return read, write

    async def _connect_http(self) -> tuple[Any, Any]:
        assert self._stack is not None
        endpoint = self.transport_metadata.endpoint_url
        assert endpoint is not None

        resolved_headers = {
            k: resolve_env_vars(v) for k, v in self._transport_headers.items()
        }
        http_client = httpx.AsyncClient(
            headers=resolved_headers,
            # A redirect may silently cross the Plan-approved host boundary.
            # MCP endpoints must therefore be configured at their final URL.
            follow_redirects=False,
        )
        await self._stack.enter_async_context(http_client)

        result = await self._stack.enter_async_context(
            streamable_http_client(endpoint, http_client=http_client)
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
        return await asyncio.wait_for(
            self._session.call_tool(name, arguments),
            timeout=self.config.tool_timeout,
        )

    async def close(self) -> None:
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
