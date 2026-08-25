# EviForge v0.4 deterministic release evidence

- Evaluated at: `2026-08-23T19:43:51.158254Z`
- Result: **PASS**
- Base Git revision: `b30ab65bfcf66f570e4789bda3921c5a6827df12`
- Candidate form: modified working tree; raw run records `git_dirty_before=true`
- Live Provider calls: **0**

## Cross-platform verification

| Surface | Result | Elapsed |
|---|---:|---:|
| Windows 10 / Python 3.12.7 | 1006 passed, 1 skipped | 72.74 s |
| WSL2 Ubuntu 24.04 / Python 3.12 | 1007 passed | 62.79 s |
| Wheel build + isolated install | EviForge 0.4.0 started successfully | — |

The Windows skip is the platform-specific POSIX symlink case. WSL verification
ran from the Chinese DrvFS checkout path and used a Linux-only environment at
`~/.cache/eviforge/venv`.

## Five-repeat deterministic usability run

All `35/35` command samples passed (`5/5` complete repetitions), the workspace
was unchanged outside the raw artifact directory, and all five OpenAI-compatible
fake SSE requests passed protocol checks with zero credential-header leaks.

| Check | Passed | Median duration |
|---|---:|---:|
| CLI help | 5/5 | 986.187 ms |
| CLI version | 5/5 | 822.682 ms |
| Offline config check | 5/5 | 910.250 ms |
| Doctor | 5/5 | 1107.978 ms |
| DAG validate | 5/5 | 930.114 ms |
| Provider fake SSE | 5/5 | 4301.320 ms |
| Release-contract tests | 5/5 | 10164.889 ms |

## Claim boundary

This is a zero-cost deterministic fixture. It proves reproducibility of the
listed CLI, Schema, SDK request and stream-parser paths; it does not measure
live model quality, live Provider latency, cost, open-ended coding success, or
OS-level isolation. The run was made from a dirty working tree, so it is local
candidate evidence rather than proof that a remote clean-clone CI run passed.

The complete raw artifact remains local/CI-only because commands and JUnit may
contain machine paths. `manifest.json` records SHA-256 hashes for audit, while
`metrics.json` and `provider-requests.jsonl` are the public, redacted evidence.
