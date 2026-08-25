from __future__ import annotations

import json
import os
import re
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

from .validator import (
    ConfigError,
    DEFAULT_CONTEXT_WINDOW,
    VALID_PERMISSION_MODES,
    VALID_PROTOCOLS,
    VALID_TEAMMATE_MODES,
    lookup_model_context_window,
    validate_config_layer_structure,
    validate_config_structure,
)


_ENV_KEY_MAP = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
}

_OFFICIAL_ENDPOINTS = {
    "anthropic": ("api.anthropic.com", {"", "/"}),
    "openai": ("api.openai.com", {"/v1", "/v1/"}),
}

_ENV_VAR_RE = re.compile(r"\$\{([^}]+)\}")


class ConfigTrustError(ConfigError):
    """Raised before an inherited executable integration can be initialized."""

    error_code = "config.integration_trust_required"
    recommendation = (
        "Review `eviforge config explain`; rerun with the global "
        "`--trust-config` flag only if the listed integrations are approved."
    )

    def __init__(self, summary: dict[str, object]) -> None:
        self.trust_summary = deepcopy(summary)
        super().__init__(
            "Inherited executable integrations are blocked by default. "
            f"Trust summary: {json.dumps(summary, ensure_ascii=True, sort_keys=True)}."
        )


def is_official_provider_endpoint(protocol: str, base_url: str) -> bool:
    """Return whether the URL is the credential boundary for a protocol key.

    Official environment variables are intentionally bound to an exact HTTPS
    host and API base path.  Look-alike hosts, embedded credentials, custom
    ports, query strings and compatibility protocols never inherit them.
    """

    expected = _OFFICIAL_ENDPOINTS.get(protocol)
    if expected is None:
        return False
    try:
        parsed = urlsplit(base_url)
        port = parsed.port
    except (TypeError, ValueError):
        return False
    expected_host, expected_paths = expected
    return bool(
        parsed.scheme.casefold() == "https"
        and parsed.hostname is not None
        and parsed.hostname.casefold().rstrip(".") == expected_host
        and port in (None, 443)
        and parsed.path in expected_paths
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
    )


def _redact_url(url: str | None) -> str | None:
    if not url:
        return url
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        port = f":{parsed.port}" if parsed.port is not None else ""
        return f"{parsed.scheme}://{host}{port}{parsed.path}"
    except (TypeError, ValueError):
        return "<invalid-url>"


