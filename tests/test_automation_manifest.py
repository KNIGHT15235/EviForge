from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from mewcode.automation_manifest import (
    AutomationManifest,
    AutomationManifestError,
    load_automation_manifest,
)
from mewcode.execution import ExecutionContext


def test_manifest_binds_exact_capabilities_and_stable_hash(tmp_path) -> None:
    manifest = AutomationManifest(
        write_set=("src/app.py",),
        commands=(("python", "-m", "pytest", "-q"),),
        network_hosts=("Packages.Example.",),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    context = ExecutionContext.from_manifest(manifest, task_id="task", cwd=tmp_path)
    assert context.plan_hash.startswith("automation:")
    assert context.write_set == ("src/app.py",)
    assert context.commands == (("python", "-m", "pytest", "-q"),)
    assert context.network_hosts == ("packages.example",)


def test_manifest_rejects_broad_paths_and_expiry(tmp_path) -> None:
    with pytest.raises(ValueError, match="workspace-relative"):
        AutomationManifest(write_set=("../outside",))
    with pytest.raises(ValueError, match="expired"):
        AutomationManifest(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))


def test_manifest_loader_rejects_unknown_fields(tmp_path) -> None:
    path = tmp_path / "grant.json"
    path.write_text(json.dumps({"schema_version": 1, "allow_all": True}))
    with pytest.raises(AutomationManifestError, match="Invalid automation manifest"):
        load_automation_manifest(path)
