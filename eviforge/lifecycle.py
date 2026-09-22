"""Public lifecycle ownership for shared interactive/headless services."""
from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from typing import Any


class Lifecycle:
    def __init__(self) -> None:
        self._tasks: set[asyncio.Task] = set()
        self._closed = False
        self.errors: list[str] = []

    def track(self, task: asyncio.Task) -> asyncio.Task:
        if self._closed:
            task.cancel()
        self._tasks.add(task)
        task.add_done_callback(self._done)
        return task

    def create_task(self, awaitable: Awaitable[Any], *, name: str) -> asyncio.Task:
        return self.track(asyncio.create_task(awaitable, name=name))

    def _done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled():
            error = task.exception()
            if error is not None:
                self.errors.append(str(error))

    def pending(self) -> tuple[asyncio.Task, ...]:
        return tuple(task for task in self._tasks if not task.done())

    async def wait(self, timeout: float | None = None) -> None:
        if self.pending():
            _, pending = await asyncio.wait(self.pending(), timeout=timeout)
            if pending:
                raise TimeoutError("Background lifecycle wait timed out")

    async def cancel(self) -> None:
        self._closed = True
        current = asyncio.current_task()
        tasks = tuple(task for task in self.pending() if task is not current)
        self._tasks.discard(current)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


async def close_resource(resource: Any) -> None:
    if resource is None:
        return
    close = getattr(resource, "aclose", None) or getattr(resource, "close", None)
    if close is not None:
        result = close()
        if inspect.isawaitable(result):
            await result
