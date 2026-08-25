from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from mewcode.config import MCPServerConfig, load_config
from mewcode.mcp.client import MCPClient
from mewcode.validator import ConfigError


@pytest.mark.asyncio
async def test_mcp_tool_call_honors_per_server_timeout() -> None:
    config = MCPServerConfig(
        name="slow",
        command="fixture",
        tool_timeout=0.01,
    )
    client = MCPClient(config)

    async def slow_call(name: str, arguments: dict):
        await asyncio.sleep(60)

    client._session = SimpleNamespace(call_tool=slow_call)

    with pytest.raises(asyncio.TimeoutError):
        await client.call_tool("slow", {})


def test_mcp_timeouts_load_from_config(tmp_path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        """
providers:
  - {name: p, protocol: openai, base_url: https://api.openai.com/v1, model: gpt-4.1}
mcp_servers:
  - name: bounded
    command: fixture
    connect_timeout: 2.5
    tool_timeout: 7
""",
        encoding="utf-8",
    )

    config = load_config(path)

    assert config.mcp_servers[0].connect_timeout == 2.5
    assert config.mcp_servers[0].tool_timeout == 7.0


@pytest.mark.parametrize("field", ["connect_timeout", "tool_timeout"])
@pytest.mark.parametrize("value", [0, -1, "slow", True])
def test_mcp_timeouts_reject_non_positive_or_non_numeric_values(
    tmp_path, field: str, value: object
) -> None:
    path = tmp_path / f"{field}.yaml"
    path.write_text(
        "\n".join(
            [
                "providers:",
                "  - {name: p, protocol: openai, base_url: https://api.openai.com/v1, model: gpt-4.1}",
                "mcp_servers:",
                "  - name: bounded",
                "    command: fixture",
                f"    {field}: {str(value).lower() if isinstance(value, bool) else value}",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match=field):
        load_config(path)
