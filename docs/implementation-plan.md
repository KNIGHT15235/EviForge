# EviForge implementation plan

## Authority and scope

The supplied resume is the functional source of truth. The LikeCC source and the
local LikeCC reference document define the compatibility baseline. Historical
benchmark numbers are not acceptance thresholds and will not be presented as new
measurements. The original LikeCC checkout remains unchanged.

## Architecture

Reuse the Python ReAct Agent, Provider adapters, ToolRegistry, Textual UI, command
handlers, sessions, MCP, skills, SubAgent, teams and Git worktree code. Rename the
new distribution, package and runtime directory to `eviforge`, `eviforge` and
`.eviforge`. Keep a separate Git baseline and a file hash manifest for comparison.

| Resume requirement | Implementation and execution boundary |
| --- | --- |
| Shared runtime | A reusable service assembly and lifecycle owner for Memory, Skill, MCP, Session, SubAgent and Team; both TUI and headless consume it, await/cancel/reap owned background tasks, and close Provider/MCP resources. |
| Plan and audit | Persistent PlanSession state and immutable content hashes bound to session and turn. Approval grants identify exact tool arguments/argv, working directory, file roots, network hosts and expiry. The final common tool execution entry checks and consumes grants again. A changed or stale plan never inherits old authorization. |
| Memory and experience | SQLite audited records with scope, source task/trace, content hash and status. User/project memory share a total 16,000-character budget fairly. Conversation extraction creates quarantined candidates only. Verification, human confirmation, feedback and rollback govern active Skill versions. Revocation refreshes already injected context. |
| Typed DAG | Strict role inputs/outputs for Explorer, Implementer, Verifier and Integrator; offline graph validation; dependency scheduling with conflicting write sets serialized; actual existing Agents run nodes behind narrowed tool capabilities. Immutable SHA-256 artifacts, node events and SQLite checkpoints support resume without rerunning completed nodes. Graph/capability drift rejects resume; interrupted or failed writable nodes require explicit replay authorization. |
| Automation and recovery | Versioned RunResult JSON Schema, JSONL event output and stable exit codes. Provider retries honor Retry-After, attempt and elapsed-time limits. A stream interrupted after partial output returns a typed ambiguous result and does not silently replay. |

## Integration contracts

- The root implementation owns Agent integration, Provider recovery, shared runtime,
  Textual/headless assembly, automation output and final CLI routing.
- Plan implementation owns `planning/`, permission extensions, plan tools/dialogs
  and its focused tests. Agent integration invokes its gate at the common final
  execution boundary, including SubAgent execution paths.
- Governance implementation owns `governance/`, memory and Skill adapters and its
  focused tests. The runtime calls its context refresh before model requests; no
  legacy markdown recall path may bypass quarantine or revocation.
- DAG implementation owns `dag/` and focused tests. It uses the existing Agent with
  typed submission and capability-filtered tools; it does not substitute fake
  callbacks for the product execution path.
- Each module exposes CLI adapters for explicit user operations; the central CLI
  wires them into one `eviforge` command. No implicit public upload is part of this
  implementation request.

## Required verification

1. Preserve and rerun the complete 720-test LikeCC regression baseline and the
   separate 90-check SubAgent script after package renaming.
2. Add meaningful behavior tests for each new boundary: session/turn/hash/expiry
   mismatches, argv/path/host changes, approval consumption, permission denial,
   fair memory budgets, quarantine/publish/revoke/rollback, DAG schemas/graphs,
   concurrency/conflicts/artifacts/drift/resume and writable replay refusal.
3. Run real local file/Bash/Git/MCP and Textual flows with deterministic model
   fakes, and Provider SDK tests using local HTTP/SSE transports. No paid model
   access is required for deterministic acceptance.
4. Exercise CLI text/JSON/JSONL results and exit codes, cancellation and background
   cleanup; verify both TUI and headless have the declared services installed.
5. Build wheel and sdist; install the wheel outside the checkout and verify entry
   points and all built-in resource files. Confirm original LikeCC is unchanged.
6. Publish local reports with actual measured results, implementation locations,
   compatibility changes and limits. Distinguish deterministic behavior checks
   from live model evaluation and OS-level sandbox claims.

## Design review

The domain designs are reviewed against the execution entry points before edits.
Cross-review must specifically consider stale authorization after await, legacy
context bypasses, incomplete streams, graph drift, completed node replay and
uncollected child tasks. Findings and resolution will be recorded below.

Reviewed implementation decisions:

- Keep one ReAct loop with a headless event adapter; instantiate all common
  services in one runtime factory. Direct legacy APIs remain testable, while
  product entry points always use governed memory and skills.
- Bind Plan policy to Agent identity even when trace IDs change. Grants remain
  audience-specific and are checked again at the final invocation boundary;
  registry changes while awaiting approval also invalidate execution.
- Refresh governed context before both inference and compaction. Never summarize
  re-injectable governed blocks into unmanaged memory; explicitly revoke recovery
  snapshots and independent skill-fork messages.
- Persist sessions by message identity, not mutable history offsets. Do not let
  notification continuations override a failed or ambiguous main run.
- Treat partial text, thinking and tool streams as observable output. Reject bad
  tool JSON and incomplete terminal states; disable SDK nested retries.
- Validate DAG checkpoint evidence and input hashes before accepting a completed
  node. Record running state before effects, and retain uncertainty if evidence,
  audit events or output submission cannot be persisted after an effect.
- Own MCP transports in their creating task, reap cancelled processes, and make
  concurrent runtime shutdown idempotent.

The concrete defects, regression evidence and scope limits are recorded in
`eviforge-improvement-report.md`; final measured results are in `verification.md`.
