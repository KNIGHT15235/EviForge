"""Control-plane path resolution kept outside the repository workspace."""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path


def resolve_control_root(control_root: str | os.PathLike[str] | None = None) -> Path:
    """Resolve the host-owned EviForge control-plane root.

    Tests and embedded runtimes should inject ``control_root``.  Production on
    Windows defaults to ``%LOCALAPPDATA%/EviForge``.  The fallback makes the
    same module usable by headless Linux/macOS evaluators without writing into
    a checked-out repository.
    """

    if control_root is not None:
        return Path(control_root).expanduser().resolve()
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        base = Path(local_app_data)
    else:
        base = Path.home() / ".local" / "share"
    return (base / "EviForge").expanduser().resolve()


def _safe_segment(value: str, *, label: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError(f"{label} must not be empty")
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value) and value != "..":
        return value
    readable = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-.")[:48] or label
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
    return f"{readable}-{digest}"


@dataclass(frozen=True, slots=True)
class ControlPlanePaths:
    control_root: Path
    workspace_id: str
    workspace_root: Path
    state_dir: Path
    database: Path
    traces_dir: Path

    @classmethod
    def build(
        cls,
        *,
        control_root: str | os.PathLike[str] | None = None,
        workspace_id: str = "default",
    ) -> ControlPlanePaths:
        root = resolve_control_root(control_root)
        safe_workspace_id = _safe_segment(workspace_id, label="workspace")
        workspace_root = root / "workspaces" / safe_workspace_id
        state_dir = workspace_root / "state"
        return cls(
            control_root=root,
            workspace_id=safe_workspace_id,
            workspace_root=workspace_root,
            state_dir=state_dir,
            database=state_dir / "runtime.db",
            traces_dir=workspace_root / "traces",
        )

    def ensure_directories(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.traces_dir.mkdir(parents=True, exist_ok=True)

    def trace_jsonl(self, trace_id: str, wall_time: str) -> Path:
        safe_trace_id = _safe_segment(trace_id, label="trace")
        month = wall_time[:7] if re.fullmatch(r"\d{4}-\d{2}.*", wall_time) else "unknown"
        return self.traces_dir / month / f"{safe_trace_id}.jsonl"