@dataclass
class ProviderConfig:
    name: str
    protocol: str
    base_url: str = field(repr=False)
    model: str
    # ``api_key`` remains supported for old local configs, but it is excluded
    # from repr and safe diagnostics.  New configs should name an environment
    # variable via ``api_key_env`` instead of storing a secret in YAML.
    api_key: str = field(default="", repr=False)
    api_key_env: str | None = None
    auth: str = "required"
    thinking: bool = False
    # 0 表示"未设置" — get_context_window() 通过四层 fallback 解析真实窗口大小。
    # 正数表示配置文件里显式指定的覆盖值。
    context_window: int = 0
    max_output_tokens: int = 0
    # 运行时 cache，存放从 provider 的 /v1/models 端点自动拉取的 context window
    # （get_context_window 的第 2 层）。通过 set_fetched_context_window() 写入一次；
    # 0 表示"尚未拉取"。不会持久化。
    _fetched_context_window: int = field(default=0, repr=False)

    def credential_env_name(self) -> str | None:
        if self.auth == "none" or self.api_key:
            return None
        if self.api_key_env:
            return self.api_key_env
        if is_official_provider_endpoint(self.protocol, self.base_url):
            return _ENV_KEY_MAP.get(self.protocol)
        return None

    def credential_source(self) -> str:
        """Return a redacted source label suitable for logs and diagnostics."""

        if self.auth == "none":
            return "none"
        if self.api_key:
            return "inline (deprecated)"
        env_name = self.credential_env_name()
        if env_name:
            return f"environment:{env_name}"
        return "missing explicit source"

    def resolve_api_key(self) -> str:
        if self.auth == "none":
            return ""
        if self.api_key:
            return self.api_key
        env_var = self.credential_env_name()
        return os.environ.get(env_var, "") if env_var else ""

    def to_safe_dict(self) -> dict[str, object]:
        """Serialize provider settings without materializing credential values."""

        return {
            "name": self.name,
            "protocol": self.protocol,
            "base_url": _redact_url(self.base_url),
            "model": self.model,
            "auth": self.auth,
            "credential_source": self.credential_source(),
            "thinking": self.thinking,
            "context_window": self.context_window,
            "max_output_tokens": self.max_output_tokens,
        }

    def set_fetched_context_window(self, window: int) -> None:
        """记录从 provider 自动拉取到的 context window（第 2 层）。

        非正数会被忽略，这样一次失败的拉取就不会污染 cache。在解析
        context window 时，每个 provider 只会调用一次。
        """
        if window > 0:
            self._fetched_context_window = window

    def get_context_window(self) -> int:
        """通过四层 fallback 解析模型的 context window，按优先级从高到低：

          1. 配置文件提供的 context_window（> 0）——显式覆盖，永远优先。
          2. 从 provider 的 /v1/models 端点自动拉取并通过 set_fetched_context_window
             缓存的值（只有 anthropic 协议的 provider 才会设置它；拉取失败或缺失时
             保持为 0 并跳过）。
          3. 内置的「模型名 -> window」映射表（按子串匹配）。
          4. 保守的默认值（claude -> 200000，其他 -> 128000）。
        """
        if self.context_window > 0:
            return self.context_window
        if self._fetched_context_window > 0:
            return self._fetched_context_window
        window = lookup_model_context_window(self.model)
        if window > 0:
            return window
        if "claude" in self.model.lower():
            return DEFAULT_CONTEXT_WINDOW
        return 128_000

    def get_max_output_tokens(self) -> int:
        if self.max_output_tokens > 0:
            return self.max_output_tokens
        if self.thinking:
            return 64000
        return 8192


def resolve_env_vars(value: str) -> str:
    return _ENV_VAR_RE.sub(lambda m: os.environ.get(m.group(1), m.group(0)), value)


def build_child_env(declared_env: dict[str, str] | None) -> dict[str, str]:
    env: dict[str, str] = {}
    path = os.environ.get("PATH", "")
    if path:
        env["PATH"] = path
    for key, value in (declared_env or {}).items():
        env[key] = resolve_env_vars(value)
    return env


@dataclass
class MCPServerConfig:
    name: str
    command: str | None = None
    args: list[str] = field(default_factory=list, repr=False)
    url: str | None = field(default=None, repr=False)
    headers: dict[str, str] = field(default_factory=dict, repr=False)
    env: dict[str, str] = field(default_factory=dict, repr=False)
    connect_timeout: float = 10.0
    tool_timeout: float = 60.0


    @property
    def is_stdio(self) -> bool:
        return self.command is not None


@dataclass
class WorktreeConfig:
    # Virtual environments are intentionally excluded: a Windows checkout may
    # be opened from WSL (or vice versa), and symlinking that interpreter tree
    # into a worktree creates an invalid cross-platform environment.
    symlink_directories: list[str] = field(default_factory=lambda: ["node_modules", "vendor"])
    stale_cleanup_interval: int = 3600
    stale_cutoff_hours: int = 24


