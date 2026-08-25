from __future__ import annotations

import os
from dataclasses import dataclass
from enum import IntEnum, StrEnum
from pathlib import Path
from typing import Any, Iterable, Mapping

from mewcode.execution.descriptor import ToolDescriptor
from mewcode.permissions.dangerous import DangerousCommandDetector, is_safe_command


class RiskLevel(IntEnum):
    L0 = 0
    L1 = 1
    L2 = 2
    L3 = 3
    L4 = 4

    def __str__(self) -> str:
        return self.name


class ReasonCode(StrEnum):
    L0_READ_ONLY = "risk.l0.read_only"
    L0_SAFE_COMMAND = "risk.l0.safe_command"
    L1_REVERSIBLE_WRITE = "risk.l1.reversible_local_write"
    L2_COMMAND_EXECUTION = "risk.l2.command_execution"
    L2_NETWORK_ACCESS = "risk.l2.network_access"
    L2_MCP_STDIO_TRANSPORT = "risk.l2.mcp_stdio_transport"
    L2_MCP_HTTP_TRANSPORT = "risk.l2.mcp_http_transport"
    L3_DESTRUCTIVE = "risk.l3.destructive_operation"
    L3_EXTERNAL_WRITE = "risk.l3.external_side_effect"
    L3_CREDENTIAL_ACCESS = "risk.l3.credential_access"
    L3_MCP_TRANSPORT_UNKNOWN = "risk.l3.mcp_transport_unknown"
    L4_RESERVED_PATH = "risk.l4.reserved_path"
    L4_DANGEROUS_COMMAND = "risk.l4.dangerous_command"
    L4_SANDBOX_ESCAPE = "risk.l4.sandbox_escape"
    L4_POLICY_FORBIDDEN = "risk.l4.policy_forbidden"

    VALIDATION_TOOL_MISMATCH = "validation.tool_mismatch"
    VALIDATION_SCHEMA_INVALID = "validation.schema_invalid"
    VALIDATION_UNEXPECTED_ARGUMENT = "validation.unexpected_argument"
    PERMISSION_ALLOWED = "permission.allowed"
    PERMISSION_DENIED = "permission.denied"
    PERMISSION_APPROVAL_REQUIRED = "permission.approval_required"
    TOOL_SUCCEEDED = "tool.succeeded"
    TOOL_REPORTED_ERROR = "tool.reported_error"
    TOOL_RAISED_EXCEPTION = "tool.raised_exception"
    TRACE_HOOK_FAILED = "trace.hook_failed"
    MANIFEST_WRITE_SET_VIOLATION = "manifest.write_set_violation"
    MANIFEST_WRITE_SET_MISSING_TARGET = "manifest.write_set_missing_target"
    MANIFEST_COMMAND_VIOLATION = "manifest.command_violation"
    MANIFEST_NETWORK_VIOLATION = "manifest.network_violation"
    MANIFEST_NETWORK_HOST_MISSING = "manifest.network_host_missing"
    MANIFEST_TRANSPORT_UNBOUND = "manifest.transport_unbound"
    INVOCATION_GRANT_INVALID = "permission.invocation_grant_invalid"
    INVOCATION_GRANT_CONSUMED = "permission.invocation_grant_consumed"


@dataclass(frozen=True, slots=True)
class RiskDecision:
    level: RiskLevel
    reason_codes: tuple[str, ...]
    hard_deny: bool = False
    matched_paths: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RiskPolicy:
    """Explainable authorization policy for L0-L4 decisions."""

    level: RiskLevel
    effect: str
    approval_scope: str


_RISK_POLICIES: Mapping[RiskLevel, RiskPolicy] = {
    RiskLevel.L0: RiskPolicy(RiskLevel.L0, "allow", "none"),
    RiskLevel.L1: RiskPolicy(RiskLevel.L1, "allow", "audited_session"),
    RiskLevel.L2: RiskPolicy(RiskLevel.L2, "ask", "session_batch"),
    RiskLevel.L3: RiskPolicy(RiskLevel.L3, "ask", "single_use_bound_ticket"),
    RiskLevel.L4: RiskPolicy(RiskLevel.L4, "deny", "not_approvable"),
}


