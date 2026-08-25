from __future__ import annotations

import json
import traceback
from contextlib import suppress
from pathlib import Path

import httpx
import pytest

import mewcode.client as client_module
from mewcode.client import AnthropicClient, AuthenticationError, OpenAICompatClient
from mewcode.config import (
    AppConfig,
    MCPServerConfig,
    ProviderConfig,
    load_config,
)
from mewcode.validator import (
    ConfigError,
    validate_config_structure,
    validate_env_references,
)


def _provider(**overrides: object) -> ProviderConfig:
    values: dict[str, object] = {
        "name": "test",
        "protocol": "openai",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4.1",
    }
    values.update(overrides)
    return ProviderConfig(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "protocol,base_url,env_name",
    [
        ("openai", "https://api.openai.com/v1", "OPENAI_API_KEY"),
        ("openai", "https://api.openai.com:443/v1/", "OPENAI_API_KEY"),
        ("anthropic", "https://api.anthropic.com", "ANTHROPIC_API_KEY"),
    ],
)
def test_official_environment_keys_are_bound_to_exact_endpoints(
    monkeypatch: pytest.MonkeyPatch,
    protocol: str,
    base_url: str,
    env_name: str,
) -> None:
    monkeypatch.setenv(env_name, "official-sentinel")
    provider = _provider(protocol=protocol, base_url=base_url)

    assert provider.resolve_api_key() == "official-sentinel"
    assert provider.credential_source() == f"environment:{env_name}"


@pytest.mark.parametrize(
    "protocol,base_url",
    [
        ("openai", "https://proxy.example/v1"),
        ("openai", "https://api.openai.com.evil.test/v1"),
        ("openai", "http://api.openai.com/v1"),
        ("openai", "https://api.openai.com:8443/v1"),
        ("openai-compat", "https://api.openai.com/v1"),
        ("anthropic", "https://gateway.example/v1"),
    ],
)
def test_custom_endpoints_never_inherit_official_environment_keys(
    monkeypatch: pytest.MonkeyPatch, protocol: str, base_url: str
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "official-openai-sentinel")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "official-anthropic-sentinel")

    provider = _provider(protocol=protocol, base_url=base_url)

    assert provider.resolve_api_key() == ""
    assert provider.credential_source() == "missing explicit source"


def test_custom_endpoint_uses_only_its_declared_environment_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "official-sentinel")
    monkeypatch.setenv("THIRD_PARTY_API_KEY", "third-party-sentinel")
    provider = _provider(
        protocol="openai-compat",
        base_url="https://third.example/v1",
        api_key_env="THIRD_PARTY_API_KEY",
    )

    assert provider.resolve_api_key() == "third-party-sentinel"


def test_custom_endpoint_without_explicit_source_fails_before_transport_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "official-sentinel")
    transport_factory_called = False

    def forbidden_transport() -> httpx.AsyncClient:
        nonlocal transport_factory_called
        transport_factory_called = True
        raise AssertionError("transport must not be created")

    monkeypatch.setattr(client_module, "_new_http_client", forbidden_transport)
    provider = _provider(
        protocol="openai-compat", base_url="https://third.example/v1"
    )

    with pytest.raises(AuthenticationError, match="explicit credential source"):
        OpenAICompatClient(provider)

    assert not transport_factory_called


@pytest.mark.asyncio
async def test_auth_none_sends_no_authorization_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"object": "list", "data": []})

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(client_module, "_new_http_client", lambda: http_client)
    provider = _provider(
        protocol="openai-compat",
        base_url="https://local.example/v1",
        auth="none",
    )
    client = OpenAICompatClient(provider)
    try:
        await client._client.models.list()
    finally:
        await client.aclose()

    assert len(captured) == 1
    assert "authorization" not in captured[0].headers


@pytest.mark.asyncio
async def test_anthropic_auth_none_sends_neither_supported_auth_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(500, json={"error": {"message": "fixture"}})

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(client_module, "_new_http_client", lambda: http_client)
    provider = _provider(
        protocol="anthropic",
        base_url="https://local.example",
        model="claude-test",
        auth="none",
    )
    client = AnthropicClient(provider)
    try:
        with suppress(Exception):
            await client._client.models.retrieve("claude-test")
    finally:
        await client.aclose()

    assert captured
    assert "authorization" not in captured[0].headers
    assert "x-api-key" not in captured[0].headers


