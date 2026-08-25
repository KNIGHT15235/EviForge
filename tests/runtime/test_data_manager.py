from __future__ import annotations

import json
import logging
import os
import zipfile
from datetime import datetime, timedelta, timezone

from mewcode.logging_config import configure_logging
from mewcode.runtime.data_manager import RuntimeDataManager


def test_stats_export_redacts_text_and_excludes_database(tmp_path) -> None:
    root = tmp_path / "control"
    trace = root / "workspaces" / "demo" / "traces" / "trace.jsonl"
    trace.parent.mkdir(parents=True)
    trace.write_text('Authorization: Bearer super-secret-token\napi_key=abc123456\n')
    database = root / "workspaces" / "demo" / "state" / "runtime.db"
    database.parent.mkdir(parents=True)
    database.write_bytes(b"sqlite bytes")

    manager = RuntimeDataManager(root)
    stats = manager.stats()
    assert stats.file_count == 2
    target = manager.export_zip(tmp_path / "export.zip")

    with zipfile.ZipFile(target) as archive:
        names = archive.namelist()
        assert "workspaces/demo/traces/trace.jsonl" in names
        assert "workspaces/demo/state/runtime.db" not in names
        content = archive.read("workspaces/demo/traces/trace.jsonl").decode()
        assert "super-secret-token" not in content
        assert "abc123456" not in content
        manifest = json.loads(archive.read("export-manifest.json"))
        assert manifest["schema_version"] == 2
        assert manifest["policy"]["mode"] == "explicit_safe_allowlist"
        assert manifest["policy"]["database_sensitivity"] == "excluded"
        assert manifest["source_root"] == "[LOCAL_CONTROL_ROOT]"
        assert any(
            item["path"] == "workspaces/demo/state/runtime.db"
            and item["reason"] == "database_requires_explicit_opt_in"
            for item in manifest["excluded"]
        )


def test_support_export_allowlist_prevents_source_cas_and_binary_leaks(tmp_path) -> None:
    root = tmp_path / "control"
    sentinels = {
        "python": "SENTINEL_PY_SOURCE_2819",
        "javascript": "SENTINEL_JS_SOURCE_9182",
        "extensionless": "SENTINEL_EXTENSIONLESS_7744",
        "cas": "SENTINEL_CAS_BLOB_5533",
        "binary": "SENTINEL_BINARY_4401",
    }
    unsafe_files = {
        root / "unexpected" / "payload.py": sentinels["python"].encode(),
        root / "unexpected" / "payload.js": sentinels["javascript"].encode(),
        root / "unexpected" / "README": sentinels["extensionless"].encode(),
        root / "workspaces" / "demo" / "artifacts" / "dag" / "sha256blob": sentinels["cas"].encode(),
        root / "unexpected" / "payload.bin": b"\x00\xff" + sentinels["binary"].encode(),
    }
    for path, payload in unsafe_files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    safe_trace = root / "workspaces" / "demo" / "traces" / "trace.jsonl"
    safe_trace.parent.mkdir(parents=True, exist_ok=True)
    safe_trace.write_text(
        '{"event":"failed","api_key":"sk-test-secret-value-12345"}\n',
        encoding="utf-8",
    )

    target = RuntimeDataManager(root).export_zip(tmp_path / "support.zip")

    with zipfile.ZipFile(target) as archive:
        names = archive.namelist()
        assert names == ["workspaces/demo/traces/trace.jsonl", "export-manifest.json"]
        bundle_bytes = b"".join(archive.read(name) for name in names)
        for sentinel in sentinels.values():
            assert sentinel.encode() not in bundle_bytes
        assert b"sk-test-secret-value-12345" not in bundle_bytes
        manifest = json.loads(archive.read("export-manifest.json"))

    excluded = {item["path"]: item for item in manifest["excluded"]}
    assert excluded["unexpected/payload.py"]["reason"] == "source_code_excluded"
    assert excluded["unexpected/payload.js"]["reason"] == "source_code_excluded"
    assert excluded["unexpected/README"]["reason"] == "cas_or_extensionless_content_excluded"
    assert excluded["workspaces/demo/artifacts/dag/sha256blob"]["category"] == "cas_artifacts"
    assert excluded["unexpected/payload.bin"]["reason"] == "path_not_in_safe_export_allowlist"
    assert manifest["included"][0]["redacted"] is True


