from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any


_TOOL_WORK_DIR: ContextVar[Path | None] = ContextVar(
    "likecc_tool_work_dir",
    default=None,
)

# ``None`` is a meaningful value here: child Agents without a FileHistory must
# not fall back to the history attached to a shared WriteFile/EditFile instance.
# A private sentinel lets direct tool calls retain that backwards-compatible
# fallback while Agent executions can explicitly bind ``None``.
_FILE_HISTORY_UNBOUND = object()
_TOOL_FILE_HISTORY: ContextVar[Any] = ContextVar(
    "likecc_tool_file_history",
    default=_FILE_HISTORY_UNBOUND,
)


def get_tool_work_dir(default: str | Path | None = None) -> Path:
    """Return the task-local directory used to resolve tool paths."""
    current = _TOOL_WORK_DIR.get()
    if current is not None:
        return current
    if default is not None:
        return Path(default).expanduser().resolve()
    return Path.cwd()


def resolve_tool_path(path: str | Path, default: str | Path | None = None) -> Path:
    """Resolve a tool path against its Agent's task-local working directory."""
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = get_tool_work_dir(default) / candidate
    return candidate.resolve(strict=False)


def get_tool_file_history(default: Any = None) -> Any:
    """Return the task-local FileHistory, or ``default`` when unbound.

    An explicitly bound ``None`` is returned as-is so an Agent without file
    history cannot accidentally write into a shared tool instance's history.
    """
    current = _TOOL_FILE_HISTORY.get()
    if current is _FILE_HISTORY_UNBOUND:
        return default
    return current


def is_safe_relative_pattern(pattern: str) -> bool:
    """Reject glob patterns that can escape their explicitly checked base path."""
    posix_pattern = PurePosixPath(pattern.replace("\\", "/"))
    windows_pattern = PureWindowsPath(pattern)
    return (
        not posix_pattern.is_absolute()
        and not windows_pattern.is_absolute()
        and ".." not in posix_pattern.parts
    )


def is_path_within(path: Path, root: Path) -> bool:
    """Return whether a resolved match stays inside its resolved search root."""
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except (OSError, RuntimeError, ValueError):
        return False


@contextmanager
def tool_working_directory(
    work_dir: str | Path,
    *,
    file_history: Any = _FILE_HISTORY_UNBOUND,
) -> Iterator[None]:
    """Bind Agent-local tool state for the current asynchronous task."""
    resolved = Path(work_dir).expanduser().resolve()
    work_dir_token = _TOOL_WORK_DIR.set(resolved)
    history_token = None
    if file_history is not _FILE_HISTORY_UNBOUND:
        history_token = _TOOL_FILE_HISTORY.set(file_history)
    try:
        yield
    finally:
        if history_token is not None:
            _TOOL_FILE_HISTORY.reset(history_token)
        _TOOL_WORK_DIR.reset(work_dir_token)
