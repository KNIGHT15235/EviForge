from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from mewcode.agent import Agent

log = logging.getLogger(__name__)


@dataclass
class ProgressInfo:
    tool_call_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    last_activity: str = ""


@dataclass
class BackgroundTask:
    id: str
    name: str
    agent: Agent
    task: str
    status: str = "running"
    result: str = ""
    start_time: float = field(default_factory=time.monotonic)
    end_time: float | None = None
    cancel: Callable[[], None] | None = None
    on_terminal: Callable[[BackgroundTask], None] | None = None
    progress: ProgressInfo = field(default_factory=ProgressInfo)


@dataclass(frozen=True, slots=True)
class TaskManagerSnapshot:
    """Stable, read-only view used by CLIs and status surfaces.

    Callers must not reach into ``TaskManager``'s asyncio containers: their
    contents change while a background task is completing.  The snapshot is
    deliberately small and JSON-friendly.
    """

    running_ids: tuple[str, ...]
    completed_ids: tuple[str, ...]
    failed_ids: tuple[str, ...]
    cancelled_ids: tuple[str, ...]
    timed_out_ids: tuple[str, ...]

    @property
    def running_count(self) -> int:
        return len(self.running_ids)

    def as_dict(self) -> dict[str, object]:
        return {
            "running_ids": list(self.running_ids),
            "completed_ids": list(self.completed_ids),
            "failed_ids": list(self.failed_ids),
            "cancelled_ids": list(self.cancelled_ids),
            "timed_out_ids": list(self.timed_out_ids),
        }


