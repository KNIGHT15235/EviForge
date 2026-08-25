# EviForge Eval Summary

> Evidence class: deterministic mechanism benchmark. This is **not** an LLM general-capability evaluation and cannot support claims about open-ended coding ability.

- Experiment: `risk_policy_mechanism_v1`
- Stratum: `deterministic_safe_fixtures`
- Contrast: `risk_policy_on_minus_legacy_category_policy`
- Primary endpoint: `risk_false_allow`
- Candidate SHA: `61078884557c1ea31bc5b621622373fa5d3a328f`
- Repository dirty at start: `False`
- Dataset SHA-256: `9d0374ff59d8ab35431773d43dd7028fd39e876a6a7d8b28bf792f8d6106c604`
- Scheduled ITT runs: 240
- Status counts: `{"completed":240}`

> If the repository was dirty at run start, treat this result as provisional smoke evidence and rerun after committing the candidate before quoting it on a resume.

## Recomputed results

| Role | Metric | Contrast | Arm A actual | Arm B actual | Paired Δ (B−A) | Cluster 95% CI | N clusters / ITT runs | Latency median/p95 A (ms) | Latency median/p95 B (ms) |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|
| Primary | `risk_false_allow` | risk_policy_on_minus_legacy_category_policy | 0.833333 | 0 | -0.75 | [-1, -0.25] | 4 / 240 | 0.0285/0.049105 | 5.43385/7.6566 |
| Secondary | `binary_pass` | risk_policy_on_minus_legacy_category_policy | 0.166667 | 1 | 0.75 | [0.25, 1] | 4 / 240 | 0.0285/0.049105 | 5.43385/7.6566 |

Timeouts, crashes and empty outputs stay in the ITT denominator as failures. Only pre-registered infrastructure failures are marked invalid, remain in `runs.jsonl`, and are counted above.

Recompute with:

```console
uv run python -m evals.run recompute --result-dir evals/results/mechanism-v1-clean
```
