from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

import mewcode.__main__ as cli
from mewcode.agent import CompletionBlockedError, CompletionBlockedEvent
from mewcode.client import (
    AmbiguousStreamError,
    AuthenticationError,
    LLMClient,
    NetworkError,
)
from mewcode.config import AppConfig, ProviderConfig
from mewcode.exit_codes import BudgetExceededError, ExitCode
from mewcode.permissions import PermissionMode
from mewcode.tools.base import StreamEnd, TextDelta


SCHEMA = json.loads(
    (Path(__file__).parents[1] / "schemas" / "run-result.schema.json").read_text(
        encoding="utf-8"
    )
)


def _config(*, hooks: list[dict] | None = None) -> AppConfig:
    return AppConfig(
        providers=[
            ProviderConfig(
                name="fixture",
                protocol="openai-compat",
                base_url="http://127.0.0.1:9999/v1",
                model="fixture-model",
                auth="none",
            )
        ],
        raw_hooks=hooks or [],
    )


def _assert_json_failure(
    capsys: pytest.CaptureFixture[str],
    *,
    status: str,
    exit_code: int,
    error_code: str,
) -> dict[str, object]:
    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    jsonschema.validate(payload, SCHEMA)
    assert payload["status"] == status
    assert payload["exit_code"] == exit_code
    assert payload["error"]["code"] == error_code
    return payload


def test_missing_config_is_a_schema_valid_stdout_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "_configure_logging", lambda _level: None)
    missing = tmp_path / "missing.yaml"

    code = cli.main(["--json", "--config", str(missing), "-p", "hello"])

    assert code == int(ExitCode.CONFIGURATION)
    _assert_json_failure(
        capsys,
        status="config_error",
        exit_code=int(ExitCode.CONFIGURATION),
        error_code="config.not_found",
    )


def test_bad_hook_config_is_a_schema_valid_stdout_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = _config(hooks=[{"id": "bad", "event": "not_an_event", "action": {}}])
    monkeypatch.setattr(cli, "_configure_logging", lambda _level: None)
    monkeypatch.setattr(cli, "_load_app_config", lambda _path: config)

    code = cli.main(["--json", "-p", "hello"])

    assert code == int(ExitCode.CONFIGURATION)
    _assert_json_failure(
        capsys,
        status="config_error",
        exit_code=int(ExitCode.CONFIGURATION),
        error_code="hook.invalid_config",
    )


def test_unknown_provider_is_a_schema_valid_stdout_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "_configure_logging", lambda _level: None)
    monkeypatch.setattr(cli, "_load_app_config", lambda _path: _config())

    code = cli.main(["--json", "--provider", "missing", "-p", "hello"])

    assert code == int(ExitCode.CONFIGURATION)
    _assert_json_failure(
        capsys,
        status="config_error",
        exit_code=int(ExitCode.CONFIGURATION),
        error_code="provider.unknown",
    )


