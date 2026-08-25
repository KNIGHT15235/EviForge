from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version


def get_version() -> str:
    """Return the installed distribution version with a source-tree fallback."""

    try:
        return version("eviforge")
    except PackageNotFoundError:
        # Editable/source-only test runs can import the package before it has
        # distribution metadata.  Keep the fallback in one place so every UI
        # surface still agrees.
        return "0.4.0+dev"


__version__ = get_version()


__all__ = ["__version__", "get_version"]