class TaskManager:


    def __init__(self) -> None:
        self._tasks: dict[str, BackgroundTask] = {}
        self._notify_queue: asyncio.Queue[str] = asyncio.Queue()
        self._async_tasks: dict[str, asyncio.Task[None]] = {}

    @staticmethod
    async def _close_owned_client(agent: Agent) -> None:
        """Release only a client explicitly transferred to this child Agent."""

        namespace = getattr(agent, "__dict__", None)
        client = namespace.get("_owned_llm_client") if isinstance(namespace, dict) else None
        if client is None:
            return
        namespace["_owned_llm_client"] = None
        try:
            from mewcode.client import aclose_client

            await aclose_client(client)
        except BaseException as exc:
            # Cleanup failure must not suppress the task result/notification.
            log.warning("Background Agent client did not close cleanly: %s", exc)


    def launch(
        self,
        agent: Agent,
        task: str,
        name: str = "",
        fork_conversation: Any = None,
        on_terminal: Callable[[BackgroundTask], None] | None = None,
    ) -> str:
        task_id = uuid.uuid4().hex[:8]
        bg = BackgroundTask(
            id=task_id,
            name=name or task_id,
            agent=agent,
            task=task,
            on_terminal=on_terminal,
        )
        self._tasks[task_id] = bg

        async_task = asyncio.create_task(
            self._run_background(task_id, fork_conversation)
        )
        self._async_tasks[task_id] = async_task

        bg.cancel = async_task.cancel
        return task_id


    async def _run_background(
        self, task_id: str, fork_conversation: Any = None
    ) -> None:
        bg = self._tasks.get(task_id)
        if bg is None:
            return

        try:
            if fork_conversation is not None:
                result = await bg.agent.run_to_completion("", fork_conversation)
            else:
                result = await bg.agent.run_to_completion(bg.task)
            bg.result = result
            bg.status = "completed"

            if bg.agent.team_name and bg.agent._team_manager:
                mailbox = bg.agent._team_manager.get_mailbox(bg.agent.team_name)
                if mailbox:
                    from mewcode.teams.mailbox import create_message
                    msg = create_message(
                        from_agent=bg.name,
                        to_agent="lead",
                        content=f"[idle] {bg.name}: completed initial task",
                        summary=f"{bg.name} idle",
                    )
                    mailbox.write("lead", msg)

                    for _ in range(60):
                        await asyncio.sleep(1)
                        msgs = mailbox.consume(bg.agent.agent_id)
                        if not msgs:
                            continue
                        prompt = "\n\n".join(
                            f"[Message from {m.from_agent}] {m.content}" for m in msgs
                        )
                        result = await bg.agent.run_to_completion(prompt)
                        bg.result = result
                        msg = create_message(
                            from_agent=bg.name,
                            to_agent="lead",
                            content=f"[idle] {bg.name}: completed follow-up",
                            summary=f"{bg.name} idle",
                        )
                        mailbox.write("lead", msg)

        except asyncio.CancelledError:
            # Team members may already have completed their assigned work and
            # only be listening for an optional follow-up.  Cancelling that
            # idle listener during headless shutdown must not rewrite a real
            # completed result as a failed task.
            if bg.status == "running":
                bg.status = "cancelled"
                bg.result = "Task was cancelled"
        except Exception as e:
            log.error("Background task %s failed: %s", task_id, e)
            bg.status = "failed"
            bg.result = f"Error: {e}"
        finally:
            await self._close_owned_client(bg.agent)
            bg.end_time = time.monotonic()
            bg.progress.input_tokens = bg.agent.total_input_tokens
            bg.progress.output_tokens = bg.agent.total_output_tokens
            if bg.on_terminal is not None:
                try:
                    bg.on_terminal(bg)
                except Exception as exc:
                    log.warning("Background terminal callback failed: %s", exc)
            self._async_tasks.pop(task_id, None)
            await self._notify_queue.put(task_id)


    def adopt_running(
        self,
        agent: Agent,
        task_description: str,
        partial_result: str = "",
        name: str = "",
        on_terminal: Callable[[BackgroundTask], None] | None = None,
    ) -> str:
        task_id = uuid.uuid4().hex[:8]
        bg = BackgroundTask(
            id=task_id,
            name=name or task_id,
            agent=agent,
            task=task_description,
            result=partial_result,
            on_terminal=on_terminal,
        )
        self._tasks[task_id] = bg

        async_task = asyncio.create_task(self._continue_background(task_id))
        self._async_tasks[task_id] = async_task
        bg.cancel = async_task.cancel
        return task_id


    async def _continue_background(self, task_id: str) -> None:
        bg = self._tasks.get(task_id)
        if bg is None:
            return

        try:
            result = await bg.agent.run_to_completion(bg.task)
            bg.result = (bg.result + "\n" + result).strip() if bg.result else result
            bg.status = "completed"
        except asyncio.CancelledError:
            if bg.status == "running":
                bg.status = "cancelled"
        except Exception as e:
            log.error("Background task %s failed: %s", task_id, e)
            bg.status = "failed"
            bg.result = f"Error: {e}"
        finally:
            await self._close_owned_client(bg.agent)
            bg.end_time = time.monotonic()
            bg.progress.input_tokens = bg.agent.total_input_tokens
            bg.progress.output_tokens = bg.agent.total_output_tokens
            if bg.on_terminal is not None:
                try:
                    bg.on_terminal(bg)
                except Exception as exc:
                    log.warning("Background terminal callback failed: %s", exc)
            self._async_tasks.pop(task_id, None)
            await self._notify_queue.put(task_id)

    def get(self, task_id: str) -> BackgroundTask | None:
        return self._tasks.get(task_id)

    def list_tasks(self) -> list[BackgroundTask]:
        return list(self._tasks.values())

    def cancel(self, task_id: str) -> bool:
        bg = self._tasks.get(task_id)
        if bg is None or bg.status != "running":
            return False
        async_task = self._async_tasks.get(task_id)
        if async_task and not async_task.done():
            async_task.cancel()
            return True
        return False

    def poll_completed(self) -> list[BackgroundTask]:
        completed: list[BackgroundTask] = []
        while not self._notify_queue.empty():
            try:
                task_id = self._notify_queue.get_nowait()
                bg = self._tasks.get(task_id)
                if bg is not None:
                    completed.append(bg)
            except asyncio.QueueEmpty:
                break
        return completed

    def drain_events(self) -> list[BackgroundTask]:
        """Return newly terminal tasks through the public lifecycle API."""

        return self.poll_completed()

    def snapshot(self) -> TaskManagerSnapshot:
        by_status: dict[str, list[str]] = {
            "running": [],
            "completed": [],
            "failed": [],
            "cancelled": [],
            "timed_out": [],
        }
        for task_id, background in self._tasks.items():
            by_status.setdefault(background.status, []).append(task_id)
        return TaskManagerSnapshot(
            running_ids=tuple(sorted(by_status["running"])),
            completed_ids=tuple(sorted(by_status["completed"])),
            failed_ids=tuple(sorted(by_status["failed"])),
            cancelled_ids=tuple(sorted(by_status["cancelled"])),
            timed_out_ids=tuple(sorted(by_status["timed_out"])),
        )

    async def wait_for_idle(self, timeout: float | None = None) -> bool:
        """Wait for the tasks that are currently running without cancelling.

        Returns ``True`` when all observed tasks finish.  New tasks launched by
        a completion callback are picked up on the next loop so callers do not
        accidentally report an idle manager while work is still being added.
        """

        loop = asyncio.get_running_loop()
        deadline = None if timeout is None else loop.time() + max(0.0, timeout)
        while self._async_tasks:
            pending = tuple(self._async_tasks.values())
            remaining = None if deadline is None else max(0.0, deadline - loop.time())
            if remaining == 0.0:
                return False
            _, still_pending = await asyncio.wait(pending, timeout=remaining)
            if still_pending:
                return False
        return True

    async def shutdown(
        self,
        *,
        policy: str = "cancel",
        timeout: float = 2.0,
    ) -> TaskManagerSnapshot:
        """Apply one explicit shutdown policy and return the terminal snapshot.

        ``wait`` gives work up to ``timeout`` seconds, then cancels stragglers.
        ``cancel`` cancels immediately and waits for cleanup.  ``detach`` is an
        explicit opt-out for embedders that keep the event loop alive; the
        process CLI never uses it implicitly.
        """

        if policy not in {"wait", "cancel", "detach"}:
            raise ValueError("background policy must be wait, cancel, or detach")
        if timeout < 0:
            raise ValueError("background timeout must be non-negative")
        if policy == "detach":
            return self.snapshot()

        if policy == "wait":
            if await self.wait_for_idle(timeout=timeout):
                return self.snapshot()
            # Preserve the reason for forced cancellation.  Treating an
            # exhausted wait budget as a user cancellation made structured
            # headless results ambiguous and hid a useful operational signal.
            for task_id, task in tuple(self._async_tasks.items()):
                background = self._tasks.get(task_id)
                if (
                    not task.done()
                    and background is not None
                    and background.status == "running"
                ):
                    background.status = "timed_out"
                    background.result = (
                        f"Task exceeded the {timeout:.2f}s background wait budget"
                    )

        for task in tuple(self._async_tasks.values()):
            if not task.done():
                task.cancel()
        pending = tuple(self._async_tasks.values())
        if pending:
            done, still_pending = await asyncio.wait(pending, timeout=max(timeout, 0.1))
            for task in done:
                # Retrieve unexpected exceptions so event-loop shutdown does
                # not emit an unhelpful "exception was never retrieved" log.
                if not task.cancelled():
                    try:
                        task.exception()
                    except BaseException:
                        pass
            if still_pending:
                log.error(
                    "Background task cleanup exceeded %.2fs: %s",
                    timeout,
                    sorted(
                        task_id
                        for task_id, task in self._async_tasks.items()
                        if task in still_pending
                    ),
                )
        return self.snapshot()