def policy_for(decision: RiskDecision) -> RiskPolicy:
    """Return deterministic policy; a hard deny can never become approvable."""

    if decision.hard_deny:
        return _RISK_POLICIES[RiskLevel.L4]
    return _RISK_POLICIES[decision.level]


class RiskEngine:
    """Deterministic minimum L0-L4 classifier.

    The engine is intentionally local-policy owned.  Tool-provided tags can
    raise risk, never lower a risk derived from category, command, or path.
    """

    def __init__(
        self,
        *,
        workspace_root: str | Path | None = None,
        control_plane_roots: Iterable[str | Path] = (),
        reserved_paths: Iterable[str | Path] = (),
        allowed_roots: Iterable[str | Path] = (),
        enforce_workspace_boundary: bool = False,
        detector: DangerousCommandDetector | None = None,
    ) -> None:
        self.workspace_root = (
            Path(workspace_root).expanduser().resolve() if workspace_root is not None else None
        )
        self.enforce_workspace_boundary = enforce_workspace_boundary
        self.detector = detector or DangerousCommandDetector()

        paths: list[Path] = []
        if self.workspace_root is not None:
            paths.extend(self.workspace_root / relative for relative in _DEFAULT_RESERVED_PATHS)
        paths.extend(self._resolve_configured(path) for path in control_plane_roots)
        paths.extend(self._resolve_configured(path) for path in reserved_paths)
        self._reserved_paths = tuple(_deduplicate_paths(paths))

        roots = [self._resolve_configured(path) for path in allowed_roots]
        if self.workspace_root is not None:
            roots.insert(0, self.workspace_root)
        self._allowed_roots = tuple(_deduplicate_paths(roots))

    def assess(
        self,
        descriptor: ToolDescriptor,
        arguments: Mapping[str, Any],
    ) -> RiskDecision:
        level, codes = self._base_risk(descriptor, arguments)
        hard_deny = level is RiskLevel.L4
        matched_paths: list[str] = []

        for raw_path in _extract_paths(arguments, descriptor.path_fields):
            resolved = self._resolve_argument_path(raw_path)
            if any(_is_within(resolved, reserved) for reserved in self._reserved_paths):
                level = RiskLevel.L4
                hard_deny = True
                codes.add(ReasonCode.L4_RESERVED_PATH.value)
                matched_paths.append(str(resolved))
                continue
            if (
                self.enforce_workspace_boundary
                and self._allowed_roots
                and not any(_is_within(resolved, root) for root in self._allowed_roots)
            ):
                level = RiskLevel.L4
                hard_deny = True
                codes.add(ReasonCode.L4_SANDBOX_ESCAPE.value)
                matched_paths.append(str(resolved))

        ordered_codes = tuple(sorted(codes))
        return RiskDecision(
            level=level,
            reason_codes=ordered_codes,
            hard_deny=hard_deny,
            matched_paths=tuple(matched_paths),
        )

    def _base_risk(
        self, descriptor: ToolDescriptor, arguments: Mapping[str, Any]
    ) -> tuple[RiskLevel, set[str]]:
        tags = descriptor.risk_tags
        if tags & _FORBIDDEN_TAGS:
            return RiskLevel.L4, {ReasonCode.L4_POLICY_FORBIDDEN.value}

        command = str(arguments.get("command", ""))
        if not command and descriptor.transport_command:
            command = " ".join(descriptor.transport_command)
        if descriptor.category == "command" and command:
            dangerous, _ = self.detector.detect(command)
            if dangerous:
                return RiskLevel.L4, {ReasonCode.L4_DANGEROUS_COMMAND.value}

        if tags & _L3_CREDENTIAL_TAGS:
            return RiskLevel.L3, {ReasonCode.L3_CREDENTIAL_ACCESS.value}
        if "mcp_transport_unknown" in tags:
            return RiskLevel.L3, {ReasonCode.L3_MCP_TRANSPORT_UNKNOWN.value}
        if tags & _L3_EXTERNAL_WRITE_TAGS or descriptor.side_effect == "external_write":
            return RiskLevel.L3, {ReasonCode.L3_EXTERNAL_WRITE.value}
        if tags & _L3_DESTRUCTIVE_TAGS:
            return RiskLevel.L3, {ReasonCode.L3_DESTRUCTIVE.value}

        if tags & _NETWORK_TAGS or descriptor.side_effect == "network":
            codes = {ReasonCode.L2_NETWORK_ACCESS.value}
            if "mcp_http_transport" in tags:
                codes.add(ReasonCode.L2_MCP_HTTP_TRANSPORT.value)
            return RiskLevel.L2, codes
        if "mcp_stdio_transport" in tags:
            return RiskLevel.L2, {
                ReasonCode.L2_COMMAND_EXECUTION.value,
                ReasonCode.L2_MCP_STDIO_TRANSPORT.value,
            }
        if descriptor.category == "command":
            if is_safe_command(command):
                return RiskLevel.L0, {ReasonCode.L0_SAFE_COMMAND.value}
            return RiskLevel.L2, {ReasonCode.L2_COMMAND_EXECUTION.value}
        if descriptor.category == "write":
            return RiskLevel.L1, {ReasonCode.L1_REVERSIBLE_WRITE.value}
        return RiskLevel.L0, {ReasonCode.L0_READ_ONLY.value}

    def _resolve_configured(self, path: str | Path) -> Path:
        candidate = Path(path).expanduser()
        if not candidate.is_absolute() and self.workspace_root is not None:
            candidate = self.workspace_root / candidate
        return candidate.resolve(strict=False)

    def _resolve_argument_path(self, path: str) -> Path:
        candidate = Path(path).expanduser()
        if not candidate.is_absolute() and self.workspace_root is not None:
            candidate = self.workspace_root / candidate
        return candidate.resolve(strict=False)


