from __future__ import annotations

import asyncio
import json
import platform
import random
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from evals.adapters import AdapterOutput, EvalAdapter
from evals.hashing import (
    canonical_json,
    dataset_sha256,
    sha256_bytes,
    sha256_file,
    verify_frozen_dataset,
)
from evals.schema import MANIFEST_SCHEMA_VERSION, RUN_SCHEMA_VERSION, EvalCase, load_dataset
from evals.scorers import SCORER_VERSION, SCORERS


PRE_REGISTERED_INFRASTRUCTURE_FAILURES = frozenset(
    {
        "fixture_corrupt",
        "filesystem_unavailable",
        "runner_interrupted",
    }
)


@dataclass(frozen=True, slots=True)
class ExperimentConfig:
    dataset_path: Path
    output_root: Path
    adapter_name: str
    experiment: str
    stratum: str
    contrast: str
    primary_scorer: str
    secondary_scorers: tuple[str, ...] = ()
    baseline_sha: str = "unknown"
    candidate_sha: str = "unknown"
    feature_flag: str = "feature_enabled"
    warmup: int = 1
    repeats: int = 3
    timeout_seconds: float = 2.0
    bootstrap_seed: int = 20260812
    bootstrap_samples: int = 10_000
    command: tuple[str, ...] = ()
    result_dir_name: str | None = None


@dataclass(frozen=True, slots=True)
class ExperimentResult:
    result_dir: Path
    manifest: Mapping[str, Any]
    rows: tuple[Mapping[str, Any], ...]


