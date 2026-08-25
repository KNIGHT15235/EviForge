from __future__ import annotations

import os
import re

from mewcode.config import MCPServerConfig, resolve_env_vars

_ENV_REFERENCE = re.compile(r"\$\{([^}]+)\}")


def config_secret_values(config: MCPServerConfig) -> frozenset[str]:
    """Return raw and resolved transport credential values for redaction."""

    values: set[str] = set()
    raw_values = (*config.headers.values(), *config.env.values())
    for raw_value in raw_values:
        raw = str(raw_value)
        if raw:
            values.add(raw)
        resolved = resolve_env_vars(raw)
        if resolved:
            values.add(resolved)
        for variable_name in _ENV_REFERENCE.findall(raw):
            environment_value = os.environ.get(variable_name, "")
            if environment_value:
                values.add(environment_value)

    # Authentication schemes often prefix the credential.  Redact both the
    # complete configured header and the credential portion in case a
    # transport exception reports only the latter.
    for header_value in config.headers.values():
        raw = str(header_value).strip()
        if " " in raw:
            _scheme, credential = raw.split(maxsplit=1)
            if credential:
                values.add(credential)
            resolved_credential = resolve_env_vars(credential)
            if resolved_credential:
                values.add(resolved_credential)
    return frozenset(value for value in values if value)


def redact_config_secrets(value: object, config: MCPServerConfig) -> str:
    return redact_secret_values(value, config_secret_values(config))


def redact_secret_values(value: object, secrets: frozenset[str]) -> str:
    text = str(value)
    for secret in sorted(secrets, key=len, reverse=True):
        text = text.replace(secret, "<redacted>")
    return text


__all__ = [
    "config_secret_values",
    "redact_config_secrets",
    "redact_secret_values",
]
