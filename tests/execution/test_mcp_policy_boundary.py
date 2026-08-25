from __future__ import annotations

from unittest.mock import MagicMock

from mcp import types as mcp_types

from mewcode.config import MCPServerConfig
from mewcode.execution import (
    ExecutionContext,
    ExecutionGateway,
    ReasonCode,
    RiskEngine,
    RiskLevel,
    ToolDescriptor,
)
from mewcode.mcp.client import MCPClient, MCPTransportMetadata
from mewcode.mcp.tool_wrapper import MCPToolWrapper


def _tool_def(*, with_url: bool = False) -> mcp_types.Tool:
    properties = {"url": {"type": "string"}} if with_url else {"query": {"type": "string"}}
    return mcp_types.Tool(
        name="search",
        description="Search through an MCP server",
        inputSchema={"type": "object", "properties": properties},
    )


def _wrapper(config: MCPServerConfig, *, with_url: bool = False) -> MCPToolWrapper:
    client = MCPClient(config)
    return MCPToolWrapper(config.name, _tool_def(with_url=with_url), client)


def _planned_context(
    tmp_path,
    *,
    commands: tuple[tuple[str, ...], ...] = (),
    network_hosts: tuple[str, ...] = (),
) -> ExecutionContext:
    return ExecutionContext(
        task_id="task-mcp",
        cwd=str(tmp_path),
        workspace_root=str(tmp_path),
        plan_hash="plan-mcp",
        commands=commands,
        network_hosts=network_hosts,
    )


def test_http_transport_metadata_reaches_descriptor_and_risk_engine() -> None:
    wrapper = _wrapper(
        MCPServerConfig(name="remote", url="https://API.Example.com:8443/mcp")
    )

    descriptor = ToolDescriptor.from_tool(wrapper)
    decision = RiskEngine().assess(descriptor, {"query": "release notes"})

    assert descriptor.category == "command"
    assert descriptor.side_effect == "network"
    assert descriptor.transport_kind == "http"
    assert descriptor.destination_hosts == ("api.example.com",)
    assert decision.level is RiskLevel.L2
    assert ReasonCode.L2_NETWORK_ACCESS.value in decision.reason_codes
    assert ReasonCode.L2_MCP_HTTP_TRANSPORT.value in decision.reason_codes


def test_transport_metadata_owns_category_and_side_effect() -> None:
    stdio = MCPTransportMetadata.from_config(
        MCPServerConfig(name="local", command="python", args=["server.py"])
    )
    http = MCPTransportMetadata.from_config(
        MCPServerConfig(name="remote", url="https://api.example/mcp")
    )

    assert (stdio.category, stdio.side_effect) == ("command", "process")
    assert (http.category, http.side_effect) == ("command", "network")


def test_transport_metadata_rejects_unbound_http_endpoint() -> None:
    for endpoint in ("relative/mcp", "file:///tmp/mcp", "https:///missing-host"):
        try:
            MCPTransportMetadata.from_config(
                MCPServerConfig(name="invalid", url=endpoint)
            )
        except ValueError as exc:
            assert "absolute HTTP(S) endpoint" in str(exc)
        else:
            raise AssertionError(f"invalid endpoint was accepted: {endpoint}")


def test_http_mcp_requires_its_configured_host_in_plan(tmp_path) -> None:
    descriptor = ToolDescriptor.from_tool(
        _wrapper(MCPServerConfig(name="remote", url="https://api.example/mcp"))
    )

    denied_empty = _planned_context(tmp_path).constraint_error(descriptor, {"query": "x"})
    denied_other = _planned_context(
        tmp_path, network_hosts=("other.example",)
    ).constraint_error(descriptor, {"query": "x"})
    allowed = _planned_context(
        tmp_path, network_hosts=("API.EXAMPLE.",)
    ).constraint_error(descriptor, {"query": "x"})

    assert denied_empty is not None and denied_empty[0] == "manifest.network_violation"
    assert denied_other is not None and denied_other[0] == "manifest.network_violation"
    assert allowed is None


def test_gateway_preview_reports_http_transport_and_empty_plan_denial(tmp_path) -> None:
    wrapper = _wrapper(MCPServerConfig(name="remote", url="https://api.example/mcp"))
    gateway = ExecutionGateway(execution_context=_planned_context(tmp_path))

    assessment = gateway.preview(wrapper, {"query": "x"})

    assert not assessment.valid
    assert ReasonCode.L2_MCP_HTTP_TRANSPORT.value in assessment.reason_codes
    assert ReasonCode.MANIFEST_NETWORK_VIOLATION.value in assessment.reason_codes