def test_missing_provider_credential_uses_auth_family_and_records_provider(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    env_name = "EVIFORGE_TEST_INTENTIONALLY_MISSING_KEY"
    monkeypatch.delenv(env_name, raising=False)
    config = AppConfig(
        providers=[
            ProviderConfig(
                name="secured",
                protocol="openai-compat",
                base_url="https://llm.example.invalid/v1",
                model="fixture-model",
                api_key_env=env_name,
                auth="required",
            )
        ]
    )
    monkeypatch.setattr(cli, "_configure_logging", lambda _level: None)
    monkeypatch.setattr(cli, "_load_app_config", lambda _path: config)
    monkeypatch.setattr(cli, "_headless_recovery_items", lambda: [])

    code = cli.main(["--json", "-p", "hello"])

    assert code == int(ExitCode.AUTHENTICATION)
    payload = _assert_json_failure(
        capsys,
        status="authentication_failed",
        exit_code=int(ExitCode.AUTHENTICATION),
        error_code="provider.authentication_failed",
    )
    assert payload["provider"] == "secured"
    assert payload["model"] == "fixture-model"


@pytest.mark.parametrize(
    ("error", "status", "exit_code", "error_code"),
    [
        (
            AuthenticationError("credential rejected"),
            "authentication_failed",
            ExitCode.AUTHENTICATION,
            "provider.authentication_failed",
        ),
        (
            NetworkError("connection reset"),
            "network_failed",
            ExitCode.NETWORK,
            "provider.network_failed",
        ),
        (
            PermissionError("grant rejected"),
            "permission_denied",
            ExitCode.PERMISSION,
            "execution.permission_denied",
        ),
        (
            CompletionBlockedError(
                CompletionBlockedEvent(
                    verdict="BLOCKED",
                    reasons=("tests failed",),
                    bundle_ref="evidence://bundle-1",
                )
            ),
            "blocked",
            ExitCode.EVIDENCE_GATE,
            "evidence.gate_blocked",
        ),
        (
            BudgetExceededError("token ceiling reached"),
            "budget_exceeded",
            ExitCode.BUDGET,
            "budget.exceeded",
        ),
        (
            RuntimeError("unexpected invariant"),
            "internal_error",
            ExitCode.INTERNAL,
            "internal.unexpected",
        ),
    ],
)
def test_headless_failure_families_have_stable_snapshots(
    error: Exception,
    status: str,
    exit_code: ExitCode,
    error_code: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def fail(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(cli, "_configure_logging", lambda _level: None)
    monkeypatch.setattr(cli, "_load_app_config", lambda _path: _config())
    monkeypatch.setattr(cli, "_hook_engine", lambda *_args: None)
    monkeypatch.setattr(cli, "_run_prompt", fail)

    code = cli.main(["--json", "-p", "hello"])

    assert code == int(exit_code)
    payload = _assert_json_failure(
        capsys,
        status=status,
        exit_code=int(exit_code),
        error_code=error_code,
    )
    assert payload["error"]["type"] == type(error).__name__
    if isinstance(error, CompletionBlockedError):
        assert payload["verdict"] == "BLOCKED"
        assert payload["evidence_bundle_ref"] == "evidence://bundle-1"


def test_ambiguous_partial_stream_jsonl_keeps_diagnostic_and_recovery_advice(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def fail(*_args, **_kwargs):
        raise AmbiguousStreamError("stream disconnected", emitted_events=2)

    monkeypatch.setattr(cli, "_configure_logging", lambda _level: None)
    monkeypatch.setattr(cli, "_load_app_config", lambda _path: _config())
    monkeypatch.setattr(cli, "_hook_engine", lambda *_args: None)
    monkeypatch.setattr(cli, "_run_prompt", fail)

    code = cli.main(["--jsonl", "-p", "hello"])

    captured = capsys.readouterr()
    assert code == int(ExitCode.NETWORK)
    assert captured.err == ""
    records = [json.loads(line) for line in captured.out.splitlines()]
    for record in records:
        jsonschema.validate(record, SCHEMA)
    assert records[0] == {
        "decision": "blocked_partial_stream",
        "emitted_events": 2,
        "error_code": "provider.partial_stream_ambiguous",
        "schema_version": 1,
        "type": "provider_retry_decision",
    }
    final = records[-1]
    assert final["type"] == "run_result"
    assert final["status"] == "ambiguous"
    assert final["error"]["code"] == "provider.partial_stream_ambiguous"
    assert final["error"]["details"] == {"emitted_events": 2}
    assert "do not replay automatically" in final["error"]["recommendation"].lower()


class _DiagnosticClient(LLMClient):
    async def stream(self, conversation, system="", tools=None):
        yield TextDelta("done")
        yield StreamEnd(stop_reason="end_turn", input_tokens=2, output_tokens=1)

    def drain_diagnostic_events(self):
        return [
            {
                "type": "provider_retry_decision",
                "decision": "retry_scheduled",
                "attempt": 1,
                "next_attempt": 2,
                "delay_seconds": 0.1,
            }
        ]


@pytest.mark.asyncio
async def test_successful_headless_result_drains_provider_retry_decisions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "control"))

    async def no_probe(_provider):
        return None

    monkeypatch.setattr("mewcode.client.resolve_context_window", no_probe)
    config = _config()
    provider = config.providers[0]

    result = await cli._run_prompt_with_client(
        config,
        PermissionMode.DEFAULT,
        None,
        "say done",
        contract_path=None,
        provider=provider,
        client=_DiagnosticClient(),
        background_timeout=0.1,
    )

    assert result.status == "succeeded"
    assert result.events[-1]["type"] == "provider_retry_decision"
    assert result.events[-1]["decision"] == "retry_scheduled"