_DEFAULT_RESERVED_PATHS = (
    Path(".git"),
    Path(".eviforge") / "runtime.db",
    Path(".eviforge") / "control",
    Path(".eviforge") / "approvals",
    Path(".eviforge") / "journal",
    Path(".eviforge") / "secrets",
    Path(".mewcode") / "runtime.db",
)
_FORBIDDEN_TAGS = frozenset({"forbidden", "sandbox_escape", "exfiltration", "policy_bypass"})
_L3_CREDENTIAL_TAGS = frozenset({"credential", "credentials", "secret", "secrets"})
_L3_EXTERNAL_WRITE_TAGS = frozenset({"external_write", "publish", "push", "deploy"})
_L3_DESTRUCTIVE_TAGS = frozenset({"destructive", "delete", "overwrite"})
_NETWORK_TAGS = frozenset({"network", "network_read", "download", "dependency_install"})


def _extract_paths(arguments: Mapping[str, Any], path_fields: frozenset[str]) -> list[str]:
    paths: list[str] = []
    for field_name in path_fields:
        value = arguments.get(field_name)
        if isinstance(value, (str, os.PathLike)):
            paths.append(os.fspath(value))
        elif isinstance(value, (list, tuple, set)):
            paths.extend(os.fspath(item) for item in value if isinstance(item, (str, os.PathLike)))
    return paths


def _is_within(candidate: Path, root: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def _deduplicate_paths(paths: Iterable[Path]) -> list[Path]:
    result: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        resolved = path.expanduser().resolve(strict=False)
        key = os.path.normcase(str(resolved))
        if key not in seen:
            seen.add(key)
            result.append(resolved)
    return result