async def execute_experiment(
    config: ExperimentConfig,
    adapter: EvalAdapter,
    *,
    repo_root: str | Path,
) -> ExperimentResult:
    if config.primary_scorer not in SCORERS:
        raise ValueError(f"Unknown primary scorer: {config.primary_scorer}")
    unknown = [name for name in config.secondary_scorers if name not in SCORERS]
    if unknown:
        raise ValueError(f"Unknown secondary scorer(s): {', '.join(unknown)}")
    if config.warmup < 0 or config.repeats < 1:
        raise ValueError("warmup must be >= 0 and repeats must be >= 1")
    if config.timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")

    frozen = verify_frozen_dataset(config.dataset_path)
    cases = [
        case
        for case in load_dataset(config.dataset_path)
        if case.experiment == config.experiment and case.stratum == config.stratum
    ]
    if not cases:
        raise ValueError(
            f"No cases for experiment={config.experiment!r}, stratum={config.stratum!r}"
        )

    repo = Path(repo_root).resolve()
    repository_state = _repository_state(repo, config.candidate_sha)
    started_at = datetime.now(timezone.utc)
    run_group_id = uuid.uuid4().hex
    result_name = config.result_dir_name or (
        f"{started_at.strftime('%Y%m%dT%H%M%SZ')}-{config.candidate_sha[:8]}-{run_group_id[:8]}"
    )
    result_dir = config.output_root / result_name
    failures_dir = result_dir / "failures"
    failures_dir.mkdir(parents=True, exist_ok=False)
    runs_path = result_dir / "runs.jsonl"

    # Warmup uses real dataset inputs but is excluded from runs and all ITT
    # denominators.  It alternates arms so one path is not preferentially hot.
    for index in range(config.warmup):
        case = cases[index % len(cases)]
        await _warmup(adapter, case, bool(index % 2), config.timeout_seconds)

    schedule = _paired_schedule(cases, config.repeats, config.bootstrap_seed)
    rows: list[dict[str, Any]] = []
    # Exclusive creation plus per-observation append preserves completed raw
    # records if the runner itself is interrupted.  Summary generation never
    # rewrites this file.
    with runs_path.open("x", encoding="utf-8", newline="\n") as runs_handle:
        for scheduled_index, (case, repeat_index, arm, enabled) in enumerate(schedule):
            row = await _execute_one(
                adapter,
                case,
                repeat_index=repeat_index,
                scheduled_index=scheduled_index,
                arm=arm,
                feature_enabled=enabled,
                timeout_seconds=config.timeout_seconds,
                contrast=config.contrast,
                primary_scorer=config.primary_scorer,
                secondary_scorers=config.secondary_scorers,
                run_group_id=run_group_id,
            )
            rows.append(row)
            runs_handle.write(canonical_json(row) + "\n")
            runs_handle.flush()
            if row["status"] != "completed":
                failure_path = failures_dir / f"{row['observation_id']}.json"
                failure_path.write_text(
                    json.dumps(row, ensure_ascii=False, indent=2, sort_keys=True),
                    encoding="utf-8",
                )
    ended_at = datetime.now(timezone.utc)
    manifest = _build_manifest(
        config,
        repo=repo,
        dataset_digest=dataset_sha256(config.dataset_path),
        frozen_manifest=frozen,
        run_group_id=run_group_id,
        started_at=started_at,
        ended_at=ended_at,
        n_cases=len(cases),
        n_rows=len(rows),
        repository_state=repository_state,
    )
    (result_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return ExperimentResult(result_dir=result_dir, manifest=manifest, rows=tuple(rows))


async def _warmup(
    adapter: EvalAdapter,
    case: EvalCase,
    enabled: bool,
    timeout_seconds: float,
) -> None:
    try:
        await asyncio.wait_for(
            adapter.run(case.case, feature_enabled=enabled, seed=case.seed),
            timeout=timeout_seconds,
        )
    except Exception:
        # A warmup failure cannot silently invalidate scheduled observations;
        # all measured runs still execute and follow ITT.
        return


def _paired_schedule(
    cases: Sequence[EvalCase], repeats: int, seed: int
) -> list[tuple[EvalCase, int, str, bool]]:
    pairs: list[tuple[EvalCase, int, list[tuple[str, bool]]]] = []
    for repeat_index in range(repeats):
        for case in cases:
            pair_seed = f"{seed}:{case.cluster_id}:{case.run_id}:{case.seed}:{repeat_index}"
            pair_rng = random.Random(pair_seed)
            arms = [("off", False), ("on", True)]
            pair_rng.shuffle(arms)
            pairs.append((case, repeat_index, arms))
    random.Random(seed).shuffle(pairs)
    return [
        (case, repeat_index, arm, enabled)
        for case, repeat_index, arms in pairs
        for arm, enabled in arms
    ]


async def _execute_one(
    adapter: EvalAdapter,
    case: EvalCase,
    *,
    repeat_index: int,
    scheduled_index: int,
    arm: str,
    feature_enabled: bool,
    timeout_seconds: float,
    contrast: str,
    primary_scorer: str,
    secondary_scorers: Sequence[str],
    run_group_id: str,
) -> dict[str, Any]:
    observation_id = uuid.uuid5(
        uuid.UUID(run_group_id),
        f"{case.run_id}:{case.seed}:{repeat_index}:{arm}",
    ).hex
    started_ns = time.perf_counter_ns()
    status = "completed"
    error_type: str | None = None
    error_message: str | None = None
    actual: Mapping[str, Any] = {}
    adapter_latency_ms: float | None = None
    invalid = False
    invalid_reason: str | None = None

    try:
        output = await asyncio.wait_for(
            adapter.run(case.case, feature_enabled=feature_enabled, seed=case.seed),
            timeout=timeout_seconds,
        )
        if output is None or not isinstance(output, AdapterOutput):
            status = "no_output"
            error_type = "NoOutput"
            error_message = "adapter returned no typed output"
        else:
            actual = dict(output.actual)
            adapter_latency_ms = float(output.latency_ms)
            if not actual:
                status = "no_output"
                error_type = "NoOutput"
                error_message = "adapter returned an empty actual object"
    except TimeoutError:
        status = "timeout"
        error_type = "TimeoutError"
        error_message = f"observation exceeded {timeout_seconds:g}s"
    except Exception as exc:
        status = "crash"
        error_type = type(exc).__name__
        error_message = str(exc)
        reason = getattr(exc, "infrastructure_failure", None)
        if reason in PRE_REGISTERED_INFRASTRUCTURE_FAILURES:
            invalid = True
            invalid_reason = str(reason)

    wall_latency_ms = (time.perf_counter_ns() - started_ns) / 1_000_000.0
    scorer_names = (primary_scorer, *secondary_scorers)
    scores: dict[str, float] = {}
    for scorer_name in scorer_names:
        # ITT: every scheduled, non-infrastructure-invalid failure remains in
        # the denominator and receives the adverse binary score.
        if invalid:
            scores[scorer_name] = 0.0
        elif status != "completed":
            scores[scorer_name] = 1.0 if scorer_name in {
                "repeat_error",
                "risk_false_allow",
                "recovery_any_duplicate",
            } else 0.0
        else:
            scores[scorer_name] = float(SCORERS[scorer_name](actual, case.expected))

    return {
        "schema_version": RUN_SCHEMA_VERSION,
        "run_group_id": run_group_id,
        "observation_id": observation_id,
        "run_id": observation_id,
        "experiment": case.experiment,
        "stratum": case.stratum,
        "cluster_id": case.cluster_id,
        "dataset_run_id": case.run_id,
        "seed": case.seed,
        "repeat_index": repeat_index,
        "scheduled_index": scheduled_index,
        "Arm": arm,
        "feature_enabled": feature_enabled,
        "Contrast": contrast,
        "Primary": primary_scorer,
        "status": status,
        "invalid": invalid,
        "invalid_reason": invalid_reason,
        "error_type": error_type,
        "error_message": error_message,
        "expected": dict(case.expected),
        "actual": dict(actual),
        "scores": scores,
        "latency_ms": wall_latency_ms,
        "adapter_latency_ms": adapter_latency_ms,
    }


def _build_manifest(
    config: ExperimentConfig,
    *,
    repo: Path,
    dataset_digest: str,
    frozen_manifest: Mapping[str, Any] | None,
    run_group_id: str,
    started_at: datetime,
    ended_at: datetime,
    n_cases: int,
    n_rows: int,
    repository_state: Mapping[str, Any],
) -> dict[str, Any]:
    lock_path = repo / "uv.lock"
    cpu = platform.processor() or _windows_cpu_name() or "unknown"
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "run_group_id": run_group_id,
        "evidence_class": "deterministic_mechanism_benchmark",
        "generalization_claim": "not_an_llm_general_capability_evaluation",
        "experiment": config.experiment,
        "stratum": config.stratum,
        "contrast": config.contrast,
        "arm_a": {
            "name": "off",
            "runtime_sha": config.candidate_sha,
            "feature_flags": {config.feature_flag: False},
        },
        "arm_b": {
            "name": "on",
            "runtime_sha": config.candidate_sha,
            "feature_flags": {config.feature_flag: True},
        },
        "paired_runtime_invariant": "same_candidate_runtime_and_adapter; feature flag only",
        "primary": config.primary_scorer,
        "secondary": list(config.secondary_scorers),
        "baseline_sha": config.baseline_sha,
        "baseline_sha_role": "external runnable-baseline reference; not Arm A runtime",
        "candidate_sha": config.candidate_sha,
        "candidate_sha_role": "paired Arm A/Arm B runtime commit reference",
        "repository_state_at_start": dict(repository_state),
        "dataset": {
            "path": str(config.dataset_path.resolve()),
            "schema_version": frozen_manifest.get("schema_version") if frozen_manifest else None,
            "dataset_sha256": dataset_digest,
            "freeze_manifest_present": frozen_manifest is not None,
        },
        "uv_lock_sha256": sha256_file(lock_path) if lock_path.is_file() else None,
        "environment": {
            "python": sys.version,
            "python_executable": sys.executable,
            "os": platform.platform(),
            "cpu": cpu,
            "machine": platform.machine(),
        },
        "adapter": config.adapter_name,
        "scorer_version": SCORER_VERSION,
        "feature_flags": {config.feature_flag: [False, True]},
        "command": list(config.command),
        "warmup": config.warmup,
        "repeats": config.repeats,
        "timeout_seconds": config.timeout_seconds,
        "bootstrap": {
            "unit": "cluster_id",
            "seed": config.bootstrap_seed,
            "samples": config.bootstrap_samples,
        },
        "itt": {
            "enabled": True,
            "scheduled_failures_count_as_failure": ["timeout", "crash", "no_output"],
            "pre_registered_infrastructure_failures": sorted(
                PRE_REGISTERED_INFRASTRUCTURE_FAILURES
            ),
        },
        "n_dataset_cases": n_cases,
        "n_scheduled_runs": n_rows,
        "started_at": started_at.isoformat(),
        "ended_at": ended_at.isoformat(),
        "duration_seconds": (ended_at - started_at).total_seconds(),
    }


def _repository_state(repo: Path, candidate_sha: str) -> dict[str, Any]:
    status = _git_capture(repo, ["status", "--porcelain=v1", "--untracked-files=normal"])
    head = _git_capture(repo, ["rev-parse", "HEAD"]).strip() or "unknown"
    return {
        "head_sha": head,
        "candidate_sha_matches_head": head == candidate_sha,
        "dirty": bool(status.strip()),
        "porcelain_sha256": sha256_bytes(status.encode("utf-8")),
        "note": (
            "A dirty run is valid as a smoke/provisional mechanism measurement but must be rerun "
            "from a clean candidate commit before a resume claim."
        ),
    }


def _git_capture(repo: Path, arguments: list[str]) -> str:
    try:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=repo,
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        return completed.stdout if completed.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _windows_cpu_name() -> str:
    if platform.system() != "Windows":
        return ""
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-Command", "(Get-CimInstance Win32_Processor).Name"],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
        return completed.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""