@dataclass
class AppConfig:
    providers: list[ProviderConfig]
    permission_mode: str = "default"
    mcp_servers: list[MCPServerConfig] = field(default_factory=list)
    raw_hooks: list[dict] = field(default_factory=list, repr=False)
    enable_fork: bool = False
    enable_verification_agent: bool = False
    worktree: WorktreeConfig = field(default_factory=WorktreeConfig)
    teammate_mode: str = ""
    enable_coordinator_mode: bool = False
    config_sources: tuple[Path, ...] = field(default_factory=tuple)
    config_provenance: dict[str, str] = field(default_factory=dict, repr=False)
    # ``explicit`` means one file was deliberately selected (``--config`` or
    # ``EVIFORGE_CONFIG``). ``layered`` means the conventional user/project
    # search path was used and therefore needs a trust decision before an
    # inherited executable integration may run.
    config_load_mode: str = field(default="constructed", repr=False)
    user_config_sources: tuple[Path, ...] = field(default_factory=tuple, repr=False)

    def env_reference_payload(self) -> dict[str, object]:
        """Return the private values that need ``${NAME}`` availability checks.

        Callers must never serialize this payload: it can contain literal MCP
        headers and Hook bodies.  ``validate_env_references`` only reports a
        structural path and variable name, never a value.
        """

        return {
            "mcp_servers": [
                {
                    "args": list(server.args),
                    "url": server.url,
                    "headers": dict(server.headers),
                    "env": dict(server.env),
                }
                for server in self.mcp_servers
            ],
            "hooks": deepcopy(self.raw_hooks),
        }

    def integration_trust_summary(
        self, *, trust_config: bool = False
    ) -> dict[str, object]:
        """Describe inherited executable integrations without exposing payloads.

        Only active command/http Hooks and MCP servers whose effective fields
        came from a conventional user-level config are listed.  Commands,
        arguments, URLs, headers and environment values are deliberately
        omitted from the summary.
        """

        user_sources = {str(path.resolve()) for path in self.user_config_sources}
        integrations: list[dict[str, str]] = []
        if self.config_load_mode == "layered" and user_sources:
            for hook in self.raw_hooks:
                action = hook.get("action")
                action_type = (
                    str(action.get("type", "")) if isinstance(action, dict) else ""
                )
                if action_type not in {"command", "http"}:
                    continue
                hook_id = str(hook.get("id", "<unnamed>"))
                source = self.config_provenance.get(f"hooks[id={hook_id}]", "")
                # Unnamed Hooks cannot be matched across layers. If a user
                # layer participated and such a Hook remains active, treat it
                # as inherited conservatively. A later ``hooks: []`` removes it
                # from ``raw_hooks`` and therefore also removes this warning.
                inherited_unnamed = (
                    hook_id == "<unnamed>" and bool(user_sources)
                )
                if source in user_sources or inherited_unnamed:
                    integrations.append(
                        {
                            "kind": "hook",
                            "id": hook_id,
                            "action_type": action_type,
                            "source_scope": "user",
                        }
                    )

            for server in self.mcp_servers:
                prefix = f"mcp_servers[name={server.name}]"
                executable_fields = {"command", "args", "url", "headers", "env"}
                sources = {
                    value
                    for key, value in self.config_provenance.items()
                    if key in {
                        f"{prefix}.{field_name}"
                        for field_name in executable_fields
                    }
                }
                # Backward-compatible fallback for an AppConfig/provenance map
                # produced before field-level MCP provenance was introduced.
                if not sources:
                    entry_source = self.config_provenance.get(prefix)
                    if entry_source:
                        sources.add(entry_source)
                if sources & user_sources:
                    integrations.append(
                        {
                            "kind": "mcp",
                            "name": server.name,
                            "transport": "stdio" if server.is_stdio else "http",
                            "source_scope": "user",
                        }
                    )

        requires_trust = bool(integrations) and not trust_config
        if trust_config and integrations:
            decision = "trusted_by_flag"
        elif integrations:
            decision = "blocked"
        elif self.config_load_mode == "explicit":
            decision = "explicit_single_file"
        else:
            decision = "no_inherited_executable_integrations"
        return {
            "schema_version": 1,
            "load_mode": self.config_load_mode,
            "decision": decision,
            "requires_trust": requires_trust,
            "integrations": integrations,
        }

    def to_safe_dict(self) -> dict[str, object]:
        """Return an explainable view that never contains credential values."""

        return {
            "providers": [provider.to_safe_dict() for provider in self.providers],
            "permission_mode": self.permission_mode,
            "mcp_servers": [
                {
                    "name": server.name,
                    "transport": "stdio" if server.is_stdio else "http",
                    "command": server.command,
                    "arg_count": len(server.args),
                    "url": _redact_url(server.url),
                    "header_names": sorted(server.headers),
                    "env_names": sorted(server.env),
                    "connect_timeout": server.connect_timeout,
                    "tool_timeout": server.tool_timeout,
                }
                for server in self.mcp_servers
            ],
            "hook_ids": [str(hook.get("id", "")) for hook in self.raw_hooks],
            "enable_fork": self.enable_fork,
            "enable_verification_agent": self.enable_verification_agent,
            "worktree": {
                "symlink_directories": list(self.worktree.symlink_directories),
                "stale_cleanup_interval": self.worktree.stale_cleanup_interval,
                "stale_cutoff_hours": self.worktree.stale_cutoff_hours,
            },
            "teammate_mode": self.teammate_mode,
            "enable_coordinator_mode": self.enable_coordinator_mode,
            "config_sources": [str(path) for path in self.config_sources],
        }

    def explain_safe(self) -> dict[str, object]:
        """Return JSON-serializable effective config and field provenance.

        Credential material is never resolved by this method. Provider entries
        expose only their credential *source*, MCP entries expose header/env
        names, and hooks expose IDs. The provenance map therefore remains safe
        to print in CLI diagnostics or attach to a support bundle.
        """

        return {
            "schema_version": "1",
            "sources": [str(path) for path in self.config_sources],
            "effective": self.to_safe_dict(),
            "provenance": dict(sorted(self.config_provenance.items())),
            "trust": self.integration_trust_summary(),
        }