@pytest.mark.asyncio
async def test_provider_redirect_is_not_followed_with_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            302,
            headers={"location": "https://unreviewed.example/v1/models"},
        )

    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=False
    )
    monkeypatch.setattr(client_module, "_new_http_client", lambda: http_client)
    monkeypatch.setenv("OPENAI_API_KEY", "official-sentinel")
    monkeypatch.setenv("THIRD_PARTY_API_KEY", "third-party-sentinel")
    provider = _provider(
        protocol="openai-compat",
        base_url="https://approved.example/v1",
        api_key_env="THIRD_PARTY_API_KEY",
    )
    client = OpenAICompatClient(provider)
    try:
        with pytest.raises(Exception):
            await client._client.models.list()
    finally:
        await client.aclose()

    assert requests
    assert {request.url.host for request in requests} == {"approved.example"}
    # The explicitly selected third-party key is used; the official key can
    # never be selected for this compatibility endpoint.
    assert all(
        request.headers.get("authorization") == "Bearer third-party-sentinel"
        for request in requests
    )


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_three_layer_merge_preserves_explicit_falsey_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.chdir(project)
    _write(
        home / ".mewcode" / "config.yaml",
        """
providers:
  - name: primary
    protocol: openai
    base_url: https://api.openai.com/v1
    model: gpt-4.1
permission_mode: acceptEdits
enable_fork: true
enable_verification_agent: true
enable_coordinator_mode: true
teammate_mode: in-process
worktree:
  symlink_directories: [node_modules, .venv]
  stale_cleanup_interval: 111
  stale_cutoff_hours: 222
mcp_servers:
  - name: inherited
    command: old-command
hooks:
  - id: inherited-hook
    event: startup
    action: {type: command, command: old-command}
""",
    )
    # This partial layer intentionally has no providers.
    _write(
        project / ".mewcode" / "config.yaml",
        """
permission_mode: default
enable_fork: false
worktree:
  symlink_directories: []
  stale_cutoff_hours: 12
""",
    )
    _write(
        project / ".mewcode" / "config.local.yaml",
        """
enable_verification_agent: false
enable_coordinator_mode: false
teammate_mode: ""
mcp_servers: []
hooks: []
""",
    )

    config = load_config()

    assert [provider.name for provider in config.providers] == ["primary"]
    assert config.permission_mode == "default"
    assert config.enable_fork is False
    assert config.enable_verification_agent is False
    assert config.enable_coordinator_mode is False
    assert config.teammate_mode == ""
    assert config.mcp_servers == []
    assert config.raw_hooks == []
    assert config.worktree.symlink_directories == []
    assert config.worktree.stale_cleanup_interval == 111
    assert config.worktree.stale_cutoff_hours == 12
    assert len(config.config_sources) == 3
    provenance = config.explain_safe()["provenance"]
    assert provenance["providers"] == str(
        (home / ".mewcode" / "config.yaml").resolve()
    )
    assert provenance["providers[name=primary]"] == provenance["providers"]
    assert provenance["permission_mode"] == str(
        (project / ".mewcode" / "config.yaml").resolve()
    )
    assert provenance["enable_verification_agent"] == str(
        (project / ".mewcode" / "config.local.yaml").resolve()
    )
    assert provenance["mcp_servers"] == str(
        (project / ".mewcode" / "config.local.yaml").resolve()
    )
    assert provenance["hooks"] == provenance["mcp_servers"]
    assert "mcp_servers[name=inherited]" not in provenance
    assert "hooks[id=inherited-hook]" not in provenance
    assert provenance["worktree.symlink_directories"] == str(
        (project / ".mewcode" / "config.yaml").resolve()
    )
    assert provenance["worktree.stale_cleanup_interval"] == str(
        (home / ".mewcode" / "config.yaml").resolve()
    )


