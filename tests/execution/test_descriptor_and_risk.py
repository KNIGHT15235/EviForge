from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from mewcode.execution import ReasonCode, RiskEngine, RiskLevel, ToolDescriptor, policy_for
from mewcode.tools.base import Tool, ToolResult


class Params(BaseModel):
    destination_path: str


class NetworkTool(Tool):
    name = "NetworkTool"
    description = "fetch a remote resource"
    params_model = Params
    category = "command"
    risk_tags = frozenset({"network"})
    side_effect = "network"
    idempotency = "conditional"
    timeout_seconds = 4

    async def execute(self, params: Params) -> ToolResult:
        return ToolResult(output="ok")


class CredentialTool(NetworkTool):
    name = "CredentialTool"
    risk_tags = frozenset({"credential"})


class ForbiddenTool(NetworkTool):
    name = "ForbiddenTool"
    risk_tags = frozenset({"policy_bypass"})


def test_descriptor_derives_and_preserves_policy_metadata() -> None:
    descriptor = ToolDescriptor.from_tool(NetworkTool())

    assert descriptor.name == "NetworkTool"
    assert descriptor.side_effect == "network"
    assert descriptor.idempotency == "conditional"
    assert descriptor.timeout_seconds == 4
    assert descriptor.path_fields == frozenset({"destination_path"})
    assert descriptor.risk_tags == frozenset({"network"})


def test_tool_tags_can_raise_risk(tmp_path: Path) -> None:
    engine = RiskEngine(workspace_root=tmp_path)

    network = engine.assess(
        ToolDescriptor.from_tool(NetworkTool()),
        {"destination_path": "download.bin"},
    )
    credential = engine.assess(
        ToolDescriptor.from_tool(CredentialTool()),
        {"destination_path": "token.txt"},
    )
    forbidden = engine.assess(
        ToolDescriptor.from_tool(ForbiddenTool()),
        {"destination_path": "x.txt"},
    )

    assert network.level is RiskLevel.L2
    assert ReasonCode.L2_NETWORK_ACCESS.value in network.reason_codes
    assert credential.level is RiskLevel.L3
    assert ReasonCode.L3_CREDENTIAL_ACCESS.value in credential.reason_codes
    assert forbidden.level is RiskLevel.L4
    assert forbidden.hard_deny
    assert ReasonCode.L4_POLICY_FORBIDDEN.value in forbidden.reason_codes


def test_policy_maps_risk_to_explainable_approval_scope(tmp_path: Path) -> None:
    engine = RiskEngine(workspace_root=tmp_path)
    network = engine.assess(
        ToolDescriptor.from_tool(NetworkTool()),
        {"destination_path": "download.bin"},
    )
    forbidden = engine.assess(
        ToolDescriptor.from_tool(ForbiddenTool()),
        {"destination_path": "x.txt"},
    )

    assert policy_for(network).effect == "ask"
    assert policy_for(network).approval_scope == "session_batch"
    assert policy_for(forbidden).effect == "deny"
    assert policy_for(forbidden).approval_scope == "not_approvable"
