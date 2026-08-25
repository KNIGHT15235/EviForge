from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class AutomationManifestError(ValueError):
    pass


class AutomationManifest(BaseModel):
    """Reviewed, exact capabilities for one non-interactive invocation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    description: str = ""
    write_set: tuple[str, ...] = ()
    commands: tuple[tuple[str, ...], ...] = ()
    network_hosts: tuple[str, ...] = ()
    expires_at: datetime | None = None

    @field_validator("write_set")
    @classmethod
    def _validate_write_set(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized: list[str] = []
        for value in values:
            path = PurePosixPath(value.replace("\\", "/"))
            if path.is_absolute() or ".." in path.parts or str(path) in {"", "."}:
                raise ValueError("write_set entries must be non-root workspace-relative paths")
            normalized.append(path.as_posix())
        if len(set(normalized)) != len(normalized):
            raise ValueError("write_set contains duplicates")
        return tuple(normalized)

    @field_validator("commands")
    @classmethod
    def _validate_commands(
        cls, values: tuple[tuple[str, ...], ...]
    ) -> tuple[tuple[str, ...], ...]:
        for argv in values:
            if not argv or any(not item or "\x00" in item for item in argv):
                raise ValueError("commands must contain non-empty exact argv arrays")
        return values

    @field_validator("network_hosts")
    @classmethod
    def _validate_hosts(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(value.casefold().rstrip(".") for value in values)
        if any(not host or "/" in host or "://" in host for host in normalized):
            raise ValueError("network_hosts must contain host names, not URLs")
        return normalized

    @model_validator(mode="after")
    def _ensure_active(self) -> "AutomationManifest":
        if self.expires_at is not None:
            expires = self.expires_at
            if expires.tzinfo is None:
                raise ValueError("expires_at must include a timezone")
            if expires.astimezone(timezone.utc) <= datetime.now(timezone.utc):
                raise ValueError("automation manifest has expired")
        return self

    @property
    def plan_hash(self) -> str:
        payload = self.model_dump(mode="json", exclude_none=True)
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return "automation:" + hashlib.sha256(encoded).hexdigest()


def load_automation_manifest(path: str | Path) -> AutomationManifest:
    source = Path(path).expanduser().resolve(strict=True)
    try:
        return AutomationManifest.model_validate_json(source.read_text(encoding="utf-8"))
    except Exception as exc:
        raise AutomationManifestError(f"Invalid automation manifest {source}: {exc}") from exc


__all__ = ["AutomationManifest", "AutomationManifestError", "load_automation_manifest"]
