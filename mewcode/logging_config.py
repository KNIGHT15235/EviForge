from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from mewcode.runtime.data_manager import redact_text
from mewcode.runtime.paths import resolve_control_root


class SecretRedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # Render first, then replace args so downstream formatters cannot
        # accidentally reconstruct the unredacted message.
        record.msg = redact_text(record.getMessage())
        record.args = ()
        return True


def configure_logging(
    *,
    level: str = "INFO",
    control_root: str | Path | None = None,
    max_bytes: int = 2_000_000,
    backup_count: int = 3,
) -> Path:
    root = resolve_control_root(control_root)
    log_dir = root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / "eviforge.log"
    handler = RotatingFileHandler(
        path,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    handler.addFilter(SecretRedactionFilter())
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    for existing in tuple(root_logger.handlers):
        if isinstance(existing, RotatingFileHandler):
            root_logger.removeHandler(existing)
            existing.close()
    root_logger.addHandler(handler)
    return path


__all__ = ["SecretRedactionFilter", "configure_logging"]
