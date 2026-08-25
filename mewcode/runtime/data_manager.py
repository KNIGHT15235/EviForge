from __future__ import annotations

import json
import os
import re
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mewcode.runtime.paths import resolve_control_root


# These mirror the defaults in ``logging_config``.  They are reported as
# defaults, rather than as a global quota, because embedding hosts may override
# the rotating logger settings.
DEFAULT_LOG_FILE_MAX_BYTES = 2_000_000
DEFAULT_LOG_BACKUP_COUNT = 3
DEFAULT_SAFE_TEXT_MAX_BYTES = 5_000_000

_DATABASE_SUFFIXES = frozenset({".db", ".sqlite", ".sqlite3"})
_SAFE_TRACE_SUFFIXES = frozenset({".json", ".jsonl", ".log", ".txt"})
_LOG_FILE_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_.-]*\.log(?:\.\d+)?$", re.IGNORECASE
)
_STRUCTURED_ERROR_CODE_RE = re.compile(
    r'(?:"error_code"\s*:\s*"|\berror_code\s*=\s*)'
    r"([A-Za-z][A-Za-z0-9_.:-]{0,127})",
    re.IGNORECASE,
)

# Every exported text member goes through this redactor.  The patterns cover
# HTTP headers, JSON/YAML/ENV assignments, CLI flags, private-key blocks, and
# common opaque token shapes.
_HEADER_SECRET_RE = re.compile(
    r"(?i)(\b(?:authorization|proxy-authorization|cookie|set-cookie)\s*:\s*)[^\r\n]+"
)
_QUOTED_SECRET_RE = re.compile(
    r'''(?ix)
    (?P<prefix>["'](?:authorization|proxy[_-]?authorization|api[_-]?key|token|
        access[_-]?token|refresh[_-]?token|id[_-]?token|session[_-]?token|
        client[_-]?secret|secret|password|passwd|passphrase|credential|cookie|
        private[_-]?key|connection[_-]?string)["']\s*:\s*)
    (?P<quote>["'])(?P<value>.*?)(?P=quote)
    '''
)
_ASSIGNMENT_SECRET_RE = re.compile(
    r'''(?ix)
    (?P<prefix>\b(?:[A-Za-z0-9]+_)*(?:api[_-]?key|token|access[_-]?token|
        refresh[_-]?token|id[_-]?token|session[_-]?token|client[_-]?secret|
        secret|password|passwd|passphrase|credential|private[_-]?key|
        connection[_-]?string)\b\s*[:=]\s*)
    (?P<quote>["']?)(?P<value>[^\s,"'}\]]+)(?P=quote)
    '''
)
_CLI_SECRET_RE = re.compile(
    r"(?i)(--(?:api-key|token|password|secret)(?:=|\s+))([^\s]+)"
)
_BEARER_RE = re.compile(r"(?i)(\bbearer\s+)[A-Za-z0-9._~+/=-]{6,}")
_KNOWN_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:sk-[A-Za-z0-9_-]{12,}|github_pat_[A-Za-z0-9_]{12,}|"
    r"gh[pousr]_[A-Za-z0-9]{12,}|xox[baprs]-[A-Za-z0-9-]{12,}|"
    r"AKIA[A-Z0-9]{12,})(?![A-Za-z0-9])"
)
_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [^-\r\n]*PRIVATE KEY-----.*?-----END [^-\r\n]*PRIVATE KEY-----",
    re.DOTALL,
)


def _redact_quoted(match: re.Match[str]) -> str:
    quote = match.group("quote")
    return f'{match.group("prefix")}{quote}[REDACTED]{quote}'


def redact_text(value: str) -> str:
    """Remove credential-shaped values without exposing the original value."""

    value = _PRIVATE_KEY_RE.sub("[REDACTED PRIVATE KEY]", value)
    value = _HEADER_SECRET_RE.sub(r"\1[REDACTED]", value)
    value = _QUOTED_SECRET_RE.sub(_redact_quoted, value)
    value = _ASSIGNMENT_SECRET_RE.sub(_redact_quoted, value)
    value = _CLI_SECRET_RE.sub(r"\1[REDACTED]", value)
    value = _BEARER_RE.sub(r"\1[REDACTED]", value)
    return _KNOWN_TOKEN_RE.sub("[REDACTED]", value)


@dataclass(frozen=True, slots=True)
class DataStats:
    root: Path
    file_count: int
    total_bytes: int
    categories: dict[str, dict[str, int]]
    limits: dict[str, object] = field(default_factory=dict)
    retention_policy: dict[str, object] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {
            "root": str(self.root),
            "file_count": self.file_count,
            "total_bytes": self.total_bytes,
            "categories": self.categories,
            "limits": self.limits,
            "retention_policy": self.retention_policy,
        }


@dataclass(frozen=True, slots=True)
class _ExportDecision:
    category: str
    include: bool
    reason: str
    sensitivity: str = "normal"
    redact: bool = False


