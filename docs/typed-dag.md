# Typed Agent workflows

`eviforge dag` runs a typed dependency graph using the same Agent and file tools
as the interactive application. Each node has a fresh conversation and file cache.
The graph selects Explorer, Implementer, Verifier or Integrator roles and narrows
their tools, read scopes, write scopes and exact verification argv invocations.

```console
eviforge dag schema
eviforge dag validate examples/dag-review.json
eviforge dag run examples/dag-review.json --mode acceptEdits --run-id docs-review
eviforge dag run examples/dag-review.json --resume RUN_ID
eviforge dag events RUN_ID --after 0
```

Schema, validation and stored-event export do not load Provider configuration or
call a model. Running a graph needs the usual Provider configuration. The graph's
capabilities are intersected with the parent permission mode and Plan gate; merely
declaring a write or command in JSON does not approve it. Active Plan grants are
bound to their approved Agent audience and are not copied to DAG children.

The example changes README.md when write permission permits it. Review the graph
and use it in a disposable checkout when trying the workflow for the first time.

## Contracts and evidence

Node IDs are unique. Dependencies must exist and form an acyclic graph. Every
input reference names a declared dependency and its actual output role. Verifiers
must consume Implementer results. Integrators must consume Implementer and
Verifier results, with passing verification covering every implementation before
the Integrator can start.

Resolved inputs are validated against distinct ExplorerInput, ImplementerInput,
VerifierInput and IntegratorInput schemas before they enter a node's prompt.

Nodes finish by calling `SubmitNodeResult` with their role's strict schema. A final
text message alone is not a valid result. Modified files must be reported and
captured with `CaptureArtifact`; the runner checks the reported paths against
actual successful file-tool writes and independently computes their hashes.
Evidence references identify the run, node, attempt, file path, byte length and
SHA-256. Immutable blobs are stored under `.eviforge/dag/artifacts/`.

A Verifier must actually inspect files or execute declared commands. Its semantic
judgment is still a model judgment; type checks and hashes do not establish that
the code is correct. Command results and tool outcomes are recorded as node events.
Exact command invocations and their returned output also become automatically
hashed artifact receipts in the Verifier output.

## Concurrency and authority

Scopes are literal relative file or directory paths, not globs. Parent/child path
overlap counts as a conflict. Writers are serialized against other writers and
readers of overlapping scopes. Independent readers and nodes with disjoint scopes
can run concurrently up to `max_concurrency`. The default read scope `.` is
conservative and will serialize writers; use explicit read scopes to increase
parallelism safely. Git and runtime metadata are never writable file-tool scopes.

Only Verifier nodes expose Bash, and only for exact `argv` and `timeout` pairs
declared by the operator. Shell command strings are rejected. Verification
processes may write files or access the network themselves; their effects are not
an operating-system sandbox. Command nodes therefore conflict with every running
node and count as potentially writable for recovery. Arbitrary MCP, Skill, Hook,
recursive Agent and team-control tools are not exposed inside DAG nodes.

## Recovery

The SQLite journal uses durable transactions, conditional node claims and an owner
fence. A second scheduler cannot claim a run with a live owner. Events identify
their run, node and attempt; `dag events` exports them as versioned JSONL.

Resume checks the graph, actual tool schemas and implementations, role definitions,
permission rules, archived evidence and the latest acknowledged file states.
Completed nodes load their previous typed result without executing again. Changed
graphs, changed capabilities, damaged evidence or externally edited checkpoint
files reject resume. Historical evidence remains valid after a later node edits
the same working file, because its archived blob is immutable.

Interrupted or failed nodes with write or command capabilities are ambiguous by
default, including failure during final output validation after a successful write.
They are never automatically replayed. After inspecting the workspace, an explicit
operator retry can be recorded as follows:

```console
eviforge dag run graph.json --resume RUN_ID --replay-node NODE_ID --replay-reason "Inspected effects and authorize retry"
```

This records the retry decision; it does not restore consumed or expired Plan
grants. Existing acknowledged-file drift must still be reconciled before retry.
Completed nodes cannot be authorized for replay in the same run. Purely read-only
unfinished nodes may restart on resume. This is conservative recovery, not an
exactly-once transaction spanning external processes and filesystem writes.

Run exit codes are 0 for success, 1 for node failure, 2 for invalid configuration,
graph or checkpoint, 3 for required replay approval, and 4 for ambiguous execution.
Cancellation follows the common CLI exit code 130.
The deterministic tests use fake model streams with actual Agents, temporary
files, actual subprocesses and SQLite; they do not measure live model quality.
