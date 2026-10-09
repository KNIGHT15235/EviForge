"""Local lifecycle actions; Git checkpoints only include attributable verified writes."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

from eviforge.hooks.models import ActionResult, HookContext


def _git(root: Path, *args: str) -> bytes | None:
    try:
        result = subprocess.run(["git", "--literal-pathspecs", "-C", str(root), *args],
                                capture_output=True, timeout=5, check=False)
        return result.stdout if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def _dirty_paths(raw: bytes | None) -> set[str] | None:
    if raw is None:
        return None
    paths = set()
    for record in raw.split(b"\0"):
        if not record:
            continue
        if len(record) < 4 or record[:2] not in {b" M", b" D", b"??"}:
            return None  # staged entries, renames and conflicts need human review
        path = os.fsdecode(record[3:])
        if path == ".eviforge" or path.startswith(".eviforge/"):
            continue
        paths.add(path)
    return paths


def git_baseline(root: Path) -> tuple[str, bool]:
    top = _git(root, "rev-parse", "--show-toplevel")
    head = _git(root, "rev-parse", "--verify", "HEAD")
    if top is None or head is None or Path(os.fsdecode(top).strip()).resolve() != root:
        return "", False
    dirty = _dirty_paths(_git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all"))
    return head.decode().strip(), dirty == set()


def _relative_path(ctx: HookContext) -> str | None:
    from eviforge.hooks.defaults import protected_path
    root = Path(ctx.work_dir).resolve()
    target = Path(ctx.file_path).expanduser()
    if not target.is_absolute():
        target = root / target
    if (not target.resolve().is_relative_to(root) or target.is_symlink()
            or protected_path(target) or protected_path(target.resolve())):
        return None
    return target.resolve().relative_to(root).as_posix()


def _digest(path: Path) -> str | None:
    if not path.is_file() or path.is_symlink() or path.stat().st_size > 2_000_000:
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def remember_before_write(runner, ctx: HookContext) -> None:
    name = _relative_path(ctx)
    if name is not None:
        runner.runs[runner.key(ctx)].before_write[name] = _digest(Path(ctx.work_dir) / name)


def _runtime_path(root: Path, filename: str) -> Path:
    root = root.resolve()
    if not root.is_dir():
        raise ValueError("Workspace is not an existing directory")
    directory = root / ".eviforge" / "hooks"
    destination = directory / filename
    if (not destination.resolve().is_relative_to(root)
            or any(p.is_symlink() for p in (root / ".eviforge", directory, destination))):
        raise ValueError("Hook artifacts must be ordinary files inside the workspace")
    return destination


def _append_record(runner, ctx: HookContext, filename: str, **extra) -> Path:
    destination = _runtime_path(Path(ctx.work_dir), filename)
    destination.parent.mkdir(parents=True, exist_ok=True)
    state = runner.runs[runner.key(ctx)]
    record = {"schema_version": "1.0", "timestamp": time.time(), "event": ctx.event_name,
              "session_id": ctx.session_id, "turn_id": ctx.turn_id, "agent_id": ctx.agent_id,
              "iteration": ctx.iteration, "run_status": ctx.run_status,
              "verification": state.evidence.get("status", "pending"), **extra}
    # Deliberately omit prompt text, tool arguments/output, file contents and configuration.
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(destination, flags, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return destination


async def _auto_commit(runner, ctx: HookContext) -> ActionResult:
    from eviforge.hooks.defaults import _run_argv, _snapshot
    state = runner.runs[runner.key(ctx)]
    root = Path(ctx.work_dir).resolve()

    def skipped(reason: str) -> ActionResult:
        _append_record(runner, ctx, "commits.jsonl", status="skipped", reason=reason)
        return ActionResult("Auto-commit skipped: " + reason)

    if not runner.auto_commit:
        return skipped("disabled by hook_policy.auto_commit")
    if ctx.event_name == "post_tool_use" and ctx.tool_succeeded is not True:
        return skipped("tool did not succeed")
    if ctx.run_status not in {"running", "success"}:
        return skipped("run is not successful/active")
    if not state.git_head or not state.git_clean:
        return skipped("requires a clean repository root with an existing HEAD at task start")
    if not state.owned:
        return skipped("no attributable WriteFile/EditFile changes")
    evidence = await runner._check(ctx)
    if evidence["status"] == "failed" or state.skipped:
        return skipped("verification failed or snapshot coverage is incomplete")
    files, limited = await asyncio.to_thread(_snapshot, root)
    if limited:
        return skipped("snapshot coverage is incomplete")
    dirty = _dirty_paths(await asyncio.to_thread(_git, root, "status", "--porcelain=v1", "-z", "--untracked-files=all"))
    if dirty is None:
        return skipped("index, conflicts or renames require human review")
    if not dirty:
        return skipped("no pending changes")
    if any(name not in state.owned or files.get(name) != state.owned[name] for name in dirty):
        return skipped("unattributed or externally changed files require human review")
    head = await asyncio.to_thread(_git, root, "rev-parse", "HEAD")
    if head is None or head.decode().strip() != state.git_head:
        return skipped("HEAD changed outside this task")
    # No shell interpolation or broad staging; normal Git hooks and signing remain active.
    names = sorted(dirty)
    before_index = await asyncio.to_thread(_git, root, "ls-files", "--stage", "-z", "--", *names)
    if before_index is None:
        return skipped("could not inspect the original index")
    from eviforge.hooks.executors import _await_uninterruptibly
    # Finish bounded staging before propagating cancellation so its index changes can be restored.
    staging = asyncio.create_task(_run_argv(["git", "--literal-pathspecs", "add", "--", *names], root, 10))
    result, cancellation = await _await_uninterruptibly(staging)
    staged_index = await asyncio.to_thread(_git, root, "ls-files", "--stage", "-z", "--", *names)
    committed = False
    try:
        if cancellation is not None:
            raise cancellation
        if result["status"] != "passed":
            return ActionResult("Auto-commit failed: Git could not stage the selected files", success=False)
        result = await _run_argv(["git", "--literal-pathspecs", "commit", "--only", "-m",
                                  "EviForge: checkpoint verified agent changes", "--", *names], root, 20)
        if result["status"] != "passed":
            _append_record(runner, ctx, "commits.jsonl", status="failed", reason="Git commit rejected or timed out")
            return ActionResult("Auto-commit failed: Git commit rejected or timed out; inspect Git hooks/identity", success=False)
        committed = True
        new_head = await asyncio.to_thread(_git, root, "rev-parse", "HEAD")
        state.git_head = new_head.decode().strip() if new_head else ""
        _append_record(runner, ctx, "commits.jsonl", status="committed", commit=state.git_head, files=names)
        return ActionResult("Auto-commit created " + state.git_head[:12] + " (local checkpoint)")
    finally:
        if not committed:
            # Restore only our staging, and only if no other actor changed HEAD or these index entries.
            current_head = await asyncio.to_thread(_git, root, "rev-parse", "HEAD")
            current_index = await asyncio.to_thread(_git, root, "ls-files", "--stage", "-z", "--", *names)
            if (current_head == head and current_index == staged_index and before_index is not None):
                await _run_argv(["git", "--literal-pathspecs", "reset", "-q", "HEAD", "--", *names], root, 5)


async def execute_lifecycle_action(runner, name: str, ctx: HookContext) -> ActionResult:
    state = runner.runs[runner.key(ctx)]
    if name in {"session_safety", "turn_safety"}:
        _runtime_path(Path(ctx.work_dir), "lifecycle.jsonl")
        _append_record(runner, ctx, "lifecycle.jsonl", action="safety_check",
                       auto_commit_eligible=bool(state.git_head and state.git_clean))
        return ActionResult("Safety check: workspace/artifact paths checked; sensitive-file guard and permission policy remain active")
    if name == "post_tool_safety":
        if ctx.tool_succeeded is not True:
            return ActionResult("Safety check skipped: tool did not succeed")
        relative = _relative_path(ctx)
        if relative is not None and relative in state.before_write:
            before = state.before_write.pop(relative)
            expected = state.owned.get(relative, state.baseline.get(relative))
            after = _digest(Path(ctx.work_dir) / relative)
            if before == expected and after is not None:
                state.owned[relative] = after
            else:
                state.owned.pop(relative, None)
        evidence = await runner._check(ctx)
        failed = evidence["status"] == "failed"
        return ActionResult("Post-tool safety/verification " + evidence["status"], success=not failed, blocking=failed)
    if name.startswith("notify_"):
        verification = state.evidence.get("status", "pending")
        _append_record(runner, ctx, "notifications.jsonl", action="local_notification")
        return ActionResult(f"EviForge {ctx.event_name}: {ctx.run_status or 'unknown'}; verification={verification}")
    if name == "log_turn":
        destination = _append_record(runner, ctx, "lifecycle.jsonl", action="turn_log")
        return ActionResult("Turn audit recorded: " + str(destination))
    if name in {"commit_after_tool", "commit_session"}:
        return await _auto_commit(runner, ctx)
    raise ValueError("Unknown lifecycle action")
