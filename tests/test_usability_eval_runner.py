from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUNNER_PATH = PROJECT_ROOT / "scripts" / "run-usability-eval.py"


def _load_runner():
    module_name = "eviforge_usability_eval_runner_test"
    spec = importlib.util.spec_from_file_location(module_name, RUNNER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_absolute_executable_returns_an_absolute_path(tmp_path: Path) -> None:
    runner = _load_runner()
    executable = tmp_path / "python"

    assert Path(runner._absolute_executable(str(executable))).is_absolute()


@pytest.mark.skipif(os.name == "nt", reason="Windows symlinks require optional privileges")
def test_absolute_executable_preserves_virtualenv_symlink(tmp_path: Path) -> None:
    runner = _load_runner()
    base_python = tmp_path / "base" / "python"
    base_python.parent.mkdir()
    base_python.touch()
    venv_python = tmp_path / ".venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.symlink_to(base_python)

    selected = Path(runner._absolute_executable(str(venv_python)))

    assert selected == venv_python.absolute()
    assert selected != base_python.resolve()
