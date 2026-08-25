# EviForge Eval Lab

This directory is the evidence layer for benchmarks, not a marketing-number generator.

Run the frozen, deterministic mechanism smoke benchmark:

```powershell
python -m evals.run mechanism
```

The command exits with code `3` when the repository is dirty or the requested
candidate SHA does not equal `HEAD`. Such output remains useful for local smoke
debugging, but it is explicitly non-quotable. A resume-ready result must come
from a clean commit and retain the raw `runs.jsonl` plus manifest hashes.

The two arms use the same candidate runtime and adapter. Only the preregistered `risk_policy` feature flag changes (`off` then `on`, with randomized pair order). Every scheduled timeout, crash, or empty output is an ITT failure. A run may be invalid only for one of the infrastructure-failure reason codes frozen in `evals.runner`; invalid records remain in `runs.jsonl`.

Each result directory contains:

- `manifest.json`: code/data/environment/protocol provenance;
- `runs.jsonl`: append-only observation-level records;
- `summary.csv` and `summary.md`: cluster-aware recomputations;
- `failures/`: one JSON artifact per non-completed observation.

The included fixtures exercise deterministic policy classification. They do **not** measure open-ended LLM coding ability and must never be reported as a general agent-quality result.

The primary `risk_false_allow` score is a per-operation 0/1 event indicator
(`should_deny && allowed`). Its arm actual is the mean over **all** scheduled
operation observations, including the benign control; it is therefore a
false-allow incidence, not a conditional dangerous-only rate. The paired
effect is computed after aggregating by the four independent `cluster_id`
values, so rewrites and repeated runs do not inflate the statistical N.
