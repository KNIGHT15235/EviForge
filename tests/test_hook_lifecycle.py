from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from eviforge.app import EviForgeApp
from eviforge.hooks import Action, ActionResult, Hook, HookEngine


@pytest.mark.asyncio
async def test_app_shutdown_hooks_is_idempotent_and_waits_for_async_hook():
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def controlled_action(action, ctx):
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return ActionResult(output="done", success=True)

    engine = HookEngine(
        [
            Hook(
                id="shutdown",
                event="shutdown",
                action=Action(type="command", command="unused"),
                async_exec=True,
            )
        ]
    )
    app = EviForgeApp(providers=[], hook_engine=engine)

    with patch("eviforge.hooks.engine.execute_action", new=controlled_action):
        shutdown_task = asyncio.create_task(app._shutdown_hooks())
        await asyncio.wait_for(started.wait(), timeout=1)
        assert not shutdown_task.done()
        release.set()
        await asyncio.wait_for(shutdown_task, timeout=1)
        await asyncio.wait_for(app._shutdown_hooks(), timeout=1)

    assert calls == 1
    assert app._hooks_closed is True
    assert engine._background_tasks == set()


@pytest.mark.asyncio
async def test_app_shutdown_timeout_cancels_and_reaps_startup_task():
    started = asyncio.Event()
    finalized = asyncio.Event()

    async def blocked_startup():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            finalized.set()

    engine = HookEngine([])
    app = EviForgeApp(providers=[], hook_engine=engine)
    app._hook_startup_task = asyncio.create_task(blocked_startup())
    await asyncio.wait_for(started.wait(), timeout=1)

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(app._shutdown_hooks(), timeout=0.01)

    assert finalized.is_set()
    assert app._hook_startup_task is None
    assert app._hooks_closed is True


@pytest.mark.asyncio
async def test_noninteractive_prompt_runs_hook_cleanup_in_same_loop():
    from eviforge import __main__ as entrypoint

    engine = HookEngine(
        [
            Hook(
                id="startup",
                event="startup",
                action=Action(type="prompt", message="started"),
            ),
            Hook(
                id="shutdown",
                event="shutdown",
                action=Action(type="prompt", message="stopped"),
            ),
        ]
    )

    async def fail_prompt(*args, **kwargs):
        raise RuntimeError("prompt failed")

    with patch("eviforge.__main__._run_prompt", new=fail_prompt):
        with pytest.raises(RuntimeError, match="prompt failed"):
            await entrypoint._run_prompt_with_hook_cleanup(
                config=None,
                permission_mode=None,
                hook_engine=engine,
                prompt="hello",
            )

    assert [note.event for note in engine.drain_notifications()] == [
        "startup",
        "shutdown",
    ]
    assert engine._background_tasks == set()
