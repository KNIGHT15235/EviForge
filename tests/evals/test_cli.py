from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def test_cli_mechanism_and_recompute(tmp_path: Path) -> None:
    result_name = "cli-smoke"
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "evals.run",
            "mechanism",
            "--output-root",
            str(tmp_path),
            "--result-name",
            result_name,
            "--warmup",
            "0",
            "--repeats",
            "1",
            "--bootstrap-samples",
            "20",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    result_dir = tmp_path / result_name
    assert (result_dir / "runs.jsonl").is_file()
    manifest = json.loads((result_dir / "manifest.json").read_text(encoding="utf-8"))
    state = manifest["repository_state_at_start"]
    expected_returncode = 3 if state["dirty"] or not state["candidate_sha_matches_head"] else 0
    assert completed.returncode == expected_returncode, completed.stderr
    if expected_returncode == 3:
        assert "provisional" in completed.stderr
    else:
        assert completed.stderr == ""
    assert "not an LLM general-capability result" in completed.stdout

    recomputed = subprocess.run(
        [
            sys.executable,
            "-m",
            "evals.run",
            "recompute",
            "--result-dir",
            str(result_dir),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert recomputed.returncode == 0, recomputed.stderr
    parsed = json.loads(recomputed.stdout)
    assert parsed[0]["EndpointRole"] == "Primary"
