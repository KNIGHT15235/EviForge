from __future__ import annotations

import csv
import hashlib
import json
import math
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from evals.hashing import canonical_json
from evals.scorers import cluster_bootstrap_mean, latency_summary, paired_cluster_deltas


SUMMARY_COLUMNS = (
    "Experiment",
    "Stratum",
    "EndpointRole",
    "Metric",
    "Contrast",
    "ArmA",
    "ArmAActual",
    "ArmB",
    "ArmBActual",
    "PairedDelta",
    "Cluster95CILow",
    "Cluster95CIHigh",
    "NClusters",
    "NRunsITT",
    "NInvalid",
    "LatencyMedianMsA",
    "LatencyP95MsA",
    "LatencyMedianMsB",
    "LatencyP95MsB",
)


def read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with Path(path).open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            raw = json.loads(line)
            if not isinstance(raw, dict):
                raise ValueError(f"runs line {line_number} is not an object")
            rows.append(raw)
    return rows


def summarise_rows(rows: Sequence[Mapping[str, Any]], manifest: Mapping[str, Any]) -> list[dict[str, Any]]:
    if not rows:
        return []
    primary = str(manifest["primary"])
    secondary = [str(value) for value in manifest.get("secondary", ())]
    result: list[dict[str, Any]] = []
    for endpoint_role, metric in [("Primary", primary), *[("Secondary", item) for item in secondary]]:
        flat_rows = [
            {**row, "metric_value": row.get("scores", {}).get(metric)}
            for row in rows
        ]
        valid = [row for row in flat_rows if not row.get("invalid")]
        by_arm: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for row in valid:
            by_arm[str(row["Arm"])].append(row)
        off_values = [float(row["metric_value"]) for row in by_arm["off"]]
        on_values = [float(row["metric_value"]) for row in by_arm["on"]]
        deltas = paired_cluster_deltas(flat_rows, value_key="metric_value")
        interval = cluster_bootstrap_mean(
            deltas,
            seed=int(manifest["bootstrap"]["seed"]),
            samples=int(manifest["bootstrap"]["samples"]),
        )
        off_latency = latency_summary([float(row["latency_ms"]) for row in by_arm["off"]])
        on_latency = latency_summary([float(row["latency_ms"]) for row in by_arm["on"]])
        result.append(
            {
                "Experiment": manifest["experiment"],
                "Stratum": manifest["stratum"],
                "EndpointRole": endpoint_role,
                "Metric": metric,
                "Contrast": manifest["contrast"],
                "ArmA": manifest["arm_a"]["name"],
                "ArmAActual": statistics.fmean(off_values) if off_values else math.nan,
                "ArmB": manifest["arm_b"]["name"],
                "ArmBActual": statistics.fmean(on_values) if on_values else math.nan,
                "PairedDelta": interval.estimate,
                "Cluster95CILow": interval.low,
                "Cluster95CIHigh": interval.high,
                "NClusters": interval.n_clusters,
                "NRunsITT": len(valid),
                "NInvalid": sum(bool(row.get("invalid")) for row in flat_rows),
                "LatencyMedianMsA": off_latency["median_ms"],
                "LatencyP95MsA": off_latency["p95_ms"],
                "LatencyMedianMsB": on_latency["median_ms"],
                "LatencyP95MsB": on_latency["p95_ms"],
            }
        )
    return result


def write_reports(result_dir: str | Path) -> list[dict[str, Any]]:
    directory = Path(result_dir)
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    recorded_runs_digest = manifest.get("artifacts", {}).get("runs.jsonl")
    if recorded_runs_digest is not None:
        actual_runs_digest = _file_digest(directory / "runs.jsonl")
        if recorded_runs_digest != actual_runs_digest:
            raise ValueError(
                "runs.jsonl digest does not match manifest; refusing to silently recompute "
                "from modified raw observations"
            )
    rows = read_jsonl(directory / "runs.jsonl")
    summaries = summarise_rows(rows, manifest)

    csv_path = directory / "summary.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=SUMMARY_COLUMNS)
        writer.writeheader()
        writer.writerows(summaries)

    statuses = Counter(str(row.get("status")) for row in rows)
    lines = [
        "# EviForge Eval Summary",
        "",
        "> Evidence class: deterministic mechanism benchmark. This is **not** an LLM general-capability evaluation and cannot support claims about open-ended coding ability.",
        "",
        f"- Experiment: `{manifest['experiment']}`",
        f"- Stratum: `{manifest['stratum']}`",
        f"- Contrast: `{manifest['contrast']}`",
        f"- Primary endpoint: `{manifest['primary']}`",
        f"- Candidate SHA: `{manifest['candidate_sha']}`",
        f"- Repository dirty at start: `{manifest.get('repository_state_at_start', {}).get('dirty', 'unknown')}`",
        f"- Dataset SHA-256: `{manifest['dataset']['dataset_sha256']}`",
        f"- Scheduled ITT runs: {manifest['n_scheduled_runs']}",
        f"- Status counts: `{canonical_json(dict(sorted(statuses.items())))}`",
        "",
        "> If the repository was dirty at run start, treat this result as provisional smoke evidence and rerun after committing the candidate before quoting it on a resume.",
        "",
        "## Recomputed results",
        "",
        "| Role | Metric | Contrast | Arm A actual | Arm B actual | Paired Δ (B−A) | Cluster 95% CI | N clusters / ITT runs | Latency median/p95 A (ms) | Latency median/p95 B (ms) |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for summary in summaries:
        lines.append(
            "| {EndpointRole} | `{Metric}` | {Contrast} | {ArmAActual:.6g} | "
            "{ArmBActual:.6g} | {PairedDelta:.6g} | [{Cluster95CILow:.6g}, "
            "{Cluster95CIHigh:.6g}] | {NClusters} / {NRunsITT} | "
            "{LatencyMedianMsA:.6g}/{LatencyP95MsA:.6g} | "
            "{LatencyMedianMsB:.6g}/{LatencyP95MsB:.6g} |".format(**summary)
        )
    lines.extend(
        [
            "",
            "Timeouts, crashes and empty outputs stay in the ITT denominator as failures. Only pre-registered infrastructure failures are marked invalid, remain in `runs.jsonl`, and are counted above.",
            "",
            "Recompute with:",
            "",
            "```powershell",
            f"python -m evals.run recompute --result-dir \"{directory.resolve()}\"",
            "```",
            "",
        ]
    )
    (directory / "summary.md").write_text("\n".join(lines), encoding="utf-8")
    manifest["artifacts"] = {
        "runs.jsonl": _file_digest(directory / "runs.jsonl"),
        "summary.csv": _file_digest(directory / "summary.csv"),
        "summary.md": _file_digest(directory / "summary.md"),
    }
    (directory / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return summaries


def _file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
