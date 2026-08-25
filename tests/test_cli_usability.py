from __future__ import annotations

import json
from pathlib import Path

import pytest

import mewcode.__main__ as cli
from mewcode.client import LLMClient
from mewcode.config import AppConfig, ConfigError, ProviderConfig
from mewcode.permissions import PermissionMode
from mewcode.tools.base import StreamEnd, TextDelta, ToolCallComplete


class _OneShotClient(LLMClient):
    async def stream(self, conversation, system="", tools=None):
        yield TextDelta("done")
        yield StreamEnd(stop_reason="end_turn", input_tokens=3, output_tokens=1)


class _ReadThenAnswerClient(LLMClient):
    def __init__(self) -> None:
        self.calls = 0

    async def stream(self, conversation, system="", tools=None):
        self.calls += 1
        if self.calls == 1:
            yield TextDelta("checking")
            yield ToolCallComplete(
                "read-fixture",
                "ReadFile",
                {"file_path": "fixture.txt"},
            )
            yield StreamEnd(stop_reason="tool_use", input_tokens=5, output_tokens=2)
            return
        if self.calls == 2:
            yield TextDelta("verified")
        yield StreamEnd(stop_reason="end_turn", input_tokens=4, output_tokens=1)


class _ReadThenFailClient(LLMClient):
    def __init__(self) -> None:
        self.calls = 0

    async def stream(self, conversation, system="", tools=None):
        self.calls += 1
        if self.calls == 1:
            yield ToolCallComplete(
                "read-before-failure",
                "ReadFile",
                {"file_path": "fixture.txt"},
            )
            yield StreamEnd(stop_reason="tool_use", input_tokens=5, output_tokens=1)
            return
        raise RuntimeError("provider failed after tool result")


def test_help_and_version_have_no_workspace_side_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as help_exit:
        cli.main(["--help"])
    with pytest.raises(SystemExit) as version_exit:
        cli.main(["--version"])
    assert help_exit.value.code == 0
    assert version_exit.value.code == 0
    assert not (tmp_path / ".mewcode").exists()


def test_init_refuses_to_overwrite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_configure_logging", lambda _level: None)
    assert cli.main(["init"]) == 0
    target = tmp_path / ".mewcode" / "config.yaml"
    original = target.read_text(encoding="utf-8")
    assert cli.main(["init"]) == 2
    assert target.read_text(encoding="utf-8") == original