class RuntimeDataManager:
    def __init__(self, control_root: str | os.PathLike[str] | None = None) -> None:
        self.root = resolve_control_root(control_root)

    def _files(self) -> list[Path]:
        if not self.root.is_dir():
            return []
        root = self.root.resolve()
        files: list[Path] = []
        for path in self.root.rglob("*"):
            if path.is_symlink() or not path.is_file():
                continue
            resolved = path.resolve()
            try:
                resolved.relative_to(root)
            except ValueError:
                continue
            files.append(resolved)
        return sorted(files)

    @staticmethod
    def _category(relative: Path) -> str:
        parts = tuple(part.casefold() for part in relative.parts)
        suffix = relative.suffix.casefold()
        # Session and Memory remain protected even if a nested filename looks
        # like a database, trace, or artifact.
        if "session" in parts or "sessions" in parts:
            return "sessions"
        if "memory" in parts or "memories" in parts:
            return "memory"
        if suffix in _DATABASE_SUFFIXES:
            return "databases"
        if parts and parts[0] == "logs":
            return "logs"
        if "traces" in parts:
            return "traces"
        if "artifacts" in parts or "cas" in parts or "sha256" in parts:
            return "cas_artifacts"
        if "dag" in parts and "runs" in parts:
            return "dag_runs"
        if "evidence" in parts:
            return "evidence"
        if parts and parts[0] == "workspaces":
            return "workspace_state"
        return "other"

    @staticmethod
    def _export_decision(relative: Path, *, include_databases: bool) -> _ExportDecision:
        parts = tuple(part.casefold() for part in relative.parts)
        suffix = relative.suffix.casefold()
        category = RuntimeDataManager._category(relative)

        if suffix in _DATABASE_SUFFIXES:
            if include_databases:
                return _ExportDecision(
                    category,
                    True,
                    "database_explicitly_included",
                    sensitivity="high_raw_database_explicit_opt_in",
                    redact=False,
                )
            return _ExportDecision(category, False, "database_requires_explicit_opt_in")

        if parts and parts[0] == "logs" and _LOG_FILE_RE.fullmatch(relative.name):
            return _ExportDecision("logs", True, "safe_runtime_log", redact=True)

        if (
            len(parts) >= 4
            and parts[0] == "workspaces"
            and parts[2] == "traces"
            and suffix in _SAFE_TRACE_SUFFIXES
        ):
            return _ExportDecision("traces", True, "safe_runtime_trace_text", redact=True)

        if (
            len(parts) >= 5
            and parts[0] == "workspaces"
            and parts[2] == "dag"
            and parts[3] == "runs"
            and suffix == ".json"
        ):
            return _ExportDecision("dag_runs", True, "safe_dag_run_metadata", redact=True)

        if suffix in {".py", ".pyw", ".js", ".jsx", ".ts", ".tsx"}:
            return _ExportDecision(category, False, "source_code_excluded")
        if category == "cas_artifacts" or not suffix:
            return _ExportDecision(category, False, "cas_or_extensionless_content_excluded")
        return _ExportDecision(category, False, "path_not_in_safe_export_allowlist")

    def stats(self) -> DataStats:
        categories: dict[str, dict[str, int]] = {}
        total = 0
        files = self._files()
        for path in files:
            relative = path.relative_to(self.root)
            category = self._category(relative)
            size = path.stat().st_size
            total += size
            entry = categories.setdefault(category, {"files": 0, "bytes": 0})
            entry["files"] += 1
            entry["bytes"] += size
        limits: dict[str, object] = {
            "runtime_total_quota_bytes": None,
            "runtime_total_quota_enforced": False,
            "default_log_file_max_bytes": DEFAULT_LOG_FILE_MAX_BYTES,
            "default_log_backup_count": DEFAULT_LOG_BACKUP_COUNT,
            "default_log_rotation_max_bytes": DEFAULT_LOG_FILE_MAX_BYTES
            * (DEFAULT_LOG_BACKUP_COUNT + 1),
            "support_export_text_member_max_bytes": DEFAULT_SAFE_TEXT_MAX_BYTES,
        }
        retention_policy: dict[str, object] = {
            "automatic_prune": False,
            "prune_default": "dry_run",
            "eligible_by_default": ["logs", "traces"],
            "protected": ["project_source", "sessions", "memory", "databases"],
            "database_prune_requires_explicit_opt_in": True,
        }
        return DataStats(
            self.root,
            len(files),
            total,
            categories,
            limits,
            retention_policy,
        )

    def recent_error_code(self, *, max_bytes_per_log: int = 262_144) -> str | None:
        """Return only an explicitly structured code, never inferred log prose."""

        if max_bytes_per_log < 1:
            raise ValueError("max_bytes_per_log must be positive")
        log_dir = self.root / "logs"
        logs = [
            path
            for path in self._files()
            if path.parent == log_dir and _LOG_FILE_RE.fullmatch(path.name)
        ]
        for path in sorted(logs, key=lambda item: item.stat().st_mtime, reverse=True):
            try:
                with path.open("rb") as stream:
                    size = path.stat().st_size
                    stream.seek(max(0, size - max_bytes_per_log))
                    text = stream.read(max_bytes_per_log).decode("utf-8", errors="ignore")
            except OSError:
                continue
            for line in reversed(text.splitlines()):
                match = _STRUCTURED_ERROR_CODE_RE.search(line)
                if match:
                    return match.group(1)
        return None

    def export_zip(
        self,
        destination: str | os.PathLike[str],
        *,
        include_databases: bool = False,
    ) -> Path:
        target = Path(destination).expanduser().resolve(strict=False)
        try:
            target.relative_to(self.root.resolve(strict=False))
        except ValueError:
            pass
        else:
            raise ValueError("data export destination must be outside the control root")
        target.parent.mkdir(parents=True, exist_ok=True)
        included: list[dict[str, object]] = []
        excluded: list[dict[str, object]] = []
        manifest: dict[str, object] = {
            "schema_version": 2,
            "exported_at": datetime.now(timezone.utc).isoformat(),
            # Do not disclose the local account path in a support bundle.
            "source_root": "[LOCAL_CONTROL_ROOT]",
            "policy": {
                "mode": "explicit_safe_allowlist",
                "all_text_members_redacted": True,
                "include_databases": include_databases,
                "database_sensitivity": (
                    "high_raw_database_explicit_opt_in" if include_databases else "excluded"
                ),
                "safe_text_member_max_bytes": DEFAULT_SAFE_TEXT_MAX_BYTES,
            },
            "included": included,
            "excluded": excluded,
            # Compatibility aliases for callers of the schema-v1 preview.
            "files": included,
            "skipped": excluded,
        }
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in self._files():
                relative = path.relative_to(self.root)
                decision = self._export_decision(
                    relative, include_databases=include_databases
                )
                source_bytes = path.stat().st_size
                if not decision.include:
                    excluded.append(
                        {
                            "path": relative.as_posix(),
                            "category": decision.category,
                            "reason": decision.reason,
                            "source_bytes": source_bytes,
                        }
                    )
                    continue
                if decision.redact and source_bytes > DEFAULT_SAFE_TEXT_MAX_BYTES:
                    excluded.append(
                        {
                            "path": relative.as_posix(),
                            "category": decision.category,
                            "reason": "safe_text_member_exceeds_size_limit",
                            "source_bytes": source_bytes,
                        }
                    )
                    continue
                if decision.redact:
                    try:
                        content = redact_text(path.read_text(encoding="utf-8"))
                    except (OSError, UnicodeDecodeError):
                        excluded.append(
                            {
                                "path": relative.as_posix(),
                                "category": decision.category,
                                "reason": "safe_text_member_is_not_readable_utf8",
                                "source_bytes": source_bytes,
                            }
                        )
                        continue
                    payload = content.encode("utf-8")
                    archive.writestr(relative.as_posix(), payload)
                else:
                    archive.write(path, relative.as_posix())
                    payload = b""
                included.append(
                    {
                        "path": relative.as_posix(),
                        "category": decision.category,
                        "reason": decision.reason,
                        "source_bytes": source_bytes,
                        "archive_bytes": len(payload) if decision.redact else source_bytes,
                        "redacted": decision.redact,
                        "sensitivity": decision.sensitivity,
                    }
                )
            archive.writestr(
                "export-manifest.json",
                json.dumps(manifest, ensure_ascii=False, indent=2),
            )
        return target

    def prune(
        self,
        *,
        older_than_days: int,
        dry_run: bool = True,
        include_databases: bool = False,
    ) -> tuple[Path, ...]:
        if older_than_days < 1:
            raise ValueError("retention must be at least one day")
        cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)
        candidates: list[Path] = []
        for path in self._files():
            relative = path.relative_to(self.root)
            category = self._category(relative)
            export_decision = self._export_decision(
                relative, include_databases=False
            )
            eligible = (
                category in {"logs", "traces"}
                and export_decision.include
                and export_decision.redact
            )
            if include_databases and category == "databases":
                eligible = True
            if not eligible:
                continue
            modified = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            if modified < cutoff:
                candidates.append(path)
        result = tuple(candidates)
        if dry_run:
            return result
        for path in result:
            path.unlink()
        # Remove only now-empty descendants, never the control root itself.
        if self.root.is_dir():
            for directory in sorted(
                (path for path in self.root.rglob("*") if path.is_dir()),
                key=lambda item: len(item.parts),
                reverse=True,
            ):
                try:
                    directory.rmdir()
                except OSError:
                    pass
        return result


__all__ = [
    "DEFAULT_LOG_BACKUP_COUNT",
    "DEFAULT_LOG_FILE_MAX_BYTES",
    "DEFAULT_SAFE_TEXT_MAX_BYTES",
    "DataStats",
    "RuntimeDataManager",
    "redact_text",
]