class _UniqueKeySafeLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects ambiguous duplicate mapping keys."""


def _construct_unique_mapping(
    loader: _UniqueKeySafeLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[object, object]:
    loader.flatten_mapping(node)
    mapping: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as exc:
            raise ConfigError("Config mapping keys must be scalar values") from exc
        if duplicate:
            raise ConfigError(f"Duplicate config field '{key}'")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeySafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _read_raw_file(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ConfigError(f"Failed to read config {path}") from exc
    try:
        raw = yaml.load(text, Loader=_UniqueKeySafeLoader)
    except ConfigError:
        raise
    except yaml.YAMLError as exc:
        # PyYAML's default exception includes the source line, which may hold a
        # credential.  Report location only so sentinel secrets cannot leak.
        mark = getattr(exc, "problem_mark", None)
        location = (
            f" at line {mark.line + 1}, column {mark.column + 1}"
            if mark is not None
            else ""
        )
        raise ConfigError(f"Failed to parse config {path}{location}") from None
    validate_config_layer_structure(raw)
    return deepcopy(raw)


_SCALAR_CONFIG_FIELDS = {
    "permission_mode",
    "enable_fork",
    "enable_verification_agent",
    "teammate_mode",
    "enable_coordinator_mode",
}
_DEFAULT_PROVENANCE_FIELDS = {
    *_SCALAR_CONFIG_FIELDS,
    "mcp_servers",
    "hooks",
    "worktree.symlink_directories",
    "worktree.stale_cleanup_interval",
    "worktree.stale_cutoff_hours",
}


def _clear_provenance_prefix(provenance: dict[str, str], prefix: str) -> None:
    for key in tuple(provenance):
        if key.startswith(prefix):
            provenance.pop(key, None)


def _record_layer_provenance(
    provenance: dict[str, str], layer: dict[str, Any], source: Path
) -> None:
    """Update final-field provenance using the same presence semantics as merge."""

    source_text = str(source.resolve())
    for key in _SCALAR_CONFIG_FIELDS:
        if key in layer:
            provenance[key] = source_text

    if "providers" in layer:
        provenance["providers"] = source_text
        _clear_provenance_prefix(provenance, "providers[")
        for entry in layer["providers"]:
            provenance[f"providers[name={entry['name']}]"] = source_text

    if "worktree" in layer:
        for field_name in layer["worktree"]:
            provenance[f"worktree.{field_name}"] = source_text

    if "mcp_servers" in layer:
        provenance["mcp_servers"] = source_text
        entries = layer["mcp_servers"]
        if not entries:
            _clear_provenance_prefix(provenance, "mcp_servers[")
        for entry in entries:
            entry_key = f"mcp_servers[name={entry['name']}]"
            if entry.get("disabled", False):
                _clear_provenance_prefix(provenance, entry_key)
            else:
                provenance[entry_key] = source_text
                # MCP entries merge field-by-field. Keep the field provenance
                # so a project layer that changes only ``args`` cannot silently
                # bless an inherited user-level command, URL, header or env.
                if "url" in entry:
                    for stale in ("command", "args", "env"):
                        provenance.pop(f"{entry_key}.{stale}", None)
                elif "command" in entry:
                    for stale in ("url", "headers"):
                        provenance.pop(f"{entry_key}.{stale}", None)
                for field_name in entry:
                    if field_name not in {"name", "disabled"}:
                        provenance[f"{entry_key}.{field_name}"] = source_text

    if "hooks" in layer:
        provenance["hooks"] = source_text
        entries = layer["hooks"]
        if not entries:
            _clear_provenance_prefix(provenance, "hooks[")
        for entry in entries:
            hook_id = entry.get("id")
            if not hook_id:
                continue
            entry_key = f"hooks[id={hook_id}]"
            if entry.get("disabled", False):
                provenance.pop(entry_key, None)
            else:
                provenance[entry_key] = source_text


def _finalize_provenance(provenance: dict[str, str]) -> dict[str, str]:
    result = dict(provenance)
    for field_name in _DEFAULT_PROVENANCE_FIELDS:
        result.setdefault(field_name, "<default>")
    return result


def _build_app_config(
    validated: dict[str, Any], *, sources: tuple[Path, ...] = (),
    provenance: dict[str, str] | None = None,
    load_mode: str = "constructed",
    user_sources: tuple[Path, ...] = (),
) -> AppConfig:
    providers = [
        ProviderConfig(
            name=p["name"],
            protocol=p["protocol"],
            base_url=p["base_url"],
            model=p["model"],
            api_key=p["api_key"],
            api_key_env=p["api_key_env"],
            auth=p["auth"],
            thinking=p["thinking"],
            context_window=p["context_window"],
            max_output_tokens=p["max_output_tokens"],
        )
        for p in validated["providers"]
    ]

    mcp_servers = [
        MCPServerConfig(
            name=s["name"],
            command=s["command"],
            args=s["args"],
            url=s["url"],
            headers=s["headers"],
            env=s["env"],
            connect_timeout=s["connect_timeout"],
            tool_timeout=s["tool_timeout"],
        )
        for s in validated["mcp_servers"]
    ]

    wt = validated["worktree"]
    worktree_cfg = WorktreeConfig(
        symlink_directories=wt["symlink_directories"],
        stale_cleanup_interval=wt["stale_cleanup_interval"],
        stale_cutoff_hours=wt["stale_cutoff_hours"],
    )

    return AppConfig(
        providers=providers,
        permission_mode=validated["permission_mode"],
        mcp_servers=mcp_servers,
        raw_hooks=validated["hooks"],
        enable_fork=validated["enable_fork"],
        enable_verification_agent=validated["enable_verification_agent"],
        worktree=worktree_cfg,
        teammate_mode=validated["teammate_mode"],
        enable_coordinator_mode=validated["enable_coordinator_mode"],
        config_sources=sources,
        config_provenance=_finalize_provenance(provenance or {}),
        config_load_mode=load_mode,
        user_config_sources=user_sources,
    )


def _load_single_file(path: Path) -> AppConfig:
    raw = _read_raw_file(path)
    validated = validate_config_structure(raw)
    provenance: dict[str, str] = {}
    _record_layer_provenance(provenance, raw, path)
    return _build_app_config(
        validated,
        sources=(path.resolve(),),
        provenance=provenance,
        load_mode="explicit",
    )


def _merge_named_mcp(base: list[dict], override: list[dict]) -> list[dict]:
    if not override:
        return []
    result = deepcopy(base)
    by_name = {str(entry.get("name")): i for i, entry in enumerate(result)}
    for raw_entry in override:
        entry = deepcopy(raw_entry)
        name = str(entry["name"])
        index = by_name.get(name)
        if entry.pop("disabled", False):
            if index is not None:
                result.pop(index)
                by_name = {str(item.get("name")): i for i, item in enumerate(result)}
            continue
        if index is None:
            result.append(entry)
            by_name[name] = len(result) - 1
            continue
        merged = {**result[index], **entry}
        # Switching transport must not retain fields from the old transport.
        if "url" in entry:
            for stale in ("command", "args", "env"):
                merged.pop(stale, None)
        elif "command" in entry:
            for stale in ("url", "headers"):
                merged.pop(stale, None)
        result[index] = merged
    return result


def _merge_hooks(base: list[dict], override: list[dict]) -> list[dict]:
    if not override:
        return []
    result = deepcopy(base)
    by_id = {
        str(entry["id"]): index
        for index, entry in enumerate(result)
        if entry.get("id")
    }
    for raw_entry in override:
        entry = deepcopy(raw_entry)
        hook_id = str(entry.get("id", ""))
        index = by_id.get(hook_id) if hook_id else None
        if entry.pop("disabled", False):
            if index is not None:
                result.pop(index)
                by_id = {
                    str(item["id"]): i
                    for i, item in enumerate(result)
                    if item.get("id")
                }
            continue
        if index is None:
            result.append(entry)
            if hook_id:
                by_id[hook_id] = len(result) - 1
        else:
            # Hook definitions are atomic: replacing the whole definition
            # avoids accidentally inheriting a privileged action field.
            result[index] = entry
    return result


def _merge_raw_config(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Merge a validated raw layer while preserving explicit falsey values."""

    merged = deepcopy(base)
    for key, value in override.items():
        if key == "worktree":
            previous = merged.get("worktree", {})
            merged[key] = {**deepcopy(previous), **deepcopy(value)}
        elif key == "mcp_servers":
            merged[key] = _merge_named_mcp(merged.get(key, []), value)
        elif key == "hooks":
            merged[key] = _merge_hooks(merged.get(key, []), value)
        else:
            # Scalars and provider lists use replace semantics.  In particular,
            # false/default/empty strings are real values, not inheritance.
            merged[key] = deepcopy(value)
    return merged


