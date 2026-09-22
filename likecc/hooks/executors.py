from __future__ import annotations

import asyncio
import logging
import os
import signal
import subprocess as sync_subprocess
from typing import TypeVar
from urllib.request import Request, urlopen
from urllib.error import URLError

from likecc.hooks.models import Action, ActionResult, HookContext

log = logging.getLogger(__name__)

_T = TypeVar("_T")


def _shell_process_options() -> dict[str, object]:
    if os.name == "posix":
        return {"start_new_session": True}
    if os.name == "nt":
        creation_flags = getattr(sync_subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        return {"creationflags": creation_flags}
    return {}


def _terminate_process_tree(proc: asyncio.subprocess.Process) -> None:
    """Terminate a shell and the descendants created for its command."""
    if os.name == "posix":
        try:
            os.killpg(proc.pid, signal.SIGKILL)
            return
        except ProcessLookupError:
            return
        except OSError:
            # Fall back to killing the direct child below.
            pass
    elif os.name == "nt":
        try:
            sync_subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                stdin=sync_subprocess.DEVNULL,
                stdout=sync_subprocess.DEVNULL,
                stderr=sync_subprocess.DEVNULL,
                check=False,
                timeout=5,
            )
        except (OSError, sync_subprocess.TimeoutExpired):
            pass

    if proc.returncode is None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass


async def _await_uninterruptibly(
    task: asyncio.Task[_T],
) -> tuple[_T, asyncio.CancelledError | None]:
    """Finish a resource task and report cancellation received while waiting."""
    cancellation: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            cancellation = exc
            continue
    return task.result(), cancellation


async def _drain_process(
    communicate_task: asyncio.Task[tuple[bytes, bytes | None]],
    *,
    suppress_cancellation: bool = False,
) -> tuple[bytes, bytes | None]:
    cancellation: asyncio.CancelledError | None = None
    try:
        result, cancellation = await _await_uninterruptibly(communicate_task)
    except Exception as exc:
        log.warning("Failed to drain hook command subprocess: %s", exc)
        result = (b"", None)
    if cancellation is not None and not suppress_cancellation:
        raise cancellation
    return result


async def _reap_process(
    proc: asyncio.subprocess.Process,
    *,
    suppress_cancellation: bool = False,
) -> None:
    """Wait for a process whose communicate task failed unexpectedly."""
    wait_task = asyncio.create_task(proc.wait(), name="hook-command-wait")
    cancellation: asyncio.CancelledError | None = None
    try:
        _, cancellation = await _await_uninterruptibly(wait_task)
    except Exception as exc:
        log.warning("Failed to reap hook command subprocess: %s", exc)
    if cancellation is not None and not suppress_cancellation:
        raise cancellation


async def execute_command(action: Action, ctx: HookContext) -> ActionResult:
    command = ctx.expand(action.command)
    spawn_task = asyncio.create_task(
        asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=ctx.work_dir or None,
            **_shell_process_options(),
        ),
        name="hook-command-spawn",
    )
    try:
        # Shield process creation so cancellation cannot strand a partially
        # connected asyncio subprocess transport (CPython gh-103847).
        proc = await asyncio.shield(spawn_task)
    except asyncio.CancelledError:
        try:
            proc, _ = await _await_uninterruptibly(spawn_task)
        except Exception as exc:
            log.warning("Hook command spawn failed during cancellation: %s", exc)
        else:
            _terminate_process_tree(proc)
            communicate_task = asyncio.create_task(
                proc.communicate(), name="hook-command-cancel-drain"
            )
            await _drain_process(
                communicate_task, suppress_cancellation=True
            )
        raise
    except Exception as exc:
        return ActionResult(output=f"Command execution error: {exc}", success=False)

    communicate_task = asyncio.create_task(
        proc.communicate(), name="hook-command-communicate"
    )
    try:
        try:
            stdout, _ = await asyncio.wait_for(
                asyncio.shield(communicate_task), timeout=action.timeout
            )
        except asyncio.TimeoutError:
            _terminate_process_tree(proc)
            await _drain_process(communicate_task)
            return ActionResult(
                output=f"Command timed out after {action.timeout}s: {command}",
                success=False,
            )
        except asyncio.CancelledError:
            _terminate_process_tree(proc)
            await _drain_process(
                communicate_task, suppress_cancellation=True
            )
            raise
        output = stdout.decode(errors="replace").strip() if stdout else ""
        return ActionResult(output=output, success=proc.returncode == 0)
    except Exception as exc:
        _terminate_process_tree(proc)
        await _drain_process(communicate_task)
        if proc.returncode is None:
            await _reap_process(proc)
        return ActionResult(output=f"Command execution error: {exc}", success=False)


async def execute_prompt(action: Action, ctx: HookContext) -> ActionResult:
    message = ctx.expand(action.message)
    return ActionResult(output=message, success=True)


async def execute_http(action: Action, ctx: HookContext) -> ActionResult:
    url = ctx.expand(action.url)
    body = ctx.expand(action.body) if action.body else None
    method = action.method or "POST"

    headers = dict(action.headers)
    for k, v in headers.items():
        headers[k] = ctx.expand(v)
    if body and "Content-Type" not in headers:
        headers["Content-Type"] = "application/json"


    def _do_request() -> ActionResult:
        try:
            data = body.encode() if body else None
            req = Request(url, data=data, headers=headers, method=method)
            with urlopen(req, timeout=30) as resp:
                resp_body = resp.read().decode(errors="replace")[:500]
                return ActionResult(
                    output=f"HTTP {resp.status}: {resp_body}",
                    success=200 <= resp.status < 300,
                )
        except URLError as e:
            return ActionResult(output=f"HTTP error: {e}", success=False)
        except Exception as e:
            return ActionResult(output=f"HTTP error: {e}", success=False)

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _do_request)


async def execute_agent(action: Action, ctx: HookContext) -> ActionResult:
    prompt = ctx.expand(action.prompt)
    log.info("Agent executor stub called with prompt: %s", prompt[:100])
    return ActionResult(
        output="agent executor not yet implemented",
        success=False,
    )


_EXECUTOR_MAP = {
    "command": execute_command,
    "prompt": execute_prompt,
    "http": execute_http,
    "agent": execute_agent,
}


async def execute_action(action: Action, ctx: HookContext) -> ActionResult:
    executor = _EXECUTOR_MAP.get(action.type)
    if executor is None:
        return ActionResult(
            output=f"Unknown action type: {action.type}",
            success=False,
        )
    return await executor(action, ctx)
