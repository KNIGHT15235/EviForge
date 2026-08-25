from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

from mewcode.hooks.executors import execute_action
from mewcode.hooks.models import (
    Action,
    ActionResult,
    ActionStatus,
    Hook,
    HookContext,
    ToolRejectedError,
)

if TYPE_CHECKING:
    from mewcode.execution import ExecutionContext, ExecutionGateway

log = logging.getLogger(__name__)


@dataclass
class HookNotification:
    hook_id: str
    event: str
    output: str
    success: bool
    status: ActionStatus | None = None
    elapsed_ms: int = 0
    error_code: str | None = None
    truncated: bool = False


class HookEngine:
    def __init__(self, hooks: list[Hook] | None = None) -> None:
        self.hooks: list[Hook] = hooks or []
        self._prompt_messages: list[str] = []
        self._notifications: list[HookNotification] = []
        self._execution_context_provider: Callable[[], ExecutionContext | None] | None = None
        self._execution_gateway: ExecutionGateway | None = None
        self._background_tasks: set[asyncio.Task[None]] = set()

    def bind_execution_policy(
        self,
        *,
        context_provider: Callable[[], ExecutionContext | None],
        gateway: ExecutionGateway | None = None,
    ) -> None:
        """Bind hooks to the host-owned, dynamically resolved execution scope.

        Interactive approval replaces ExecutionContext objects, so the provider
        is evaluated for every action instead of retaining a stale snapshot.
        Raw command/HTTP hooks do not expose a typed, invocation-scoped approval
        channel.  Once a reviewed manifest is active they therefore fail closed.
        ``gateway`` is retained as an ownership marker for a future typed hook
        adapter; it never implicitly authorizes a shell command or URL request.
        """

        if not callable(context_provider):
            raise TypeError("context_provider must be callable")
        self._execution_context_provider = context_provider
        self._execution_gateway = gateway


    def find_matching_hooks(self, event: str, ctx: HookContext) -> list[Hook]:
        matched: list[Hook] = []
        for hook in self.hooks:
            if hook.event != event:
                continue
            if not hook.should_run():
                continue
            if hook.condition is not None and not hook.condition.evaluate(ctx):
                continue
            matched.append(hook)
        return matched


    async def run_hooks(self, event: str, ctx: HookContext) -> None:
        matched = self.find_matching_hooks(event, ctx)
        for hook in matched:
            hook.mark_executed()
            if hook.async_exec:
                task = asyncio.create_task(
                    self._run_single(hook, ctx),
                    name=f"eviforge-hook-{hook.id}",
                )
                self._background_tasks.add(task)
                task.add_done_callback(self._background_tasks.discard)
            else:
                await self._run_single(hook, ctx)

    async def shutdown(self, timeout: float = 1.0) -> None:
        """Drain or cancel async hooks and their subprocess trees."""

        tasks = tuple(self._background_tasks)
        if not tasks:
            return
        _, pending = await asyncio.wait(tasks, timeout=max(0.0, timeout))
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._background_tasks.difference_update(tasks)


    async def _run_single(self, hook: Hook, ctx: HookContext) -> None:
        try:
            result = await self._execute_action(hook.action, ctx)
            if hook.action.type == "prompt" and result.success:
                self._prompt_messages.append(result.output)
            self._notifications.append(
                HookNotification(
                    hook_id=hook.id,
                    event=hook.event,
                    output=result.output,
                    success=result.success,
                    status=result.status,
                    elapsed_ms=result.elapsed_ms,
                    error_code=result.error_code,
                    truncated=result.truncated,
                )
            )
            if not result.success:
                log.warning(
                    "Hook '%s' action failed: %s", hook.id, result.output
                )
        except Exception as e:
            log.warning("Hook '%s' execution error: %s", hook.id, e)
            self._notifications.append(
                HookNotification(
                    hook_id=hook.id,
                    event=hook.event,
                    output=str(e),
                    success=False,
                    status=ActionStatus.FAILED,
                    error_code="hook.execution_error",
                )
            )


    async def run_pre_tool_hooks(
        self, ctx: HookContext
    ) -> ToolRejectedError | None:
        matched = self.find_matching_hooks("pre_tool_use", ctx)
        for hook in matched:
            hook.mark_executed()
            try:
                result = await self._execute_action(hook.action, ctx)
                self._notifications.append(
                    HookNotification(
                        hook_id=hook.id,
                        event="pre_tool_use",
                        output=result.output,
                        success=result.success,
                        status=result.status,
                        elapsed_ms=result.elapsed_ms,
                        error_code=result.error_code,
                        truncated=result.truncated,
                    )
                )
                if hook.reject:
                    return ToolRejectedError(
                        tool=ctx.tool_name,
                        reason=result.output,
                        hook_id=hook.id,
                    )
            except Exception as e:
                log.warning("Hook '%s' execution error: %s", hook.id, e)
        return None

    async def _execute_action(self, action: Action, ctx: HookContext) -> ActionResult:
        blocked = self._planned_side_effect_error(action)
        if blocked is not None:
            return ActionResult(output=blocked, success=False)
        return await execute_action(action, ctx)

    def _planned_side_effect_error(self, action: Action) -> str | None:
        if action.type not in {"command", "http"}:
            return None
        provider = self._execution_context_provider
        if provider is None:
            # Preserve compatibility for an unbound legacy HookEngine.
            return None
        try:
            context = provider()
        except Exception as exc:
            return (
                "Hook side effect blocked: execution context lookup failed "
                f"({type(exc).__name__})"
            )
        if context is None or getattr(context, "plan_hash", "unplanned") == "unplanned":
            return None
        return (
            f"Hook {action.type} side effect blocked by reviewed Plan: "
            "raw hook actions are not represented by an invocation-bound manifest grant"
        )

    def get_prompt_messages(self) -> list[str]:
        messages = list(self._prompt_messages)
        self._prompt_messages.clear()
        return messages


    def drain_notifications(self) -> list[HookNotification]:
        notifications = list(self._notifications)
        self._notifications.clear()
        return notifications
