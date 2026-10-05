"""Remote side effects are reconciled by an operator, never automatically replayed."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path


def unresolved_writes(directory: Path, server: str | None = None) -> list[str]:
    path = directory / "events.jsonl"
    if not path.exists():
        return []
    calls: dict[str, dict] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            call_id = record["call_id"]
            if record["event"] == "call_started":
                calls[call_id] = record
            elif record["event"] == "call_finished" and call_id in calls:
                calls[call_id]["status"] = record["status"]
            elif record["event"] == "call_reconciled" and call_id in calls:
                calls[call_id]["status"] = "reconciled"
    except (ValueError, KeyError):
        raise ValueError("MCP journal is corrupt; remote writes are blocked")
    return [call_id for call_id, record in calls.items() if record.get("write") and record.get("status") in {None, "ambiguous", "completed_unarchived"} and (server is None or record["server"] == server)]


def reconcile(directory: Path, call_id: str, outcome: str, evidence: str) -> None:
    if not re.fullmatch(r"[a-f0-9]{32}", call_id) or call_id not in unresolved_writes(directory):
        raise ValueError("Call is not an unresolved remote write")
    if outcome not in {"executed", "not_executed"} or not evidence.strip():
        raise ValueError("Reconciliation requires a remote read-back receipt and explicit outcome")
    with (directory / "events.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"schema_version": 1, "event": "call_reconciled", "timestamp": time.time(), "call_id": call_id, "outcome": outcome, "evidence": evidence}, ensure_ascii=False) + "\n")
