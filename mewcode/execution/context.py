"""Host-owned execution scope and invocation-scoped authorization grants."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse

from pydantic import BaseModel

from mewcode.execution.descriptor import ToolDescriptor
from mewcode.execution.risk import RiskDecision


def normalized_arguments_hash(arguments: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        dict(arguments),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class ExecutionAssessment:
    """Strict-schema preview suitable for rendering before an approval UI."""

    invocation_id: str
    tool_name: str
    descriptor: ToolDescriptor | None
    risk: RiskDecision | None
    normalized_arguments: Mapping[str, Any] = field(default_factory=dict)
    arguments_hash: str = ""
    valid: bool = True
    error: str = ""
    reason_codes: tuple[str, ...] = ()
    _params: BaseModel | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    """Plan-owned constraints; this object is never deserialized from tool args.

    ``None`` for one of the constraint collections means that no approved
    manifest was supplied (legacy compatibility).  An empty tuple means an
    approved manifest explicitly permits no operations of that kind.
    """

    task_id: str
    cwd: str
    plan_hash: str
    expected_pre_state_hash: str = "not-attested"
    workspace_root: str | None = None
    write_set: tuple[str, ...] | None = None
    commands: tuple[tuple[str, ...], ...] | None = None
    network_hosts: tuple[str, ...] | None = None

    @classmethod
    def unplanned(cls, *, task_id: str, cwd: str | os.PathLike[str]) -> "ExecutionContext":
        real = str(Path(cwd).expanduser().resolve(strict=False))
        return cls(task_id=task_id, cwd=real, plan_hash="unplanned", workspace_root=real)

    @classmethod
    def from_manifest(
        cls,
        manifest: object,
        *,
        task_id: str,
        cwd: str | os.PathLike[str],
        expected_pre_state_hash: str = "not-attested",
        workspace_root: str | os.PathLike[str] | None = None,
    ) -> "ExecutionContext":
        """Adapt ``plan_contract.ExecutionManifest`` without coupling packages."""

        real_cwd = str(Path(cwd).expanduser().resolve(strict=False))
        root = Path(workspace_root or cwd).expanduser().resolve(strict=False)
        return cls(
            task_id=task_id,
            cwd=real_cwd,
            plan_hash=str(getattr(manifest, "plan_hash")),
            expected_pre_state_hash=expected_pre_state_hash,
            workspace_root=str(root),
            write_set=tuple(str(item) for item in getattr(manifest, "write_set")),
            commands=tuple(tuple(str(part) for part in argv) for argv in getattr(manifest, "commands")),
            network_hosts=tuple(str(host).casefold() for host in getattr(manifest, "network_hosts")),
        )

    def constraint_error(
        self, descriptor: ToolDescriptor, arguments: Mapping[str, Any]
    ) -> tuple[str, str] | None:
        root = Path(self.workspace_root or self.cwd).expanduser().resolve(strict=False)
        if descriptor.transport_kind == "unknown" and (
            self.commands is not None or self.network_hosts is not None
        ):
            return (
                "manifest.transport_unbound",
                "tool transport has no host-bound command or destination metadata",
            )
        if descriptor.category == "write" and self.write_set is not None:
            paths = _argument_paths(descriptor, arguments)
            if not paths:
                return "manifest.write_set_missing_target", "write tool exposes no bound target path"
            allowed = {_relative_key(root, value) for value in self.write_set}
            for value in paths:
                candidate = _relative_key(root, value)
                if candidate not in allowed:
                    return "manifest.write_set_violation", f"write target is outside approved manifest: {candidate}"

        # Only shell/process launchers expose ``command``/``argv``.  Internal
        # orchestration tools historically use category="command" too, but
        # their structured operations are not executable command manifests.
        if descriptor.transport_kind == "stdio" and self.commands is not None:
            argv = descriptor.transport_command or None
            if argv is None or argv not in self.commands:
                return "manifest.command_violation", "MCP stdio transport argv is not approved"
        elif (
            descriptor.category == "command"
            and self.commands is not None
            and ("command" in arguments or "argv" in arguments)
        ):
            if descriptor.supports_exact_argv and arguments.get("argv") is None:
                return (
                    "manifest.exact_argv_required",
                    "reviewed Plan commands must use the structured argv field; shell command strings are forbidden",
                )
            argv = _command_argv(arguments)
            if descriptor.supports_exact_argv:
                if argv is None or argv not in self.commands:
                    return "manifest.command_violation", "command argv is not in the approved manifest"
            else:
                # Legacy/custom command tools have no shell-free execution
                # contract. Keep exact string compatibility for them without
                # pretending to parse a shell grammar; Bash opts into the
                # stronger supports_exact_argv path above.
                command = arguments.get("command")
                if not isinstance(command, str) or not any(
                    len(approved) == 1 and approved[0] == command
                    or " ".join(approved) == command
                    for approved in self.commands
                ):
                    return "manifest.command_violation", "command is not in the approved manifest"

        argument_hosts = _network_hosts(arguments)
        is_network = descriptor.side_effect in {"network", "external_write"} or bool(
            descriptor.risk_tags & {"network", "network_read", "download", "publish", "deploy"}
        )
        if (is_network or argument_hosts) and self.network_hosts is not None:
            hosts = {
                host.casefold().rstrip(".") for host in descriptor.destination_hosts
            }
            hosts.update(argument_hosts)
            allowed_hosts = {host.casefold().rstrip(".") for host in self.network_hosts}
            if not hosts:
                return "manifest.network_host_missing", "network tool exposes no bound destination host"
            for host in hosts:
                if host not in allowed_hosts:
                    return "manifest.network_violation", f"network host is not approved: {host}"
        return None

    def approved_command_argv(
        self, descriptor: ToolDescriptor, arguments: Mapping[str, Any]
    ) -> tuple[str, ...] | None:
        """Return the exact host-approved argv for a planned command tool.

        The returned tuple comes from the immutable manifest, not from a shell
        re-serialization. ``constraint_error`` must have returned ``None``
        before callers use it.
        """

        if self.commands is None or not descriptor.supports_exact_argv:
            return None
        candidate = _command_argv(arguments)
        if candidate is None:
            return None
        for approved in self.commands:
            if candidate == approved:
                return approved
        return None


@dataclass(frozen=True, slots=True)
class InvocationGrant:
    """Opaque, one-invocation capability issued by an ``ExecutionGateway``."""

    invocation_id: str
    tool_name: str
    arguments_hash: str
    cwd_realpath: str
    plan_hash: str
    approver: str
    issued_at: float
    _signature: str = field(repr=False)

    @staticmethod
    def _payload(
        invocation_id: str,
        tool_name: str,
        arguments_hash: str,
        cwd_realpath: str,
        plan_hash: str,
        approver: str,
        issued_at: float,
    ) -> bytes:
        return "\x1f".join(
            (invocation_id, tool_name, arguments_hash, cwd_realpath, plan_hash, approver, repr(issued_at))
        ).encode("utf-8")

    @classmethod
    def issue(
        cls,
        secret: bytes,
        *,
        invocation_id: str,
        tool_name: str,
        arguments_hash: str,
        context: ExecutionContext,
        approver: str,
    ) -> "InvocationGrant":
        issued_at = time.time()
        cwd = str(Path(context.cwd).expanduser().resolve(strict=False))
        payload = cls._payload(
            invocation_id, tool_name, arguments_hash, cwd, context.plan_hash, approver, issued_at
        )
        signature = hmac.new(secret, payload, hashlib.sha256).hexdigest()
        return cls(invocation_id, tool_name, arguments_hash, cwd, context.plan_hash, approver, issued_at, signature)

    def verify(
        self,
        secret: bytes,
        *,
        invocation_id: str,
        tool_name: str,
        arguments_hash: str,
        context: ExecutionContext,
    ) -> bool:
        cwd = str(Path(context.cwd).expanduser().resolve(strict=False))
        if (
            self.invocation_id != invocation_id
            or self.tool_name != tool_name
            or self.arguments_hash != arguments_hash
            or self.cwd_realpath != cwd
            or self.plan_hash != context.plan_hash
        ):
            return False
        payload = self._payload(
            self.invocation_id,
            self.tool_name,
            self.arguments_hash,
            self.cwd_realpath,
            self.plan_hash,
            self.approver,
            self.issued_at,
        )
        expected = hmac.new(secret, payload, hashlib.sha256).hexdigest()
        return hmac.compare_digest(self._signature, expected)


def _argument_paths(descriptor: ToolDescriptor, arguments: Mapping[str, Any]) -> list[str]:
    result: list[str] = []
    for name in descriptor.path_fields:
        value = arguments.get(name)
        if isinstance(value, (str, os.PathLike)):
            result.append(os.fspath(value))
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            result.extend(os.fspath(item) for item in value if isinstance(item, (str, os.PathLike)))
    return result


def _relative_key(root: Path, raw: str) -> str:
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve(strict=False)
    try:
        return os.path.normcase(resolved.relative_to(root).as_posix()).replace("\\", "/")
    except ValueError:
        return f"<outside>/{os.path.normcase(str(resolved))}"


def _command_argv(arguments: Mapping[str, Any]) -> tuple[str, ...] | None:
    argv = arguments.get("argv")
    if isinstance(argv, Sequence) and not isinstance(argv, (str, bytes)) and all(
        isinstance(item, str) for item in argv
    ):
        return tuple(argv)
    # A shell string is intentionally never converted back into argv.  Shell
    # grammars differ by platform and metacharacters can change semantics after
    # validation. Planned Bash calls must use their typed ``argv`` field.
    return None


def _network_hosts(arguments: Mapping[str, Any]) -> set[str]:
    hosts: set[str] = set()

    def visit(value: Any, field_name: str = "") -> None:
        if isinstance(value, Mapping):
            for nested_name, nested_value in value.items():
                visit(nested_value, str(nested_name).casefold())
            return
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            for nested_value in value:
                visit(nested_value, field_name)
            return
        if not isinstance(value, str):
            return
        singular = field_name.removesuffix("s")
        if singular in {"host", "hostname"} or singular.endswith(("_host", "_hostname")):
            hosts.add(value.casefold().rstrip("."))
        elif singular in {"url", "uri", "endpoint"} or singular.endswith(
            ("_url", "_uri", "_endpoint")
        ):
            parsed = urlparse(value if "://" in value else "//" + value)
            if parsed.hostname:
                hosts.add(parsed.hostname.casefold().rstrip("."))

    visit(arguments)
    return hosts
