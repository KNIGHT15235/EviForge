from __future__ import annotations

from pathlib import Path

from mewcode.runtime import ControlPlanePaths, resolve_control_root


def test_control_root_can_be_injected(tmp_path: Path) -> None:
    paths = ControlPlanePaths.build(control_root=tmp_path, workspace_id="repo-one")

    assert paths.control_root == tmp_path.resolve()
    assert paths.database == tmp_path / "workspaces" / "repo-one" / "state" / "runtime.db"


def test_control_root_defaults_to_local_app_data(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    assert resolve_control_root() == (tmp_path / "EviForge").resolve()


def test_unsafe_workspace_id_cannot_escape_control_root(tmp_path: Path) -> None:
    paths = ControlPlanePaths.build(control_root=tmp_path, workspace_id="../../outside")

    assert paths.workspace_root.is_relative_to(tmp_path.resolve())
    assert ".." not in paths.workspace_id
