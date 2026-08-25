from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from pathlib import Path
from typing import Sequence

from evals.adapters import RiskPolicyMechanismAdapter
from evals.report import write_reports
from evals.runner import ExperimentConfig, execute_experiment


DEFAULT_DATASET = Path(__file__).parent / "datasets" / "risk_policy_mechanism.v1.jsonl"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m evals.run",
        description="Run or recompute a reproducible EviForge evaluation.",
    )
    subparsers = parser.add_subparsers(dest="command_name")

    run_parser = subparsers.add_parser("mechanism", help="run the frozen risk mechanism benchmark")
    run_parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    run_parser.add_argument("--output-root", type=Path, default=Path("evals/results"))
    run_parser.add_argument("--result-name")
    run_parser.add_argument("--warmup", type=int, default=2)
    run_parser.add_argument("--repeats", type=int, default=5)
    run_parser.add_argument("--timeout", type=float, default=2.0)
    run_parser.add_argument("--bootstrap-seed", type=int, default=20260812)
    run_parser.add_argument("--bootstrap-samples", type=int, default=10_000)
    run_parser.add_argument("--baseline-sha")
    run_parser.add_argument("--candidate-sha")

    recompute_parser = subparsers.add_parser(
        "recompute", help="regenerate summary.csv and summary.md from manifest + runs"
    )
    recompute_parser.add_argument("--result-dir", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    actual_argv = list(sys.argv[1:] if argv is None else argv)
    if not actual_argv:
        actual_argv = ["mechanism"]
    args = build_parser().parse_args(actual_argv)
    if args.command_name == "mechanism":
        return asyncio.run(_run_mechanism(args))
    if args.command_name == "recompute":
        summaries = write_reports(args.result_dir)
        print(json.dumps(summaries, ensure_ascii=False, indent=2))
        return 0
    raise AssertionError(f"unknown command {args.command_name!r}")


async def _run_mechanism(args: argparse.Namespace) -> int:
    repo_root = Path(__file__).resolve().parents[1]
    candidate_sha = args.candidate_sha or _git_sha(repo_root, "HEAD")
    baseline_sha = args.baseline_sha or _git_sha(repo_root, "runnable-baseline")
    command = tuple([sys.executable, "-m", "evals.run", *sys.argv[1:]])
    config = ExperimentConfig(
        dataset_path=args.dataset,
        output_root=args.output_root,
        adapter_name="risk-policy-mechanism",
        experiment="risk_policy_mechanism_v1",
        stratum="deterministic_safe_fixtures",
        contrast="risk_policy_on_minus_legacy_category_policy",
        primary_scorer="risk_false_allow",
        secondary_scorers=("binary_pass",),
        baseline_sha=baseline_sha,
        candidate_sha=candidate_sha,
        feature_flag="risk_policy",
        warmup=args.warmup,
        repeats=args.repeats,
        timeout_seconds=args.timeout,
        bootstrap_seed=args.bootstrap_seed,
        bootstrap_samples=args.bootstrap_samples,
        command=command,
        result_dir_name=args.result_name,
    )
    adapter = RiskPolicyMechanismAdapter(workspace_root=repo_root)
    result = await execute_experiment(config, adapter, repo_root=repo_root)
    summaries = write_reports(result.result_dir)
    print(result.result_dir.resolve())
    for summary in summaries:
        print(
            f"{summary['EndpointRole']} {summary['Metric']}: "
            f"off={summary['ArmAActual']:.6g}, on={summary['ArmBActual']:.6g}, "
            f"paired_delta={summary['PairedDelta']:.6g}, "
            f"N_clusters={summary['NClusters']}, N_runs_ITT={summary['NRunsITT']}"
        )
    print("Scope: deterministic mechanism benchmark; not an LLM general-capability result.")
    state = result.manifest.get("repository_state_at_start", {})
    if state.get("dirty") or not state.get("candidate_sha_matches_head"):
        print(
            "Result is provisional: repository must be clean and candidate SHA must match HEAD.",
            file=sys.stderr,
        )
        return 3
    return 0


def _git_sha(repo_root: Path, revision: str) -> str:
    completed = subprocess.run(
        ["git", "rev-parse", revision],
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip() if completed.returncode == 0 else "unknown"


if __name__ == "__main__":
    raise SystemExit(main())