def test_dag_validate_is_offline_and_does_not_load_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    graph = tmp_path / "graph.json"
    graph.write_text(
        json.dumps(
            {
                "nodes": [
                    {
                        "node_id": "inspect",
                        "role": "explorer",
                        "objective": "inspect",
                        "acceptance_criteria": [
                            {
                                "criterion_id": "ok",
                                "description": "fixture passes",
                                "verifier_argv": ["python", "-c", "raise SystemExit(0)"],
                            }
                        ],
                        "artifact_contract": {"required_outputs": ["report"]},
                        "token_budget": 10,
                        "timeout_seconds": 1,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_configure_logging", lambda _level: None)
    monkeypatch.setattr(
        cli,
        "load_config",
        lambda *_args, **_kwargs: pytest.fail("offline validation loaded config"),
    )
    assert cli.main(["dag", "validate", str(graph), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["valid"] is True
    assert not (tmp_path / ".mewcode").exists()


@pytest.mark.asyncio
async def test_headless_composition_returns_versioned_result_and_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "control"))

    async def no_probe(_provider):
        return None

    monkeypatch.setattr("mewcode.client.resolve_context_window", no_probe)
    provider = ProviderConfig(
        name="local",
        protocol="openai-compat",
        base_url="http://127.0.0.1:9999/v1",
        model="fixture-model",
        auth="none",
    )
    config = AppConfig(providers=[provider])

    result = await cli._run_prompt_with_client(
        config,
        PermissionMode.DEFAULT,
        None,
        "say done",
        contract_path=None,
        provider=provider,
        client=_OneShotClient(),
        background_timeout=0.1,
    )

    assert result.schema_version == 1
    assert result.status == "succeeded"
    assert result.provider == "local"
    assert result.result == "done"
    assert "sessions" in result.capabilities
    assert list((tmp_path / ".mewcode" / "sessions").glob("*.meta"))


@pytest.mark.asyncio
async def test_headless_session_persists_tool_use_and_result_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mewcode.memory.session import SessionManager

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "control"))
    (tmp_path / "fixture.txt").write_text("evidence", encoding="utf-8")

    async def no_probe(_provider):
        return None

    monkeypatch.setattr("mewcode.client.resolve_context_window", no_probe)
    provider = ProviderConfig(
        name="local",
        protocol="openai-compat",
        base_url="http://127.0.0.1:9999/v1",
        model="fixture-model",
        auth="none",
    )
    result = await cli._run_prompt_with_client(
        AppConfig(providers=[provider]),
        PermissionMode.DEFAULT,
        None,
        "inspect the fixture",
        contract_path=None,
        provider=provider,
        client=_ReadThenAnswerClient(),
        background_timeout=0.1,
    )

    manager = SessionManager(str(tmp_path))
    meta = manager.list()[0]
    resumed = manager.resume(meta.id)
    assert resumed is not None
    try:
        tool_uses = [
            tool_use
            for message in resumed.messages
            for tool_use in message.tool_uses
        ]
        tool_results = [
            tool_result
            for message in resumed.messages
            for tool_result in message.tool_results
        ]
        assert result.result == "verified"
        assert [(item.tool_use_id, item.tool_name) for item in tool_uses] == [
            ("read-fixture", "ReadFile")
        ]
        assert [item.tool_use_id for item in tool_results] == ["read-fixture"]
        assert "evidence" in tool_results[0].content
    finally:
        resumed.session.close()


@pytest.mark.asyncio
async def test_headless_failure_still_persists_completed_tool_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mewcode.memory.session import SessionManager

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "control"))
    (tmp_path / "fixture.txt").write_text("durable evidence", encoding="utf-8")

    async def no_probe(_provider):
        return None

    monkeypatch.setattr("mewcode.client.resolve_context_window", no_probe)
    provider = ProviderConfig(
        name="local",
        protocol="openai-compat",
        base_url="http://127.0.0.1:9999/v1",
        model="fixture-model",
        auth="none",
    )
    with pytest.raises(RuntimeError, match="provider failed after tool result"):
        await cli._run_prompt_with_client(
            AppConfig(providers=[provider]),
            PermissionMode.DEFAULT,
            None,
            "inspect before failure",
            contract_path=None,
            provider=provider,
            client=_ReadThenFailClient(),
            background_timeout=0.1,
        )

    manager = SessionManager(str(tmp_path))
    meta = manager.list()[0]
    resumed = manager.resume(meta.id)
    assert resumed is not None
    try:
        assert resumed.messages[0].content == "inspect before failure"
        assert resumed.messages[1].tool_uses[0].tool_use_id == "read-before-failure"
        assert resumed.messages[2].tool_results[0].tool_use_id == "read-before-failure"
        assert "durable evidence" in resumed.messages[2].tool_results[0].content
    finally:
        resumed.session.close()


@pytest.mark.asyncio
async def test_user_prompt_matching_old_scaffolding_prefix_is_not_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mewcode.memory.session import SessionManager

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "control"))

    async def no_probe(_provider):
        return None

    monkeypatch.setattr("mewcode.client.resolve_context_window", no_probe)
    provider = ProviderConfig(
        name="local",
        protocol="openai-compat",
        base_url="http://127.0.0.1:9999/v1",
        model="fixture-model",
        auth="none",
    )
    prompt = "Current working directory: this is user-authored text"
    await cli._run_prompt_with_client(
        AppConfig(providers=[provider]),
        PermissionMode.DEFAULT,
        None,
        prompt,
        contract_path=None,
        provider=provider,
        client=_OneShotClient(),
        background_timeout=0.1,
    )

    manager = SessionManager(str(tmp_path))
    resumed = manager.resume(manager.list()[0].id)
    assert resumed is not None
    try:
        assert [message.content for message in resumed.messages[:2]] == [prompt, "done"]
    finally:
        resumed.session.close()


@pytest.mark.asyncio
async def test_headless_resume_requires_explicit_provider_profile_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mewcode.memory.session import SessionManager

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "control"))

    async def no_probe(_provider):
        return None

    monkeypatch.setattr("mewcode.client.resolve_context_window", no_probe)
    first = ProviderConfig(
        name="first",
        protocol="openai-compat",
        base_url="http://127.0.0.1:9999/v1",
        model="fixture-model",
        auth="none",
    )
    await cli._run_prompt_with_client(
        AppConfig(providers=[first]),
        PermissionMode.DEFAULT,
        None,
        "first turn",
        contract_path=None,
        provider=first,
        client=_OneShotClient(),
        background_timeout=0.1,
    )
    manager = SessionManager(str(tmp_path))
    session_id = manager.list()[0].id
    second = ProviderConfig(
        name="first",
        protocol="openai-compat",
        base_url="http://127.0.0.1:9999/v1",
        model="changed-model",
        auth="none",
    )

    probe_calls = 0

    async def count_probe(_provider):
        nonlocal probe_calls
        probe_calls += 1

    monkeypatch.setattr("mewcode.client.resolve_context_window", count_probe)
    with pytest.raises(ConfigError, match="provider profile changed") as exc_info:
        await cli._run_prompt_with_client(
            AppConfig(providers=[second]),
            PermissionMode.DEFAULT,
            None,
            "resume turn",
            contract_path=None,
            provider=second,
            client=_OneShotClient(),
            resume_session_id=session_id,
            background_timeout=0.1,
        )
    assert getattr(exc_info.value, "error_code", "") == "session.profile_drift"
    assert probe_calls == 0

    result = await cli._run_prompt_with_client(
        AppConfig(providers=[second]),
        PermissionMode.DEFAULT,
        None,
        "resume turn",
        contract_path=None,
        provider=second,
        client=_OneShotClient(),
        resume_session_id=session_id,
        allow_session_drift=True,
        background_timeout=0.1,
    )
    assert result.status == "succeeded"
    updated_meta = manager.get_meta(session_id)
    assert updated_meta.provider_name == "first"
    assert updated_meta.provider_profile["model"] == "changed-model"

    updated_meta.capabilities.append("tampered-capability")
    updated_meta.save(manager._sessions_dir / f"{session_id}.meta")
    probe_calls = 0
    with pytest.raises(ConfigError, match="capability profile changed"):
        await cli._run_prompt_with_client(
            AppConfig(providers=[second]),
            PermissionMode.DEFAULT,
            None,
            "must not contact provider",
            contract_path=None,
            provider=second,
            client=_OneShotClient(),
            resume_session_id=session_id,
            background_timeout=0.1,
        )
    assert probe_calls == 0