def test_database_export_requires_opt_in_and_marks_high_sensitivity(tmp_path) -> None:
    root = tmp_path / "control"
    database = root / "workspaces" / "demo" / "state" / "runtime.db"
    database.parent.mkdir(parents=True)
    database.write_bytes(b"sqlite-sensitive-record")

    target = RuntimeDataManager(root).export_zip(
        tmp_path / "support-with-db.zip", include_databases=True
    )

    with zipfile.ZipFile(target) as archive:
        assert archive.read("workspaces/demo/state/runtime.db") == b"sqlite-sensitive-record"
        manifest = json.loads(archive.read("export-manifest.json"))
    assert manifest["policy"]["include_databases"] is True
    assert manifest["policy"]["database_sensitivity"] == "high_raw_database_explicit_opt_in"
    assert manifest["included"][0]["sensitivity"] == "high_raw_database_explicit_opt_in"
    assert manifest["included"][0]["redacted"] is False


def test_stats_report_categories_limits_and_retention_policy(tmp_path) -> None:
    root = tmp_path / "control"
    log = root / "logs" / "eviforge.log"
    trace = root / "workspaces" / "demo" / "traces" / "trace.jsonl"
    log.parent.mkdir(parents=True)
    trace.parent.mkdir(parents=True)
    log.write_text("log", encoding="utf-8")
    trace.write_text("trace", encoding="utf-8")

    stats = RuntimeDataManager(root).stats()

    assert stats.categories["logs"] == {"files": 1, "bytes": 3}
    assert stats.categories["traces"] == {"files": 1, "bytes": 5}
    assert stats.limits["runtime_total_quota_enforced"] is False
    assert stats.limits["default_log_rotation_max_bytes"] == 8_000_000
    assert stats.retention_policy["prune_default"] == "dry_run"
    assert "memory" in stats.retention_policy["protected"]


def test_prune_defaults_to_dry_run(tmp_path) -> None:
    root = tmp_path / "control"
    old = root / "logs" / "old.log"
    old.parent.mkdir(parents=True)
    old.write_text("old")
    timestamp = (datetime.now(timezone.utc) - timedelta(days=40)).timestamp()
    os.utime(old, (timestamp, timestamp))
    manager = RuntimeDataManager(root)

    candidates = manager.prune(older_than_days=30)
    assert candidates == (old.resolve(),)
    assert old.exists()
    manager.prune(older_than_days=30, dry_run=False)
    assert not old.exists()


def test_prune_never_selects_source_sessions_memory_or_database_by_default(tmp_path) -> None:
    root = tmp_path / "control"
    paths = {
        "log": root / "logs" / "old.log",
        "trace": root / "workspaces" / "demo" / "traces" / "old.jsonl",
        "source": root / "unexpected" / "old.py",
        "session": root / "sessions" / "old.json",
        "memory": root / "memory" / "old.md",
        "database": root / "workspaces" / "demo" / "state" / "runtime.db",
    }
    timestamp = (datetime.now(timezone.utc) - timedelta(days=40)).timestamp()
    for path in paths.values():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("old", encoding="utf-8")
        os.utime(path, (timestamp, timestamp))

    manager = RuntimeDataManager(root)
    candidates = manager.prune(older_than_days=30)

    assert candidates == (paths["log"].resolve(), paths["trace"].resolve())
    manager.prune(older_than_days=30, dry_run=False)
    assert not paths["log"].exists()
    assert not paths["trace"].exists()
    assert all(paths[name].exists() for name in ("source", "session", "memory", "database"))


def test_recent_error_code_reads_only_explicit_structured_field(tmp_path) -> None:
    root = tmp_path / "control"
    log = root / "logs" / "eviforge.log"
    log.parent.mkdir(parents=True)
    log.write_text(
        "ERROR provider network failure\n"
        '{"message":"request failed","error_code":"provider.timeout"}\n',
        encoding="utf-8",
    )
    manager = RuntimeDataManager(root)
    assert manager.recent_error_code() == "provider.timeout"

    log.write_text("ERROR provider network failure\n", encoding="utf-8")
    assert manager.recent_error_code() is None


def test_rotating_log_redacts_secrets(tmp_path) -> None:
    path = configure_logging(control_root=tmp_path, max_bytes=1024, backup_count=1)
    logging.getLogger("test").warning("Authorization: Bearer secret-token-123")
    for handler in logging.getLogger().handlers:
        handler.flush()
    content = path.read_text(encoding="utf-8")
    assert "secret-token-123" not in content
    assert "[REDACTED]" in content
