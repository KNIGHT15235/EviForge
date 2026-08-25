from __future__ import annotations

import json
from datetime import datetime, timezone

from mewcode.memory.session import SessionMeta


def test_old_session_meta_loads_without_evidence_fields(tmp_path):
    path = tmp_path / "legacy.meta"
    now = datetime.now(timezone.utc).isoformat()
    path.write_text(
        json.dumps({"id": "legacy", "created_at": now, "last_active": now}),
        encoding="utf-8",
    )

    loaded = SessionMeta.load(path)

    assert loaded is not None
    assert loaded.task_id == ""
    assert loaded.evidence_bundle_ref == ""


def test_session_meta_round_trips_evidence_reference(tmp_path):
    path = tmp_path / "current.meta"
    meta = SessionMeta(
        id="current",
        task_id="task-123",
        evidence_bundle_ref="sha256:abc",
    )

    meta.save(path)
    loaded = SessionMeta.load(path)

    assert loaded is not None
    assert loaded.task_id == "task-123"
    assert loaded.evidence_bundle_ref == "sha256:abc"
