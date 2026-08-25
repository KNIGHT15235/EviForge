from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol

from mewcode.execution import RiskEngine, ToolDescriptor
from mewcode.tools.base import Tool


@dataclass(frozen=True, slots=True)
class AdapterOutput:
    actual: Mapping[str, Any]
    latency_ms: float


class EvalAdapter(Protocol):
    name: str

    async def run(
        self,
        case: Mapping[str, Any],
        *,
        feature_enabled: bool,
        seed: int,
    ) -> AdapterOutput: ...


class _SyntheticTool(Tool):
    """Descriptor-only tool used to benchmark policy classification.

    It never executes.  Fixtures select policy metadata, and both arms execute
    in this same adapter/runtime; only ``feature_enabled`` changes the decision
    path.
    """

    from pydantic import BaseModel

    class Params(BaseModel):
        command: str = ""
        file_path: str = ""

    name = "FixtureTool"
    description = "deterministic evaluation fixture"
    params_model = Params
    category = "read"

    async def execute(self, params: Params):  # pragma: no cover - descriptor only
        raise AssertionError("mechanism benchmark must not execute fixture tools")


class RiskPolicyMechanismAdapter:
    name = "risk-policy-mechanism"

    def __init__(self, *, workspace_root: str | Path) -> None:
        self._workspace_root = Path(workspace_root).resolve()

    async def run(
        self,
        case: Mapping[str, Any],
        *,
        feature_enabled: bool,
        seed: int,
    ) -> AdapterOutput:
        del seed
        started = time.perf_counter()
        tool = _SyntheticTool()
        tool.category = str(case.get("category", "read"))  # type: ignore[assignment]
        tool.risk_tags = frozenset(map(str, case.get("risk_tags", ())))
        if "side_effect" in case:
            tool.side_effect = str(case["side_effect"])

        arguments = dict(case.get("arguments", {}))
        if feature_enabled:
            decision = RiskEngine(
                workspace_root=self._workspace_root,
                enforce_workspace_boundary=True,
            ).assess(ToolDescriptor.from_tool(tool), arguments)
            allowed = not decision.hard_deny
            risk_level = str(decision.level)
            reason_codes = list(decision.reason_codes)
        else:
            allowed, risk_level, reason_codes = _legacy_policy(tool, arguments)

        elapsed = (time.perf_counter() - started) * 1000.0
        return AdapterOutput(
            actual={
                "decision": "allow" if allowed else "deny",
                "allowed": allowed,
                "risk_level": risk_level,
                "reason_codes": reason_codes,
            },
            latency_ms=elapsed,
        )


def _legacy_policy(tool: Tool, arguments: Mapping[str, Any]) -> tuple[bool, str, list[str]]:
    """Frozen comparator: old category policy without the new risk layer."""

    del arguments
    if tool.category == "read":
        return True, "legacy.read", ["legacy.category_allow"]
    return False, "legacy.ask", ["legacy.side_effect_not_preapproved"]


ADAPTERS = {"risk-policy-mechanism": RiskPolicyMechanismAdapter}