def test_http_mcp_argument_destination_is_also_constrained(tmp_path) -> None:
    descriptor = ToolDescriptor.from_tool(
        _wrapper(
            MCPServerConfig(name="remote", url="https://api.example/mcp"),
            with_url=True,
        )
    )
    context = _planned_context(tmp_path, network_hosts=("api.example",))

    violation = context.constraint_error(
        descriptor, {"url": "https://unapproved.example/resource"}
    )

    assert violation is not None
    assert violation[0] == "manifest.network_violation"
    assert "unapproved.example" in violation[1]


def test_nested_destination_arguments_are_constrained_for_stdio_mcp(tmp_path) -> None:
    descriptor = ToolDescriptor.from_tool(
        _wrapper(MCPServerConfig(name="local", command="python", args=["server.py"]))
    )
    context = _planned_context(
        tmp_path,
        commands=(("python", "server.py"),),
        network_hosts=(),
    )

    violation = context.constraint_error(
        descriptor,
        {"options": {"callback_urls": ["https://unapproved.example/hook"]}},
    )

    assert violation is not None
    assert violation[0] == "manifest.network_violation"
    assert "unapproved.example" in violation[1]


def test_stdio_mcp_is_bound_to_exact_host_configured_argv(tmp_path) -> None:
    descriptor = ToolDescriptor.from_tool(
        _wrapper(
            MCPServerConfig(
                name="local",
                command="python",
                args=["-m", "trusted_mcp"],
            )
        )
    )
    decision = RiskEngine().assess(descriptor, {"query": "x"})

    assert descriptor.side_effect == "process"
    assert descriptor.transport_kind == "stdio"
    assert descriptor.transport_command == ("python", "-m", "trusted_mcp")
    assert decision.level is RiskLevel.L2
    assert ReasonCode.L2_MCP_STDIO_TRANSPORT.value in decision.reason_codes
    assert _planned_context(tmp_path).constraint_error(descriptor, {"query": "x"}) == (
        "manifest.command_violation",
        "MCP stdio transport argv is not approved",
    )
    assert (
        _planned_context(
            tmp_path,
            commands=(("python", "-m", "trusted_mcp"),),
        ).constraint_error(descriptor, {"query": "x"})
        is None
    )


def test_unplanned_local_stdio_mcp_remains_executable(tmp_path) -> None:
    wrapper = _wrapper(
        MCPServerConfig(name="local", command="python", args=["server.py"])
    )
    gateway = ExecutionGateway(
        execution_context=ExecutionContext.unplanned(task_id="local", cwd=tmp_path)
    )

    assessment = gateway.preview(wrapper, {"query": "x"})

    assert assessment.valid
    assert assessment.risk is not None and assessment.risk.level is RiskLevel.L2
    assert ReasonCode.L2_MCP_STDIO_TRANSPORT.value in assessment.reason_codes


def test_dangerous_stdio_transport_is_hard_denied() -> None:
    descriptor = ToolDescriptor.from_tool(
        _wrapper(MCPServerConfig(name="bad", command="rm", args=["-rf", "/"]))
    )

    decision = RiskEngine().assess(descriptor, {"query": "x"})

    assert decision.level is RiskLevel.L4
    assert decision.hard_deny
    assert ReasonCode.L4_DANGEROUS_COMMAND.value in decision.reason_codes


def test_unbound_legacy_mcp_is_compatible_unplanned_but_blocked_by_plan(tmp_path) -> None:
    client = MagicMock(spec=MCPClient)
    client.is_alive = True
    wrapper = MCPToolWrapper("legacy", _tool_def(), client)
    descriptor = ToolDescriptor.from_tool(wrapper)
    decision = RiskEngine().assess(descriptor, {"query": "x"})

    assert descriptor.transport_kind == "unknown"
    assert decision.level is RiskLevel.L3
    assert ReasonCode.L3_MCP_TRANSPORT_UNKNOWN.value in decision.reason_codes
    violation = _planned_context(tmp_path).constraint_error(descriptor, {"query": "x"})
    assert violation is not None
    assert violation[0] == ReasonCode.MANIFEST_TRANSPORT_UNBOUND.value
    assert ExecutionContext.unplanned(task_id="legacy", cwd=tmp_path).constraint_error(
        descriptor, {"query": "x"}
    ) is None


def test_client_transport_snapshot_is_immutable_after_config_mutation() -> None:
    config = MCPServerConfig(name="remote", url="https://approved.example/mcp")
    client = MCPClient(config)

    config.url = "https://changed.example/mcp"

    assert client.transport_metadata.endpoint_url == "https://approved.example/mcp"
    assert client.transport_metadata.destination_hosts == ("approved.example",)