def test_named_mcp_and_hook_entries_can_be_overridden_or_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.chdir(project)
    _write(
        home / ".mewcode" / "config.yaml",
        """
providers:
  - {name: primary, protocol: openai, base_url: https://api.openai.com/v1, model: gpt-4.1}
mcp_servers:
  - {name: keep, command: old, args: [old]}
  - {name: remove, command: remove-me}
hooks:
  - {id: replace, event: startup, action: {type: command, command: old}}
  - {id: remove, event: startup, action: {type: command, command: remove-me}}
""",
    )
    _write(
        project / ".mewcode" / "config.local.yaml",
        """
mcp_servers:
  - {name: keep, args: [new]}
  - {name: remove, disabled: true}
hooks:
  - {id: replace, event: shutdown, action: {type: command, command: new}}
  - {id: remove, disabled: true}
""",
    )

    config = load_config()

    assert [(server.name, server.command, server.args) for server in config.mcp_servers] == [
        ("keep", "old", ["new"])
    ]
    assert config.raw_hooks == [
        {
            "id": "replace",
            "event": "shutdown",
            "action": {"type": "command", "command": "new"},
        }
    ]
    provenance = config.config_provenance
    local_source = str(
        (project / ".mewcode" / "config.local.yaml").resolve()
    )
    assert provenance["mcp_servers[name=keep]"] == local_source
    assert "mcp_servers[name=remove]" not in provenance
    assert provenance["hooks[id=replace]"] == local_source
    assert "hooks[id=remove]" not in provenance


def test_eviforge_config_is_an_isolated_single_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected = tmp_path / "selected.yaml"
    _write(
        selected,
        """
providers:
  - {name: selected, protocol: openai, base_url: https://api.openai.com/v1, model: gpt-4.1}
""",
    )
    monkeypatch.setenv("EVIFORGE_CONFIG", str(selected))

    config = load_config()

    assert [provider.name for provider in config.providers] == ["selected"]
    assert config.config_sources == (selected.resolve(),)


def test_eviforge_home_config_is_supported_as_a_layered_compatibility_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.chdir(project)
    _write(
        home / ".eviforge" / "config.yaml",
        """
providers:
  - {name: local, protocol: openai-compat, base_url: http://127.0.0.1:9999/v1, model: local, auth: none}
""",
    )

    config = load_config()

    assert [provider.name for provider in config.providers] == ["local"]
    assert config.config_load_mode == "layered"
    assert config.user_config_sources == (
        (home / ".eviforge" / "config.yaml").resolve(),
    )


def test_trust_summary_blocks_only_active_user_level_executable_integrations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.chdir(project)
    secret = "do-not-print-command-or-header"
    _write(
        home / ".mewcode" / "config.yaml",
        f"""
providers:
  - {{name: local, protocol: openai-compat, base_url: http://127.0.0.1:9999/v1, model: local, auth: none}}
mcp_servers:
  - name: inherited-mcp
    command: {secret}
    env: {{TOKEN: literal-{secret}}}
hooks:
  - id: inherited-hook
    event: startup
    action: {{type: command, command: {secret}}}
  - id: prompt-only
    event: user_prompt_submit
    action: {{type: prompt, message: safe}}
""",
    )
    _write(
        project / ".mewcode" / "config.yaml",
        "permission_mode: default\n",
    )

    config = load_config()
    summary = config.integration_trust_summary()
    rendered = json.dumps(summary)

    assert summary["requires_trust"] is True
    assert summary["decision"] == "blocked"
    assert {item["kind"] for item in summary["integrations"]} == {"hook", "mcp"}
    assert "prompt-only" not in rendered
    assert secret not in rendered
    assert config.integration_trust_summary(trust_config=True)["decision"] == "trusted_by_flag"


def test_project_empty_lists_remove_inherited_integration_trust_requirement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.chdir(project)
    _write(
        home / ".mewcode" / "config.yaml",
        """
providers:
  - {name: local, protocol: openai-compat, base_url: http://127.0.0.1:9999/v1, model: local, auth: none}
mcp_servers:
  - {name: inherited-mcp, command: inherited-command}
hooks:
  - {id: inherited-hook, event: startup, action: {type: command, command: inherited-command}}
""",
    )
    _write(
        project / ".mewcode" / "config.yaml",
        "mcp_servers: []\nhooks: []\n",
    )

    summary = load_config().integration_trust_summary()

    assert summary["requires_trust"] is False
    assert summary["integrations"] == []


