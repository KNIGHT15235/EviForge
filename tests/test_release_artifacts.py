from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "ci.yml"
EVAL_RUNNER = PROJECT_ROOT / "scripts" / "run-usability-eval.py"


def test_cross_platform_release_acceptance_workflow_is_complete() -> None:
    assert WORKFLOW.is_file()
    text = WORKFLOW.read_text(encoding="utf-8")
    required_fragments = (
        "ubuntu-latest",
        "windows-latest",
        "uv sync --frozen --dev",
        "python -m compileall -q mewcode scripts",
        "uv run pytest -q",
        "uv run eviforge --help",
        "uv run eviforge --version",
        "--config examples/config.offline.yaml config check --json",
        "doctor --json",
        "dag validate examples/task-graph.json --json",
        "scripts/run-usability-eval.py",
        "actions/upload-artifact@v4",
    )
    missing = [fragment for fragment in required_fragments if fragment not in text]
    assert not missing, f"CI is missing release gates: {missing}"
    assert "continue-on-error" not in text


def test_eval_runner_is_publicly_executable_and_defaults_to_five_repeats() -> None:
    assert EVAL_RUNNER.is_file()
    source = EVAL_RUNNER.read_text(encoding="utf-8")
    assert "DEFAULT_REPETITIONS = 5" in source
    assert "live_provider_executed\": False" in source
    for artifact in (
        "environment.json",
        "commands.jsonl",
        "metrics.json",
        "junit.xml",
        "stdout",
        "stderr",
        "summary.md",
        "provider-requests.jsonl",
    ):
        assert artifact in source
    completed = subprocess.run(
        [sys.executable, str(EVAL_RUNNER), "--help"],
        cwd=PROJECT_ROOT,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=15,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "must be at least 5" in completed.stdout
    assert "eval-results" in completed.stdout


def _public_executable_text(path: Path) -> str:
    text = path.read_text(encoding="utf-8", errors="replace")
    if path.suffix.casefold() != ".md":
        return text
    return "\n".join(
        match.group(1)
        for match in re.finditer(r"```[^\n]*\n(.*?)```", text, re.DOTALL)
    )


def test_public_executable_commands_do_not_use_author_absolute_paths() -> None:
    roots = (
        PROJECT_ROOT / "README.md",
        PROJECT_ROOT / "docs",
        PROJECT_ROOT / "scripts",
        PROJECT_ROOT / "examples",
        PROJECT_ROOT / ".github",
    )
    files: list[Path] = []
    for root in roots:
        if root.is_file():
            files.append(root)
        elif root.is_dir():
            files.extend(
                path
                for path in root.rglob("*")
                if path.is_file()
                and path.suffix.casefold()
                in {".md", ".py", ".sh", ".ps1", ".yaml", ".yml", ".json", ".toml", ".txt"}
            )
    forbidden = (
        re.compile(r"/mnt/[a-z]/Desktop/简历项目", re.IGNORECASE),
        re.compile(r"[a-z]:\\Desktop\\简历项目", re.IGNORECASE),
        re.compile(r"[a-z]:\\Users\\[^\\\s\"']+(?:\\|\b)", re.IGNORECASE),
    )
    violations: list[str] = []
    for path in files:
        public_text = _public_executable_text(path)
        if any(pattern.search(public_text) for pattern in forbidden):
            violations.append(path.relative_to(PROJECT_ROOT).as_posix())
    assert not violations, f"author-specific absolute paths remain in public commands: {violations}"
