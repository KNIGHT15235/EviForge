from __future__ import annotations

from importlib.metadata import version

from mewcode.version import __version__


def test_runtime_version_matches_distribution_metadata() -> None:
    assert __version__ == version("eviforge")
