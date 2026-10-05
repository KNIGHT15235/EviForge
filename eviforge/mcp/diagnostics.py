"""Credential-safe transport diagnostics, independent of remote error text."""
from __future__ import annotations

import asyncio
import re
from typing import Any

from eviforge.config import MCPServerConfig, ConfigError, resolve_env_vars


def resolve_required(value: str) -> str:
    resolved = resolve_env_vars(value)
    missing = re.findall(r"\$\{([^}]+)\}", resolved)
    if missing:
        raise ConfigError("auth_required: missing environment variables: " + ", ".join(missing))
    return resolved


def safe_error(exc: BaseException, config: MCPServerConfig | None = None) -> str:
    # Transport errors can include complete URLs, headers, argv and remote bodies.
    # Classify locally; do not interpolate raw exception strings into reports.
    if isinstance(exc, ConfigError):
        return str(exc) if str(exc).startswith("auth_required:") else "config_error"
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return "timeout"
    code = getattr(getattr(exc, "response", None), "status_code", None)
    if code in {401, 403}:
        return "auth_required"
    return f"transport_error ({type(exc).__name__})"


def redact(value: str, config: MCPServerConfig) -> str:
    for candidate in (*config.env.values(), *config.headers.values()):
        resolved = resolve_env_vars(candidate)
        for secret in (resolved, resolved.removeprefix("Bearer ")):
            if len(secret) >= 8 and "${" not in secret:
                value = value.replace(secret, "[REDACTED]")
    return value


def redact_data(value: Any, config: MCPServerConfig) -> Any:
    if isinstance(value, str):
        return redact(value, config)
    if isinstance(value, dict):
        return {key: redact_data(item, config) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_data(item, config) for item in value]
    return value
