from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import AsyncMock, patch

import pytest
from mcp import types as mcp_types

from mewcode.config import MCPServerConfig
from mewcode.mcp import inspect_mcp_servers


def _tool(name: str = "search") -> mcp_types.Tool:
    return mcp_types.Tool(
        name=name,
        description="fixture tool",
        inputSchema={"type": "object", "properties": {}},
    )


@pytest.mark.asyncio
async def test_inspection_is_parallel_and_failure_does_not_block_healthy_server() -> None:
    slow_config = MCPServerConfig(name="slow", command="slow")
    good_config = MCPServerConfig(name="good", command="good")
    slow_client = AsyncMock()
    good_client = AsyncMock()

    async def hang() -> None:
        await asyncio.sleep(10)

    slow_client.connect.side_effect = hang
    good_client.list_tools.return_value = [_tool()]

    with patch("mewcode.mcp.manager.MCPClient") as client_type:
        client_type.side_effect = (
            lambda config: slow_client if config.name == "slow" else good_client
        )
        started = time.monotonic()
        result = await inspect_mcp_servers(
            [slow_config, good_config],
            total_timeout=0.03,
        )
        elapsed = time.monotonic() - started

    assert elapsed < 0.5
    assert result.ok is False
    payload = result.as_dict()
    assert payload["partial"] is True
    by_name = {item.name: item for item in result.servers}
    assert by_name["good"].healthy is True
    assert by_name["good"].tool_names == ("mcp_good_search",)
    assert by_name["slow"].state == "unavailable"
    assert by_name["slow"].diagnostics[0].code == "mcp_initialization_failed"
    slow_client.close.assert_awaited_once()
    good_client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_one_deadline_covers_connect_and_list() -> None:
    config = MCPServerConfig(name="cumulative", command="fixture")
    client = AsyncMock()

    async def slow_connect() -> None:
        await asyncio.sleep(0.03)

    async def slow_list() -> list[mcp_types.Tool]:
        await asyncio.sleep(0.03)
        return []

    client.connect.side_effect = slow_connect
    client.list_tools.side_effect = slow_list
    with patch("mewcode.mcp.manager.MCPClient", return_value=client):
        result = await inspect_mcp_servers([config], total_timeout=0.05)

    assert result.ok is False
    assert result.servers[0].state == "unavailable"
    assert "timed out" in result.servers[0].diagnostics[0].message
    client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_result_redacts_header_and_environment_values(monkeypatch) -> None:
    monkeypatch.setenv("MCP_TEST_TOKEN", "resolved-header-secret")
    config = MCPServerConfig(
        name="private",
        url="https://example.test/mcp",
        headers={"Authorization": "${MCP_TEST_TOKEN}"},
        env={"PRIVATE_VALUE": "environment-secret"},
    )
    client = AsyncMock()
    client.connect.side_effect = RuntimeError(
        "rejected resolved-header-secret and environment-secret; "
        "raw=${MCP_TEST_TOKEN}"
    )

    with patch("mewcode.mcp.manager.MCPClient", return_value=client):
        result = await inspect_mcp_servers([config], total_timeout=0.1)

    rendered = json.dumps(result.as_dict(), ensure_ascii=False)
    assert "resolved-header-secret" not in rendered
    assert "environment-secret" not in rendered
    assert "${MCP_TEST_TOKEN}" not in rendered
    assert rendered.count("<redacted>") >= 2
    client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_cleanup_failure_is_explicit_and_redacted() -> None:
    config = MCPServerConfig(
        name="leaky",
        command="fixture",
        env={"TOKEN": "cleanup-secret"},
    )
    client = AsyncMock()
    client.list_tools.return_value = []
    client.close.side_effect = RuntimeError("could not close cleanup-secret")

    with patch("mewcode.mcp.manager.MCPClient", return_value=client):
        result = await inspect_mcp_servers([config], total_timeout=0.1)

    inspection = result.servers[0]
    assert inspection.reachable is True
    assert inspection.state == "cleanup_failed"
    assert inspection.diagnostics[0].code == "mcp_cleanup_failed"
    rendered = json.dumps(result.as_dict())
    assert "cleanup-secret" not in rendered
    client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_test_requires_exact_server_and_unknown_server_is_structured() -> None:
    config = MCPServerConfig(name="known", command="fixture")

    missing = await inspect_mcp_servers([config], operation="test")
    unknown = await inspect_mcp_servers(
        [config], operation="test", server_name="unknown"
    )

    assert missing.ok is False
    assert missing.diagnostics[0].code == "mcp_server_required"
    assert unknown.ok is False
    assert unknown.diagnostics[0].code == "mcp_server_not_found"
    assert unknown.servers == ()


@pytest.mark.asyncio
async def test_one_shot_reconnect_reports_limitation_without_opening_transport() -> None:
    config = MCPServerConfig(name="known", command="fixture")

    with patch("mewcode.mcp.manager.MCPClient") as client_type:
        result = await inspect_mcp_servers(
            [config], operation="reconnect", server_name="known"
        )

    assert result.ok is False
    assert result.diagnostics[0].code == "mcp_reconnect_not_persistent"
    assert "one-shot" in result.diagnostics[0].message
    assert result.servers == ()
    client_type.assert_not_called()


@pytest.mark.asyncio
async def test_list_can_select_one_server() -> None:
    first = MCPServerConfig(name="first", command="fixture")
    second = MCPServerConfig(name="second", command="fixture")
    client = AsyncMock()
    client.list_tools.return_value = [_tool("selected")]

    with patch("mewcode.mcp.manager.MCPClient", return_value=client):
        result = await inspect_mcp_servers(
            [first, second], operation="list", server_name="second"
        )

    assert result.ok is True
    assert [server.name for server in result.servers] == ["second"]
    assert result.servers[0].tool_names == ("mcp_second_selected",)
    client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancellation_waits_for_connecting_transport_cleanup() -> None:
    config = MCPServerConfig(name="cancelled", command="fixture")
    client = AsyncMock()
    connecting = asyncio.Event()

    async def hang() -> None:
        connecting.set()
        await asyncio.sleep(60)

    client.connect.side_effect = hang
    with patch("mewcode.mcp.manager.MCPClient", return_value=client):
        task = asyncio.create_task(
            inspect_mcp_servers([config], total_timeout=30.0)
        )
        await asyncio.wait_for(connecting.wait(), timeout=1.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_duplicate_remote_tool_names_are_reported_once() -> None:
    config = MCPServerConfig(name="duplicate", command="fixture")
    client = AsyncMock()
    client.list_tools.return_value = [_tool("same"), _tool("same")]

    with patch("mewcode.mcp.manager.MCPClient", return_value=client):
        result = await inspect_mcp_servers([config], total_timeout=0.1)

    assert result.ok is True
    assert result.servers[0].tool_names == ("mcp_duplicate_same",)
    assert result.as_dict()["servers"][0]["tool_count"] == 1
