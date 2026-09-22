from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from likecc.hooks.executors import execute_action
from likecc.hooks.models import ActionResult, Hook, HookContext, ToolRejectedError

log = logging.getLogger(__name__)


@dataclass
class HookNotification:
    hook_id: str
    event: str
    output: str
    success: bool


class HookEngine:
    def __init__(self, hooks: list[Hook] | None = None) -> None:
        self.hooks: list[Hook] = hooks or []
        self._prompt_messages: list[str] = []
        self._notifications: list[HookNotification] = []
        self._background_tasks: set[asyncio.Task[None]] = set()


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
                    name=f"hook-{hook.id}",
                )
                self._background_tasks.add(task)
                task.add_done_callback(self._background_task_done)
            else:
                await self._run_single(hook, ctx)

    def _background_task_done(self, task: asyncio.Task[None]) -> None:
        self._background_tasks.discard(task)
        if task.cancelled():
            return

        exception = task.exception()
        if exception is not None:
            log.error(
                "Unhandled background hook task error",
                exc_info=(type(exception), exception, exception.__traceback__),
            )

    @staticmethod
    def _raise_background_task_errors(
        tasks: set[asyncio.Task[None]],
    ) -> None:
        for task in tasks:
            if not task.cancelled():
                task.result()

    async def wait_for_background_hooks(self) -> None:
        """Wait until all currently queued asynchronous hooks are reaped."""
        while self._background_tasks:
            tasks = set(self._background_tasks)
            done, _ = await asyncio.wait(tasks)
            self._raise_background_task_errors(done)

    async def cancel_background_hooks(self) -> None:
        """Cancel and reap all currently queued asynchronous hooks."""
        cancellation: asyncio.CancelledError | None = None
        while self._background_tasks:
            tasks = set(self._background_tasks)
            for task in tasks:
                task.cancel()
            waiter = asyncio.create_task(
                asyncio.wait(tasks), name="hook-cancellation-wait"
            )
            while not waiter.done():
                try:
                    await asyncio.shield(waiter)
                except asyncio.CancelledError as exc:
                    cancellation = exc
            done, _ = waiter.result()
            self._raise_background_task_errors(done)
        if cancellation is not None:
            raise cancellation


    async def _run_single(self, hook: Hook, ctx: HookContext) -> None:
        try:
            result = await execute_action(hook.action, ctx)
            if hook.action.type == "prompt" and result.success:
                self._prompt_messages.append(result.output)
            self._notifications.append(
                HookNotification(
                    hook_id=hook.id,
                    event=hook.event,
                    output=result.output,
                    success=result.success,
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
                )
            )


    async def run_pre_tool_hooks(
        self, ctx: HookContext
    ) -> ToolRejectedError | None:
        matched = self.find_matching_hooks("pre_tool_use", ctx)
        for hook in matched:
            hook.mark_executed()
            try:
                result = await execute_action(hook.action, ctx)
                self._notifications.append(
                    HookNotification(
                        hook_id=hook.id,
                        event="pre_tool_use",
                        output=result.output,
                        success=result.success,
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

    def get_prompt_messages(self) -> list[str]:
        messages = list(self._prompt_messages)
        self._prompt_messages.clear()
        return messages


    def drain_notifications(self) -> list[HookNotification]:
        notifications = list(self._notifications)
        self._notifications.clear()
        return notifications
