
from __future__ import annotations

import asyncio

from pydantic import BaseModel, Field, model_validator

from eviforge.tools.base import Tool, ToolResult
from eviforge.tools.work_dir import get_tool_work_dir
from eviforge.hooks.executors import (
    _await_uninterruptibly, _drain_process, _shell_process_options, _terminate_process_tree,
)

MAX_TIMEOUT = 600


class Params(BaseModel):
    command: str | None = Field(default=None, description="Legacy shell command; unavailable for exact plan authorization")
    argv: list[str] | None = Field(default=None, description="Exact executable and argument array; executed directly without a shell")
    timeout: int = Field(default=120, description="Timeout in seconds (max 600)")

    @model_validator(mode="after")
    def one_command(self) -> "Params":
        if (self.command is None) == (self.argv is None):
            raise ValueError("Provide exactly one of command or argv")
        if self.command is not None and not self.command.strip():
            raise ValueError("command must not be empty")
        if self.argv is not None and (not self.argv or not self.argv[0] or any("\x00" in arg for arg in self.argv)):
            raise ValueError("argv must contain an executable and no NUL bytes")
        if self.timeout <= 0:
            raise ValueError("timeout must be positive")
        return self


class Bash(Tool):
    name = "Bash"
    description = "Execute argv directly, or a legacy shell command, and return stdout/stderr. Exact argv authorization controls process startup, not the process's filesystem or network effects."
    params_model = Params
    category = "command"


    async def execute(self, params: Params) -> ToolResult:
        timeout = min(params.timeout, MAX_TIMEOUT)
        options = dict(stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                       cwd=str(get_tool_work_dir()), **_shell_process_options())
        spawn = (asyncio.create_subprocess_exec(*params.argv, **options) if params.argv is not None
                 else asyncio.create_subprocess_shell(params.command, **options))
        spawn_task = asyncio.create_task(spawn, name="bash-spawn")
        try:
            proc = await asyncio.shield(spawn_task)
        except asyncio.CancelledError:
            try:
                proc, _ = await _await_uninterruptibly(spawn_task)
            except Exception:
                pass
            else:
                _terminate_process_tree(proc)
                await _drain_process(asyncio.create_task(proc.communicate()), suppress_cancellation=True)
            raise
        except Exception as exc:
            return ToolResult(output=f"Error executing command: {exc}", is_error=True)

        communicate = asyncio.create_task(proc.communicate(), name="bash-drain")
        try:
            stdout, stderr = await asyncio.wait_for(asyncio.shield(communicate), timeout=timeout)
        except asyncio.TimeoutError:
            _terminate_process_tree(proc)
            await _drain_process(communicate)
            return ToolResult(output=f"Error: command timed out after {timeout}s", is_error=True)
        except asyncio.CancelledError:
            _terminate_process_tree(proc)
            await _drain_process(communicate, suppress_cancellation=True)
            raise
        except Exception as e:
            _terminate_process_tree(proc)
            await _drain_process(communicate)
            return ToolResult(output=f"Error executing command: {e}", is_error=True)

        parts: list[str] = []
        if stdout:
            parts.append(f"STDOUT:\n{stdout.decode(errors='replace')}")
        if stderr:
            parts.append(f"STDERR:\n{stderr.decode(errors='replace')}")
        if not parts:
            parts.append("(no output)")

        output = "\n".join(parts)
        return ToolResult(output=output, is_error=proc.returncode != 0)