def test_cli_rejects_session_path_traversal(tmp_path: Path) -> None:
    from mewcode.memory.session import SessionManager

    manager = SessionManager(str(tmp_path))
    with pytest.raises(ValueError, match="invalid session id"):
        manager.delete("../outside")


def test_headless_blocks_inherited_hook_and_mcp_before_runtime_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    sentinel = tmp_path / "startup-hook-ran"
    home_config = home / ".mewcode" / "config.yaml"
    home_config.parent.mkdir(parents=True)
    home_config.write_text(
        f"""
providers:
  - {{name: local, protocol: openai-compat, base_url: http://127.0.0.1:9999/v1, model: local, auth: none}}
mcp_servers:
  - {{name: inherited-mcp, command: must-not-start-mcp}}
hooks:
  - id: inherited-startup
    event: startup
    action:
      type: command
      command: python -c \"from pathlib import Path; Path(r'{sentinel}').touch()\"
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.chdir(project)
    monkeypatch.setattr(cli, "_configure_logging", lambda _level: None)

    async def forbidden_runtime(*_args, **_kwargs):
        pytest.fail("headless runtime crossed the config trust boundary")

    monkeypatch.setattr(cli, "_run_prompt", forbidden_runtime)

    assert cli.main(["--json", "-p", "must not execute"]) == 2
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    message = payload["error"]["message"]
    assert payload["error"]["code"] == "config.integration_trust_required"
    assert payload["error"]["details"]["trust_summary"]["decision"] == "blocked"
    assert "Trust summary" in message
    assert "inherited-startup" in message
    assert "must-not-start-mcp" not in message
    assert captured.err == ""
    assert not sentinel.exists()


@pytest.mark.parametrize(
    "command,expected_exit",
    [
        (["config", "check", "--json"], 2),
        (["doctor", "--json"], 1),
    ],
)
def test_config_check_and_doctor_validate_environment_references(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: list[str],
    expected_exit: int,
) -> None:
    monkeypatch.delenv("EVIFORGE_UNSET_FOR_CLI", raising=False)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
providers:
  - {name: local, protocol: openai-compat, base_url: http://127.0.0.1:9999/v1, model: local, auth: none}
mcp_servers:
  - name: remote
    url: https://mcp.example.test
    headers:
      Authorization: Bearer ${EVIFORGE_UNSET_FOR_CLI}
""",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_configure_logging", lambda _level: None)

    assert cli.main(["--config", str(config_path), *command]) == expected_exit
    payload = json.loads(capsys.readouterr().out)
    environment = next(
        item for item in payload["items"] if item["id"] == "config.environment"
    )
    rendered = json.dumps(environment)
    assert environment["status"] == "error"
    assert "EVIFORGE_UNSET_FOR_CLI" in rendered
    assert "Bearer" not in rendered


@pytest.mark.asyncio
async def test_headless_recovery_blocks_before_provider_factory_or_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    provider = ProviderConfig(
        name="blocked-provider",
        protocol="openai-compat",
        base_url="http://127.0.0.1:9999/v1",
        model="fixture-model",
        auth="none",
    )
    config = AppConfig(providers=[provider])
    factory_calls = 0
    metadata_calls = 0

    def forbidden_factory(_provider):
        nonlocal factory_calls
        factory_calls += 1
        raise AssertionError("Provider factory crossed recovery boundary")

    async def forbidden_metadata(_provider):
        nonlocal metadata_calls
        metadata_calls += 1
        raise AssertionError("Provider metadata crossed recovery boundary")

    monkeypatch.setattr(
        cli,
        "_headless_recovery_items",
        lambda: [
            {
                "action_id": "uncertain-1",
                "state": "uncertain",
                "recommendation": "human_review_only",
                "reason": "external effect outcome is unknown",
            }
        ],
    )
    monkeypatch.setattr("mewcode.client.create_client", forbidden_factory)
    monkeypatch.setattr("mewcode.client.resolve_context_window", forbidden_metadata)

    result = await cli._run_prompt(
        config,
        PermissionMode.DEFAULT,
        None,
        "must not run",
    )

    assert result.status == "recovery_blocked"
    assert result.recovery[0]["action_id"] == "uncertain-1"
    assert factory_calls == 0
    assert metadata_calls == 0
