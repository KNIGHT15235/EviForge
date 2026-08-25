from __future__ import annotations

from pathlib import Path

from mewcode.config import AppConfig, MCPServerConfig, ProviderConfig
from mewcode.diagnostics import check_config, run_doctor


def test_config_check_is_offline_redacted_and_actionable(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("THIRD_PARTY_KEY", "sentinel-secret")
    config = AppConfig(
        providers=[
            ProviderConfig(
                name="third-party",
                protocol="openai-compat",
                base_url="https://llm.example/v1",
                model="your-model",
                api_key_env="THIRD_PARTY_KEY",
            )
        ],
        mcp_servers=[MCPServerConfig(name="missing", command="definitely-not-installed")],
        config_sources=(tmp_path / "config.yaml",),
    )

    report = check_config(config)
    rendered = str(report.as_dict())

    assert report.ok is False
    assert "provider.third-party.model" in rendered
    assert "mcp.missing.command" in rendered
    assert "THIRD_PARTY_KEY" in rendered
    assert "sentinel-secret" not in rendered


def test_doctor_does_not_create_workspace_files(tmp_path: Path) -> None:
    before = list(tmp_path.iterdir())
    report = run_doctor(work_dir=tmp_path)
    after = list(tmp_path.iterdir())
    assert before == after == []
    assert any(item.check_id == "runtime.python" for item in report.items)


def test_config_check_reports_missing_env_reference_without_leaking_value(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("EVIFORGE_MISSING_TOKEN", raising=False)
    config = AppConfig(
        providers=[
            ProviderConfig(
                name="local",
                protocol="openai-compat",
                base_url="http://127.0.0.1:9999/v1",
                model="local",
                auth="none",
            )
        ],
        mcp_servers=[
            MCPServerConfig(
                name="remote",
                url="https://mcp.example.test",
                headers={
                    "Authorization": "Bearer literal-prefix-${EVIFORGE_MISSING_TOKEN}"
                },
            )
        ],
        config_sources=(tmp_path / "config.yaml",),
    )

    report = check_config(config)
    item = next(i for i in report.items if i.check_id == "config.environment")
    rendered = str(item.as_dict())

    assert item.status == "error"
    assert "EVIFORGE_MISSING_TOKEN" in rendered
    assert "literal-prefix" not in rendered
    assert "Set the named environment variable" in rendered


def test_doctor_reports_real_limits_and_unknown_error_code(monkeypatch, tmp_path: Path) -> None:
    local_data = tmp_path / "local-data"
    monkeypatch.setenv("LOCALAPPDATA", str(local_data))

    report = run_doctor(work_dir=tmp_path)

    by_id = {item.check_id: item for item in report.items}
    limits = by_id["runtime.data_limits"]
    assert limits.status == "warning"
    assert limits.details["limits"]["runtime_total_quota_enforced"] is False
    assert limits.details["retention_policy"]["prune_default"] == "dry_run"
    recent = by_id["runtime.last_error_code"]
    assert recent.message.endswith("unknown")
    assert recent.details == {
        "error_code": None,
        "source": "unknown",
        "inferred": False,
    }


def test_doctor_reports_only_structured_recent_error_code(monkeypatch, tmp_path: Path) -> None:
    local_data = tmp_path / "local-data"
    monkeypatch.setenv("LOCALAPPDATA", str(local_data))
    log = local_data / "EviForge" / "logs" / "eviforge.log"
    log.parent.mkdir(parents=True)
    log.write_text(
        "ERROR looks like a timeout but must not be guessed\n"
        "error_code=hook.command_timeout request failed\n",
        encoding="utf-8",
    )

    report = run_doctor(work_dir=tmp_path)

    recent = next(item for item in report.items if item.check_id == "runtime.last_error_code")
    assert recent.status == "ok"
    assert recent.details["error_code"] == "hook.command_timeout"
    assert recent.details["inferred"] is False
