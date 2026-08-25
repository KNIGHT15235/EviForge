from __future__ import annotations

import asyncio
import logging
import os
import signal
import subprocess
import time

import httpx

from mewcode.hooks.models import Action, ActionResult, ActionStatus, HookContext

log = logging.getLogger(__name__)

MAX_HOOK_OUTPUT_CHARS = 4_000


async def _terminate_process_tree(
    proc: asyncio.subprocess.Process,
) -> None:
    """Terminate the shell and descendants without leaving inherited pipes open."""

    if proc.returncode is not None:
        return
    if os.name == "nt":
        # CREATE_NEW_PROCESS_GROUP lets Ctrl-Break reach cmd.exe and every
        # console child it started.  Unlike taskkill /T this does not require
        # elevated process-enumeration rights in locked-down Windows hosts.
        try:
            proc.send_signal(signal.CTRL_BREAK_EVENT)
            await asyncio.wait_for(proc.wait(), timeout=1.0)
        except (AttributeError, OSError, ProcessLookupError, asyncio.TimeoutError):
            killer = await asyncio.create_subprocess_exec(
                "taskkill.exe",
                "/PID",
                str(proc.pid),
                "/T",
                "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await killer.communicate()
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if proc.returncode is None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
    await proc.wait()


async def execute_command(action: Action, ctx: HookContext) -> ActionResult:
    command = ctx.expand(action.command)
    try:
        process_options: dict[str, object] = {}
        if os.name == "nt":
            process_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            process_options["start_new_session"] = True
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            **process_options,
        )
        communication = asyncio.create_task(proc.communicate())
        try:
            # shield prevents wait_for from spending the entire child runtime
            # cancelling communicate before control reaches the tree killer.
            stdout, _ = await asyncio.wait_for(
                asyncio.shield(communication), timeout=action.timeout
            )
        except asyncio.TimeoutError:
            await _terminate_process_tree(proc)
            await asyncio.gather(communication, return_exceptions=True)
            return ActionResult(
                output=f"Command timed out after {action.timeout}s: {command}",
                success=False,
                status=ActionStatus.TIMED_OUT,
                error_code="hook.command_timeout",
            )
        except asyncio.CancelledError:
            await _terminate_process_tree(proc)
            await asyncio.gather(communication, return_exceptions=True)
            raise
        output = stdout.decode(errors="replace").strip() if stdout else ""
        success = proc.returncode == 0
        return ActionResult(
            output=output,
            success=success,
            error_code=None if success else "hook.command_exit_nonzero",
        )
    except Exception as e:
        return ActionResult(
            output=f"Command execution error: {e}",
            success=False,
            error_code="hook.command_error",
        )


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


    try:
        # The outer deadline is a total wall-clock budget; httpx's timeout also
        # bounds individual connect/read/write/pool operations.  Using a native
        # async client means HookEngine.shutdown() can cancel the request and
        # close its connection instead of leaving a worker thread behind.
        async with asyncio.timeout(action.timeout):
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(action.timeout),
                follow_redirects=False,
            ) as client:
                response = await client.request(
                    method,
                    url,
                    content=body.encode() if body else None,
                    headers=headers,
                )
                response_body = response.content.decode(errors="replace")[:500]
                return ActionResult(
                    output=f"HTTP {response.status_code}: {response_body}",
                    success=200 <= response.status_code < 300,
                    error_code=(
                        None
                        if 200 <= response.status_code < 300
                        else "hook.http_status"
                    ),
                )
    except (TimeoutError, httpx.TimeoutException):
        return ActionResult(
            output=f"HTTP request timed out after {action.timeout}s: {url}",
            success=False,
            status=ActionStatus.TIMED_OUT,
            error_code="hook.http_timeout",
        )
    except httpx.HTTPError as e:
        return ActionResult(
            output=f"HTTP error: {e}",
            success=False,
            error_code="hook.http_error",
        )
    except asyncio.CancelledError:
        raise
    except Exception as e:
        return ActionResult(
            output=f"HTTP error: {e}",
            success=False,
            error_code="hook.http_error",
        )


async def execute_agent(action: Action, ctx: HookContext) -> ActionResult:
    prompt = ctx.expand(action.prompt)
    log.warning("Unsupported Hook agent action rejected (prompt length=%d)", len(prompt))
    return ActionResult(
        output="unsupported_action: Hook agent executor is not implemented",
        success=False,
        status=ActionStatus.UNSUPPORTED,
        error_code="hook.unsupported_action",
    )


_EXECUTOR_MAP = {
    "command": execute_command,
    "prompt": execute_prompt,
    "http": execute_http,
    "agent": execute_agent,
}


async def execute_action(action: Action, ctx: HookContext) -> ActionResult:
    started = time.monotonic()
    executor = _EXECUTOR_MAP.get(action.type)
    if executor is None:
        result = ActionResult(
            output=f"Unknown action type: {action.type}",
            success=False,
            status=ActionStatus.UNSUPPORTED,
            error_code="hook.unknown_action",
        )
    else:
        result = await executor(action, ctx)

    result.elapsed_ms = max(0, round((time.monotonic() - started) * 1_000))
    if len(result.output) > MAX_HOOK_OUTPUT_CHARS:
        omitted = len(result.output) - MAX_HOOK_OUTPUT_CHARS
        result.output = (
            result.output[:MAX_HOOK_OUTPUT_CHARS]
            + f"\n… ({omitted} Hook output characters truncated)"
        )
        result.truncated = True
    return result
