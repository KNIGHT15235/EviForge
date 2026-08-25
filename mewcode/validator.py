"""MewCode 的配置校验逻辑。"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from urllib.parse import urlsplit

VALID_PROTOCOLS = {"anthropic", "openai", "openai-compat"}

VALID_AUTH_MODES = {"required", "none"}

VALID_PERMISSION_MODES = {
    "default",
    "acceptEdits",
    "plan",
    "bypassPermissions",
    "custom",
    "dontAsk",
}

VALID_TEAMMATE_MODES = {"", "in-process"}

DEFAULT_CONTEXT_WINDOW = 200_000

_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ENV_REFERENCE_RE = re.compile(r"\$\{([^}]*)\}")

_TOP_LEVEL_FIELDS = {
    "providers",
    "permission_mode",
    "mcp_servers",
    "hooks",
    "enable_fork",
    "enable_verification_agent",
    "worktree",
    "teammate_mode",
    "enable_coordinator_mode",
}
_PROVIDER_FIELDS = {
    "name",
    "protocol",
    "base_url",
    "model",
    "api_key",
    "api_key_env",
    "auth",
    "thinking",
    "context_window",
    "max_output_tokens",
}
_MCP_FIELDS = {
    "name",
    "command",
    "args",
    "url",
    "headers",
    "env",
    "connect_timeout",
    "tool_timeout",
}
_MCP_LAYER_FIELDS = _MCP_FIELDS | {"disabled"}
_WORKTREE_FIELDS = {
    "symlink_directories",
    "stale_cleanup_interval",
    "stale_cutoff_hours",
}
_HOOK_FIELDS = {"id", "event", "action", "if", "reject", "once", "async"}
_HOOK_LAYER_FIELDS = _HOOK_FIELDS | {"disabled"}
_HOOK_ACTION_FIELDS = {
    "type",
    "command",
    "message",
    "url",
    "method",
    "body",
    "headers",
    "prompt",
    "timeout",
}

# 内置的"模型名子串 -> context window（最大输入 token 数）"映射表，
# 是 context window 回退链的第 3 层（见 ProviderConfig.get_context_window）。
# 按从最具体到最通用排序，第一个子串命中即生效。值仅为合理起始点，
# 模型更新/重命名后可能过时。如果值不准确，在配置中设置 context_window 覆盖（最高优先级）。
MODEL_CONTEXT_WINDOWS: list[tuple[str, int]] = [
    ("1m", 1_000_000),       # 也覆盖 "-1m" 后缀（如 claude-...-1m）
    ("gpt-4.1", 1_000_000),  # GPT-4.1 系列的 window 为 1M
    ("gpt-4o", 128_000),
    ("gpt-4-turbo", 128_000),
    ("o1", 200_000),         # OpenAI 推理模型 o1 / o3 / o4
    ("o3", 200_000),
    ("o4", 200_000),
    ("gpt-3.5", 16_385),
    ("claude", 200_000),
]


def lookup_model_context_window(model: str) -> int:
    """通过子串匹配（第 3 层），返回内置映射表中该模型对应的
    context window；没有匹配则返回 0。"""
    m = model.lower()
    for substr, window in MODEL_CONTEXT_WINDOWS:
        if substr in m:
            return window
    return 0


class ConfigError(Exception):
    pass


def _reject_unknown_fields(
    value: Mapping[object, object], allowed: set[str], label: str
) -> None:
    unknown = sorted(str(key) for key in value if key not in allowed)
    if unknown:
        raise ConfigError(
            f"{label}: unknown field(s): {', '.join(unknown)}"
        )


def _require_non_empty_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{label} must be a non-empty string")
    return value


def _validate_url(value: object, label: str) -> str:
    url = _require_non_empty_string(value, label)
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        _ = parsed.port
    except ValueError:
        raise ConfigError(f"{label} must be a valid HTTP(S) URL") from None
    if parsed.scheme.casefold() not in {"http", "https"} or not hostname:
        raise ConfigError(f"{label} must be an absolute HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise ConfigError(f"{label} must not contain embedded credentials")
    if parsed.fragment:
        raise ConfigError(f"{label} must not contain a URL fragment")
    return url


def _validate_string_mapping(value: object, label: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ConfigError(f"{label} must be a mapping of strings")
    if not all(isinstance(k, str) and isinstance(v, str) for k, v in value.items()):
        raise ConfigError(f"{label} must contain only string keys and values")
    return dict(value)


def _validate_positive_number(value: object, label: str) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or value <= 0
    ):
        raise ConfigError(f"{label} must be a positive number")
    return float(value)


def _validate_env_reference_syntax(raw: object, path: str = "config") -> None:
    """Reject malformed ``${NAME}`` references without resolving secrets."""

    if isinstance(raw, dict):
        for key, value in raw.items():
            _validate_env_reference_syntax(value, f"{path}.{key}")
        return
    if isinstance(raw, list):
        for index, value in enumerate(raw):
            _validate_env_reference_syntax(value, f"{path}[{index}]")
        return
    if not isinstance(raw, str):
        return
    for match in _ENV_REFERENCE_RE.finditer(raw):
        name = match.group(1)
        if not _ENV_NAME_RE.fullmatch(name):
            raise ConfigError(
                f"{path}: invalid environment variable reference; "
                "use ${UPPER_CASE_NAME}"
            )


def validate_env_references(raw: object, path: str = "config") -> None:
    """Fail early for unresolved environment references, without echoing values.

    Values remain as placeholders in the returned runtime configuration so MCP
    transports can resolve them immediately before use.  This function only
    checks that every referenced variable exists and never includes its value
    in an exception.
    """

    _validate_env_reference_syntax(raw, path)
    if isinstance(raw, dict):
        for key, value in raw.items():
            validate_env_references(value, f"{path}.{key}")
        return
    if isinstance(raw, list):
        for index, value in enumerate(raw):
            validate_env_references(value, f"{path}[{index}]")
        return
    if not isinstance(raw, str):
        return
    for match in _ENV_REFERENCE_RE.finditer(raw):
        name = match.group(1)
        if name not in os.environ:
            raise ConfigError(
                f"{path}: environment variable '{name}' is not set"
            )


def validate_providers(
    raw_providers: list, *, strict_urls: bool = False
) -> list[dict]:
    """校验 providers 列表，返回清洗后的 provider 字典列表。"""
    if not isinstance(raw_providers, list) or len(raw_providers) == 0:
        raise ConfigError("At least one provider must be configured")

    providers: list[dict] = []
    names: set[str] = set()
    for i, entry in enumerate(raw_providers):
        if not isinstance(entry, dict):
            raise ConfigError(f"Provider #{i + 1}: must be a mapping")

        label = f"Provider #{i + 1}"
        _reject_unknown_fields(entry, _PROVIDER_FIELDS, label)

        missing = [f for f in ("name", "protocol", "base_url", "model") if f not in entry]
        if missing:
            raise ConfigError(f"{label}: missing fields: {', '.join(missing)}")

        name = _require_non_empty_string(entry["name"], f"{label}.name")
        if name in names:
            raise ConfigError(f"Duplicate provider name '{name}'")
        names.add(name)

        protocol = entry["protocol"]
        if not isinstance(protocol, str) or protocol not in VALID_PROTOCOLS:
            raise ConfigError(
                f"{label}: invalid protocol '{protocol}', "
                f"must be one of: {', '.join(sorted(VALID_PROTOCOLS))}"
            )

        if strict_urls:
            base_url = _validate_url(entry["base_url"], f"{label}.base_url")
        else:
            # Keep the standalone helper backward-compatible for callers that
            # use a dummy URL in unit fixtures. Full config validation is strict.
            base_url = _require_non_empty_string(
                entry["base_url"], f"{label}.base_url"
            )
        model = _require_non_empty_string(entry["model"], f"{label}.model")

        auth = entry.get("auth", "required")
        if not isinstance(auth, str) or auth not in VALID_AUTH_MODES:
            raise ConfigError(
                f"{label}: invalid auth mode '{auth}', must be one of: "
                f"{', '.join(sorted(VALID_AUTH_MODES))}"
            )

        api_key = entry.get("api_key", "")
        if not isinstance(api_key, str):
            raise ConfigError(f"{label}.api_key must be a string")
        if _ENV_REFERENCE_RE.search(api_key):
            raise ConfigError(
                f"{label}.api_key must not use ${{...}}; use api_key_env instead"
            )

        api_key_env = entry.get("api_key_env")
        if api_key_env is not None:
            if not isinstance(api_key_env, str) or not _ENV_NAME_RE.fullmatch(api_key_env):
                raise ConfigError(
                    f"{label}.api_key_env must be an environment variable name"
                )
        if api_key and api_key_env:
            raise ConfigError(
                f"{label}: configure only one of api_key or api_key_env"
            )
        if auth == "none" and (api_key or api_key_env):
            raise ConfigError(
                f"{label}: auth 'none' cannot be combined with api_key or api_key_env"
            )

        # 默认为 0（"未设置"）而非硬编码的 window 值：0 会让
        # ProviderConfig.get_context_window() 走四层回退链解析
        #（自动拉取 / 映射表 / 默认值）。配置中显式指定的值仍须为正整数，
        # 且作为最高优先级覆盖。
        context_window = entry.get("context_window", 0)
        if not isinstance(context_window, int) or isinstance(context_window, bool) or context_window < 0:
            raise ConfigError(
                f"{label}: context_window must be a non-negative integer"
            )

        thinking = entry.get("thinking", False)
        if not isinstance(thinking, bool):
            raise ConfigError(f"{label}: thinking must be a boolean")
        if thinking and protocol != "anthropic":
            raise ConfigError(
                f"{label}: thinking=true is only supported by the anthropic protocol"
            )

        max_output_tokens = entry.get("max_output_tokens", 0)
        if (
            not isinstance(max_output_tokens, int)
            or isinstance(max_output_tokens, bool)
            or max_output_tokens < 0
        ):
            raise ConfigError(
                f"{label}: max_output_tokens must be a non-negative integer"
            )

        providers.append(
            {
                "name": name,
                "protocol": protocol,
                "base_url": base_url,
                "model": model,
                "api_key": api_key,
                "api_key_env": api_key_env,
                "auth": auth,
                "thinking": thinking,
                "context_window": context_window,
                "max_output_tokens": max_output_tokens,
            }
        )

    return providers


def validate_permission_mode(mode: str) -> str:
    """校验 permission_mode 取值。"""
    if not isinstance(mode, str) or mode not in VALID_PERMISSION_MODES:
        raise ConfigError(
            f"Invalid permission_mode '{mode}', "
            f"must be one of: {', '.join(sorted(VALID_PERMISSION_MODES))}"
        )
    return mode


def validate_mcp_servers(raw_mcp: list | None) -> list[dict]:
    """校验 mcp_servers 配置段，返回清洗后的 server 配置字典列表。"""
    if raw_mcp is None:
        return []

    if not isinstance(raw_mcp, list):
        raise ConfigError("'mcp_servers' must be a list of server configs")

    servers: list[dict] = []
    names: set[str] = set()
    for i, entry in enumerate(raw_mcp):
        if not isinstance(entry, dict):
            raise ConfigError(f"MCP server #{i + 1}: must be a mapping")
        label = f"MCP server #{i + 1}"
        _reject_unknown_fields(entry, _MCP_FIELDS, label)
        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ConfigError(f"{label}: missing 'name'")
        if name in names:
            raise ConfigError(f"Duplicate MCP server name '{name}'")
        names.add(name)
        has_command = "command" in entry
        has_url = "url" in entry
        if has_command and has_url:
            raise ConfigError(
                f"MCP server '{name}': cannot have both 'command' and 'url'"
            )
        if not has_command and not has_url:
            raise ConfigError(
                f"MCP server '{name}': must have either 'command' or 'url'"
            )
        command = entry.get("command")
        if has_command:
            command = _require_non_empty_string(
                command, f"MCP server '{name}'.command"
            )
        url = entry.get("url")
        if has_url:
            url = _validate_url(url, f"MCP server '{name}'.url")
        args = entry.get("args", [])
        if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
            raise ConfigError(f"MCP server '{name}'.args must be a list of strings")
        headers = _validate_string_mapping(
            entry.get("headers", {}), f"MCP server '{name}'.headers"
        )
        env = _validate_string_mapping(
            entry.get("env", {}), f"MCP server '{name}'.env"
        )
        connect_timeout = _validate_positive_number(
            entry.get("connect_timeout", 10.0),
            f"MCP server '{name}'.connect_timeout",
        )
        tool_timeout = _validate_positive_number(
            entry.get("tool_timeout", 60.0),
            f"MCP server '{name}'.tool_timeout",
        )
        servers.append(
            {
                "name": name,
                "command": command,
                "args": args,
                "url": url,
                "headers": headers,
                "env": env,
                "connect_timeout": connect_timeout,
                "tool_timeout": tool_timeout,
            }
        )

    return servers


def validate_hooks(raw_hooks: list | None) -> list:
    """校验 hooks 配置段。"""
    if raw_hooks is None:
        return []
    if not isinstance(raw_hooks, list):
        raise ConfigError("'hooks' must be a list of hook definitions")
    hook_ids: set[str] = set()
    for i, entry in enumerate(raw_hooks):
        if not isinstance(entry, dict):
            raise ConfigError(f"Hook #{i + 1}: must be a mapping")
        label = f"Hook #{i + 1}"
        _reject_unknown_fields(entry, _HOOK_FIELDS, label)
        hook_id = entry.get("id")
        if hook_id is not None:
            hook_id = _require_non_empty_string(hook_id, f"{label}.id")
            if hook_id in hook_ids:
                raise ConfigError(f"Duplicate hook id '{hook_id}'")
            hook_ids.add(hook_id)
        action = entry.get("action")
        if action is not None:
            if not isinstance(action, dict):
                raise ConfigError(f"{label}.action must be a mapping")
            _reject_unknown_fields(action, _HOOK_ACTION_FIELDS, f"{label}.action")
            headers = action.get("headers")
            if headers is not None:
                _validate_string_mapping(headers, f"{label}.action.headers")
        for bool_key in ("reject", "once", "async"):
            if bool_key in entry and not isinstance(entry[bool_key], bool):
                raise ConfigError(f"{label}.{bool_key} must be a boolean")
    return list(raw_hooks)


def validate_bool_field(value: object, field_name: str) -> bool:
    """校验一个布尔类型的配置字段。"""
    if not isinstance(value, bool):
        raise ConfigError(f"'{field_name}' must be a boolean")
    return value


def validate_worktree(raw_wt: dict | None) -> dict:
    """校验 worktree 配置段，返回清洗后的配置字典。"""
    defaults = {
        "symlink_directories": ["node_modules", "vendor"],
        "stale_cleanup_interval": 3600,
        "stale_cutoff_hours": 24,
    }

    if raw_wt is None:
        return defaults

    if not isinstance(raw_wt, dict):
        raise ConfigError("'worktree' must be a mapping")

    _reject_unknown_fields(raw_wt, _WORKTREE_FIELDS, "worktree")

    sym = raw_wt.get("symlink_directories", defaults["symlink_directories"])
    if not isinstance(sym, list) or not all(isinstance(s, str) for s in sym):
        raise ConfigError("'worktree.symlink_directories' must be a list of strings")

    interval = raw_wt.get("stale_cleanup_interval", defaults["stale_cleanup_interval"])
    if not isinstance(interval, int) or isinstance(interval, bool) or interval <= 0:
        raise ConfigError("'worktree.stale_cleanup_interval' must be a positive integer")

    cutoff = raw_wt.get("stale_cutoff_hours", defaults["stale_cutoff_hours"])
    if not isinstance(cutoff, int) or isinstance(cutoff, bool) or cutoff <= 0:
        raise ConfigError("'worktree.stale_cutoff_hours' must be a positive integer")

    return {
        "symlink_directories": sym,
        "stale_cleanup_interval": interval,
        "stale_cutoff_hours": cutoff,
    }


def validate_teammate_mode(mode: object) -> str:
    """校验 teammate_mode 取值。"""
    if not isinstance(mode, str) or mode not in VALID_TEAMMATE_MODES:
        raise ConfigError(
            f"Invalid teammate_mode '{mode}', "
            f"must be one of: {', '.join(repr(m) for m in sorted(VALID_TEAMMATE_MODES))}"
        )
    return mode


def _validate_mcp_layer(raw_mcp: object) -> None:
    if not isinstance(raw_mcp, list):
        raise ConfigError("'mcp_servers' must be a list of server configs")
    seen: set[str] = set()
    for i, entry in enumerate(raw_mcp):
        if not isinstance(entry, dict):
            raise ConfigError(f"MCP server #{i + 1}: must be a mapping")
        label = f"MCP server #{i + 1}"
        _reject_unknown_fields(entry, _MCP_LAYER_FIELDS, label)
        name = _require_non_empty_string(entry.get("name"), f"{label}.name")
        if name in seen:
            raise ConfigError(f"Duplicate MCP server name '{name}' in one layer")
        seen.add(name)
        disabled = entry.get("disabled", False)
        if not isinstance(disabled, bool):
            raise ConfigError(f"MCP server '{name}'.disabled must be a boolean")
        if disabled and len(entry) != 2:
            raise ConfigError(
                f"MCP server '{name}': disabled entry may only contain name and disabled"
            )
        if "command" in entry:
            _require_non_empty_string(
                entry["command"], f"MCP server '{name}'.command"
            )
        if "url" in entry:
            _validate_url(entry["url"], f"MCP server '{name}'.url")
        if "command" in entry and "url" in entry:
            raise ConfigError(
                f"MCP server '{name}': cannot have both 'command' and 'url'"
            )
        if "args" in entry:
            args = entry["args"]
            if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
                raise ConfigError(f"MCP server '{name}'.args must be a list of strings")
        if "headers" in entry:
            _validate_string_mapping(
                entry["headers"], f"MCP server '{name}'.headers"
            )
        if "env" in entry:
            _validate_string_mapping(entry["env"], f"MCP server '{name}'.env")
        for timeout_name in ("connect_timeout", "tool_timeout"):
            if timeout_name in entry:
                _validate_positive_number(
                    entry[timeout_name], f"MCP server '{name}'.{timeout_name}"
                )


def _validate_hooks_layer(raw_hooks: object) -> None:
    if not isinstance(raw_hooks, list):
        raise ConfigError("'hooks' must be a list of hook definitions")
    seen: set[str] = set()
    for i, entry in enumerate(raw_hooks):
        if not isinstance(entry, dict):
            raise ConfigError(f"Hook #{i + 1}: must be a mapping")
        label = f"Hook #{i + 1}"
        _reject_unknown_fields(entry, _HOOK_LAYER_FIELDS, label)
        hook_id = entry.get("id")
        if hook_id is not None:
            hook_id = _require_non_empty_string(hook_id, f"{label}.id")
            if hook_id in seen:
                raise ConfigError(f"Duplicate hook id '{hook_id}' in one layer")
            seen.add(hook_id)
        disabled = entry.get("disabled", False)
        if not isinstance(disabled, bool):
            raise ConfigError(f"{label}.disabled must be a boolean")
        if disabled:
            if hook_id is None:
                raise ConfigError(f"{label}: disabling an inherited hook requires an id")
            if len(entry) != 2:
                raise ConfigError(
                    f"Hook '{hook_id}': disabled entry may only contain id and disabled"
                )
            continue
        action = entry.get("action")
        if action is not None:
            if not isinstance(action, dict):
                raise ConfigError(f"{label}.action must be a mapping")
            _reject_unknown_fields(action, _HOOK_ACTION_FIELDS, f"{label}.action")
            if "headers" in action:
                _validate_string_mapping(action["headers"], f"{label}.action.headers")
        for bool_key in ("reject", "once", "async"):
            if bool_key in entry and not isinstance(entry[bool_key], bool):
                raise ConfigError(f"{label}.{bool_key} must be a boolean")


def _validate_worktree_layer(raw_wt: object) -> None:
    if not isinstance(raw_wt, dict):
        raise ConfigError("'worktree' must be a mapping")
    _reject_unknown_fields(raw_wt, _WORKTREE_FIELDS, "worktree")
    if "symlink_directories" in raw_wt:
        sym = raw_wt["symlink_directories"]
        if not isinstance(sym, list) or not all(isinstance(s, str) for s in sym):
            raise ConfigError("'worktree.symlink_directories' must be a list of strings")
    for field_name in ("stale_cleanup_interval", "stale_cutoff_hours"):
        if field_name not in raw_wt:
            continue
        value = raw_wt[field_name]
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ConfigError(f"'worktree.{field_name}' must be a positive integer")


def validate_config_layer_structure(raw: object) -> dict:
    """Validate one partial config layer without injecting default values.

    A layer may omit ``providers``.  Presence is deliberately preserved so a
    higher-priority layer can explicitly set ``false``, ``default`` or ``[]``
    instead of those values being mistaken for "not configured".
    """

    if not isinstance(raw, dict):
        raise ConfigError("Config layer must be a mapping")
    _reject_unknown_fields(raw, _TOP_LEVEL_FIELDS, "Config")
    _validate_env_reference_syntax(raw)

    if "providers" in raw:
        validate_providers(raw["providers"], strict_urls=True)
    if "permission_mode" in raw:
        validate_permission_mode(raw["permission_mode"])
    if "mcp_servers" in raw:
        _validate_mcp_layer(raw["mcp_servers"])
    if "hooks" in raw:
        _validate_hooks_layer(raw["hooks"])
    for field_name in (
        "enable_fork",
        "enable_verification_agent",
        "enable_coordinator_mode",
    ):
        if field_name in raw:
            validate_bool_field(raw[field_name], field_name)
    if "worktree" in raw:
        _validate_worktree_layer(raw["worktree"])
    if "teammate_mode" in raw:
        validate_teammate_mode(raw["teammate_mode"])
    return raw


def validate_config_structure(raw: object) -> dict:
    """校验的主入口。校验解析后的原始配置，返回清洗后的字典。

    返回的字典包含以下键：
        providers、permission_mode、mcp_servers、hooks、
        enable_fork、enable_verification_agent、worktree、
        teammate_mode、enable_coordinator_mode
    """
    if not isinstance(raw, dict):
        raise ConfigError("Config must be a mapping")
    _reject_unknown_fields(raw, _TOP_LEVEL_FIELDS, "Config")
    if "providers" not in raw:
        raise ConfigError("Config must contain a 'providers' list")

    # Syntax is always enforced. Environment availability is exposed through
    # ``validate_env_references`` for ``config check`` / ``doctor`` so loading a
    # config for offline inspection does not require every optional MCP secret.
    _validate_env_reference_syntax(raw)

    return {
        "providers": validate_providers(raw["providers"], strict_urls=True),
        "permission_mode": validate_permission_mode(raw.get("permission_mode", "default")),
        "mcp_servers": validate_mcp_servers(raw.get("mcp_servers")),
        "hooks": validate_hooks(raw.get("hooks")),
        "enable_fork": validate_bool_field(raw.get("enable_fork", False), "enable_fork"),
        "enable_verification_agent": validate_bool_field(
            raw.get("enable_verification_agent", False), "enable_verification_agent"
        ),
        "worktree": validate_worktree(raw.get("worktree")),
        "teammate_mode": validate_teammate_mode(raw.get("teammate_mode", "")),
        "enable_coordinator_mode": validate_bool_field(
            raw.get("enable_coordinator_mode", False), "enable_coordinator_mode"
        ),
    }
