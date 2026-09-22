"""Canonical capability descriptions for the built-in execution boundaries.

An argv grant approves a process invocation, not its internal file/network effects.
File and HTTP scopes are enforced by their respective built-in tool adapters.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def content_digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def canonical_path(value: str, cwd: str) -> str:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path(cwd) / path
    return str(path.resolve(strict=False))


def within(path: str, roots: tuple[str, ...] | list[str]) -> bool:
    for root in roots:
        try:
            Path(path).relative_to(Path(root))
            return True
        except ValueError:
            continue
    return False


@dataclass(frozen=True)
class GateDecision:
    allowed: bool
    code: str
    reason: str

    @property
    def effect(self) -> str:
        return "allow" if self.allowed else "deny"


@dataclass(frozen=True)
class ExecutionIntent:
    tool_name: str
    arguments_json: str
    cwd: str
    argv: tuple[str, ...] = ()
    read_paths: tuple[str, ...] = ()
    write_paths: tuple[str, ...] = ()
    network_hosts: tuple[str, ...] = ()
    opaque_process: bool = False

    @property
    def intent_hash(self) -> str:
        return content_digest(asdict(self))

    @property
    def arguments_hash(self) -> str:
        return hashlib.sha256(self.arguments_json.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PlanAction:
    tool_name: str
    arguments_json: str
    cwd: str
    read_paths: tuple[str, ...] = ()
    write_paths: tuple[str, ...] = ()
    network_hosts: tuple[str, ...] = ()
    uses: int = 1
    opaque_process: bool = False

    def as_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["arguments"] = json.loads(result.pop("arguments_json"))
        return result

    def matches(self, intent: ExecutionIntent) -> bool:
        return (
            self.tool_name == intent.tool_name
            and self.arguments_json == intent.arguments_json
            and self.cwd == intent.cwd
            and all(within(path, self.read_paths) for path in intent.read_paths)
            and all(within(path, self.write_paths) for path in intent.write_paths)
            and set(intent.network_hosts).issubset(self.network_hosts)
        )


@dataclass
class ApprovalGrant:
    grant_id: str
    plan_id: str
    content_hash: str
    session_id: str
    source_turn_id: str
    execution_turn_id: str
    agent_id: str
    action: PlanAction
    expires_at: float
    remaining_uses: int


def builtin_type(name: str) -> type:
    # Resolve locally, rather than trusting remote Tool.category or descriptions.
    from eviforge.tools.bash import Bash
    from eviforge.tools.edit_file import EditFile
    from eviforge.tools.glob import Glob
    from eviforge.tools.grep import Grep
    from eviforge.tools.http_request import HttpRequest
    from eviforge.tools.read_file import ReadFile
    from eviforge.tools.write_file import WriteFile
    classes = {item.name: item for item in (Bash, EditFile, Glob, Grep, HttpRequest, ReadFile, WriteFile)}
    try:
        return classes[name]
    except KeyError as exc:
        raise ValueError(f"UNSUPPORTED_TOOL: no trusted capability adapter for {name}") from exc


def execution_intent(tool: Any, arguments: dict[str, Any], cwd: str) -> ExecutionIntent:
    from eviforge.dag.capabilities import ScopedTool
    if type(tool) is ScopedTool:
        # This exact internal wrapper performs its own narrower DAG check again
        # during execute. Never unwrap an arbitrary object's ``original``.
        tool = tool.original
    expected = builtin_type(tool.name)
    if type(tool) is not expected:
        raise ValueError(f"UNSUPPORTED_TOOL: tool implementation for {tool.name} is not trusted")
    args = tool.params_model.model_validate(arguments).model_dump(mode="json", exclude_none=True)
    directory = canonical_path(cwd, str(Path.cwd()))
    read_paths: tuple[str, ...] = ()
    write_paths: tuple[str, ...] = ()
    argv: tuple[str, ...] = ()
    hosts: tuple[str, ...] = ()
    if tool.name in {"ReadFile", "WriteFile", "EditFile"}:
        target = canonical_path(args["file_path"], directory)
        if tool.name == "ReadFile":
            read_paths = (target,)
        else:
            write_paths = (target,)
    elif tool.name in {"Glob", "Grep"}:
        read_paths = (canonical_path(args.get("path", "."), directory),)
    elif tool.name == "Bash":
        if "argv" not in args:
            raise ValueError("EXACT_ARGV_REQUIRED: plan commands must use argv, without shell parsing")
        argv = tuple(args["argv"])
    elif tool.name == "HttpRequest":
        from eviforge.tools.http_request import network_origin
        hosts = tuple(args["allowed_hosts"])
        if network_origin(args["url"]) not in hosts:
            raise ValueError("NETWORK_SCOPE_DENIED: request URL is outside allowed_hosts")
    return ExecutionIntent(tool.name, canonical_json(args), directory, argv, read_paths, write_paths, hosts, tool.name == "Bash")


def normalize_action(raw: dict[str, Any], default_cwd: str) -> PlanAction:
    """Freeze validated arguments and explicit scope ceilings into a plan action."""
    from eviforge.tools.http_request import normalize_host_scope
    if set(raw) - {"tool_name", "arguments", "cwd", "read_paths", "write_paths", "network_hosts", "uses", "opaque_process"}:
        raise ValueError("Unknown plan action fields")
    tool_name = raw["tool_name"]
    cls = builtin_type(tool_name)
    # Only its class and parameter model are needed; constructor dependencies
    # (file caches/HTTP transport) must never execute during plan validation.
    tool = object.__new__(cls)
    intent = execution_intent(tool, raw.get("arguments", {}), raw.get("cwd", default_cwd))
    reads = tuple(sorted({canonical_path(path, intent.cwd) for path in raw.get("read_paths", intent.read_paths)}))
    writes = tuple(sorted({canonical_path(path, intent.cwd) for path in raw.get("write_paths", intent.write_paths)}))
    hosts = tuple(sorted({normalize_host_scope(value) for value in raw.get("network_hosts", intent.network_hosts)}))
    uses = raw.get("uses", 1)
    if isinstance(uses, bool) or not isinstance(uses, int) or not 1 <= uses <= 100:
        raise ValueError("uses must be an integer between 1 and 100")
    action = PlanAction(tool_name, intent.arguments_json, intent.cwd, reads, writes, hosts, uses, intent.opaque_process)
    if not action.matches(intent):
        raise ValueError("ACTION_OUTSIDE_SCOPE: concrete arguments exceed their declared resource scopes")
    return action
