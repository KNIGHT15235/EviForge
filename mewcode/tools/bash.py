
from __future__ import annotations

import asyncio

from pydantic import BaseModel, Field, model_validator

from mewcode.tools.base import Tool, ToolResult

MAX_TIMEOUT = 600


class Params(BaseModel):
    command: str | None = Field(
        default=None,
        description="Legacy shell command string (forbidden by a reviewed Plan)",
    )
    argv: tuple[str, ...] | None = Field(
        default=None,
        description="Exact executable and arguments; required by reviewed Plans",
    )
    timeout: int = Field(default=120, description="Timeout in seconds (max 600)")

    @model_validator(mode="after")
    def exactly_one_command_form(self) -> "Params":
        has_command = isinstance(self.command, str) and bool(self.command.strip())
        has_argv = self.argv is not None and len(self.argv) > 0
        if has_command == has_argv:
            raise ValueError("provide exactly one of command or argv")
        if self.argv is not None and any(not item or "\x00" in item for item in self.argv):
            raise ValueError("argv entries must be non-empty and contain no NUL")
        return self


class Bash(Tool):
    name = "Bash"
    description = "Execute a shell command and return stdout and stderr."
    params_model = Params
    category = "command"
    supports_exact_argv = True


    async def execute(self, params: Params) -> ToolResult:
        if params.argv is not None:
            return await self.execute_argv(params, params.argv)

        assert params.command is not None
        timeout = min(params.timeout, MAX_TIMEOUT)

        try:
            proc = await asyncio.create_subprocess_shell(
                params.command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return ToolResult(output=f"Error: command timed out after {timeout}s", is_error=True)
        except Exception as e:
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

    async def execute_argv(
        self,
        params: Params,
        argv: tuple[str, ...],
        *,
        cwd: str | None = None,
    ) -> ToolResult:
        """Execute a reviewed argv without invoking a command shell."""

        timeout = min(params.timeout, MAX_TIMEOUT)
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            assert proc is not None
            proc.kill()
            await proc.wait()
            return ToolResult(output=f"Error: command timed out after {timeout}s", is_error=True)
        except Exception as exc:
            return ToolResult(output=f"Error executing argv: {exc}", is_error=True)

        parts: list[str] = []
        if stdout:
            parts.append(f"STDOUT:\n{stdout.decode(errors='replace')}")
        if stderr:
            parts.append(f"STDERR:\n{stderr.decode(errors='replace')}")
        if not parts:
            parts.append("(no output)")
        return ToolResult(output="\n".join(parts), is_error=proc.returncode != 0)