def load_config(path: Path | None = None) -> AppConfig:
    if path is None:
        explicit_env_path = os.environ.get("EVIFORGE_CONFIG", "").strip()
        if explicit_env_path:
            path = Path(explicit_env_path).expanduser()
    if path is not None:
        if not path.exists():
            raise ConfigError(f"Config file not found: {path}")
        return _load_single_file(path)

    cwd = Path.cwd()
    home = Path.home()
    user_candidates = [
        home / ".mewcode" / "config.yaml",
        home / ".eviforge" / "config.yaml",
    ]
    candidates = [
        *user_candidates,
        cwd / ".mewcode" / "config.yaml",
        cwd / ".mewcode" / "config.local.yaml",
    ]

    merged_raw: dict[str, Any] | None = None
    sources: list[Path] = []
    provenance: dict[str, str] = {}
    for p in candidates:
        if not p.exists():
            continue
        layer = _read_raw_file(p)
        sources.append(p.resolve())
        _record_layer_provenance(provenance, layer, p)
        if merged_raw is None:
            merged_raw = layer
        else:
            merged_raw = _merge_raw_config(merged_raw, layer)

    if merged_raw is None:
        raise ConfigError(
            "No config file found. Expected .mewcode/config.yaml "
            "in project, ~/.mewcode/config.yaml, or ~/.eviforge/config.yaml"
        )
    validated = validate_config_structure(merged_raw)
    return _build_app_config(
        validated,
        sources=tuple(sources),
        provenance=provenance,
        load_mode="layered",
        user_sources=tuple(
            candidate.resolve() for candidate in user_candidates if candidate.exists()
        ),
    )