def test_partial_project_mcp_override_does_not_bless_inherited_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.chdir(project)
    _write(
        home / ".mewcode" / "config.yaml",
        """
providers:
  - {name: local, protocol: openai-compat, base_url: http://127.0.0.1:9999/v1, model: local, auth: none}
mcp_servers:
  - {name: inherited-mcp, command: inherited-command, args: [old]}
""",
    )
    _write(
        project / ".mewcode" / "config.yaml",
        """
mcp_servers:
  - {name: inherited-mcp, args: [reviewed]}
""",
    )

    config = load_config()
    summary = config.integration_trust_summary()

    assert config.mcp_servers[0].command == "inherited-command"
    assert summary["requires_trust"] is True
    assert summary["integrations"] == [
        {
            "kind": "mcp",
            "name": "inherited-mcp",
            "transport": "stdio",
            "source_scope": "user",
        }
    ]


def test_explicit_single_file_does_not_require_layered_config_trust(
    tmp_path: Path,
) -> None:
    selected = tmp_path / "selected.yaml"
    _write(
        selected,
        """
providers:
  - {name: local, protocol: openai-compat, base_url: http://127.0.0.1:9999/v1, model: local, auth: none}
mcp_servers:
  - {name: selected-mcp, command: selected-command}
hooks:
  - {id: selected-hook, event: startup, action: {type: command, command: selected-command}}
""",
    )

    summary = load_config(selected).integration_trust_summary()

    assert summary["requires_trust"] is False
    assert summary["decision"] == "explicit_single_file"


@pytest.mark.parametrize("protocol", ["openai", "openai-compat"])
def test_thinking_true_is_rejected_for_non_anthropic_protocols(
    protocol: str,
) -> None:
    with pytest.raises(ConfigError, match="only supported by the anthropic protocol"):
        validate_config_structure(
            {
                "providers": [
                    {
                        "name": "invalid-thinking",
                        "protocol": protocol,
                        "base_url": "https://api.openai.com/v1",
                        "model": "model",
                        "thinking": True,
                    }
                ]
            }
        )


def test_thinking_true_remains_valid_for_anthropic() -> None:
    validated = validate_config_structure(
        {
            "providers": [
                {
                    "name": "thinking",
                    "protocol": "anthropic",
                    "base_url": "https://api.anthropic.com",
                    "model": "claude-test",
                    "thinking": True,
                }
            ]
        }
    )

    assert validated["providers"][0]["thinking"] is True


@pytest.mark.parametrize(
    "content,match",
    [
        (
            "providers:\n  - {name: p, protocol: openai, base_url: https://api.openai.com/v1, model: x, typo: true}\n",
            "unknown field",
        ),
        (
            "providers:\n  - {name: p, protocol: openai, base_url: https://api.openai.com/v1, model: x}\nunknown: true\n",
            "unknown field",
        ),
        (
            "providers:\n  - {name: p, protocol: openai, base_url: https://api.openai.com/v1, model: x}\nenable_fork: true\nenable_fork: false\n",
            "Duplicate config field",
        ),
    ],
)
def test_strict_schema_rejects_unknown_or_duplicate_fields(
    tmp_path: Path, content: str, match: str
) -> None:
    path = tmp_path / "config.yaml"
    _write(path, content)

    with pytest.raises(ConfigError, match=match):
        load_config(path)


def test_unresolved_environment_diagnostic_names_source_but_not_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MISSING_TOKEN", raising=False)
    raw = {"headers": {"Authorization": "Bearer ${MISSING_TOKEN}"}}

    with pytest.raises(ConfigError) as raised:
        validate_env_references(raw)

    assert "MISSING_TOKEN" in str(raised.value)
    assert "Bearer" not in str(raised.value)


def test_config_repr_safe_export_and_yaml_errors_never_echo_secret(
    tmp_path: Path,
) -> None:
    secret = "credential-sentinel-never-print"
    provider = _provider(api_key=secret)
    config = AppConfig(
        providers=[provider],
        mcp_servers=[
            MCPServerConfig(
                name="mcp",
                command="server",
                args=[secret],
                headers={"Authorization": secret},
                env={"TOKEN": secret},
            )
        ],
        raw_hooks=[{"id": "safe-id", "secret": secret}],
    )

    rendered = (
        repr(config)
        + json.dumps(config.to_safe_dict())
        + json.dumps(config.explain_safe())
    )
    assert secret not in rendered

    bad_yaml = tmp_path / "bad.yaml"
    _write(bad_yaml, f'providers: "{secret}\n')
    with pytest.raises(ConfigError) as raised:
        load_config(bad_yaml)
    snapshot = "".join(
        traceback.format_exception(type(raised.value), raised.value, raised.value.__traceback__)
    )
    assert secret not in snapshot
