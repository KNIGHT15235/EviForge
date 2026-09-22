"""Local append-only application audit. Not tamper-proof against the OS user."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class PlanAudit:
    def __init__(self, path: Path) -> None:
        self.path = path

    def append(self, event: str, *, timestamp: float, **fields: Any) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as output:
            output.write(json.dumps({"schema_version": 1, "event": event, "timestamp": timestamp, **fields}, sort_keys=True, ensure_ascii=False) + "\n")
            output.flush()
