from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import mewcode.__main__ as cli
from mewcode.app import EviForgeApp
from mewcode.agents.task_manager import TaskManager
from mewcode.client import LLMClient, resolve_context_window
from mewcode.tools.agent_tool import AgentTool, AgentToolParams


class OwnedTestClient(LLMClient):
    def __init__(self) -> None:
        self.transport = AsyncMock()
        self._client = self.transport

    async def stream(self, conversation, system="", tools=None):
        if False:
            yield


@pytest.mark.asyncio
async def test_llm_client_close_and_aclose_share_one_idempotent_close() -> None:
    client = OwnedTestClient()

    await asyncio.gather(client.close(), client.aclose(), client.close())

    client.transport.close.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_context_window_probe_closes_its_short_lived_client(monkeypatch) -> None:
    client = AsyncMock()
    client.fetch_model_context_window.return_value = 321_000
    provider = SimpleNamespace(
        context_window=0,
        _fetched_context_window=0,
        protocol="anthropic",
        set_fetched_context_window=AsyncMock(),
    )
    # The real config mutator is synchronous.
    provider.set_fetched_context_window = lambda value: setattr(
        provider, "_fetched_context_window", value
    )
    monkeypatch.setattr("mewcode.client.create_client", lambda config: client)

    await resolve_context_window(provider)

    assert provider._fetched_context_window == 321_000
    client.aclose.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_headless_prompt_closes_owned_client_after_bootstrap_failure(
    monkeypatch,
) -> None:
    client = AsyncMock()
    provider = SimpleNamespace()
    config = SimpleNamespace(providers=(provider,))
    monkeypatch.setattr("mewcode.client.create_client", lambda config: client)
    monkeypatch.setattr(
        "mewcode.client.resolve_context_window", AsyncMock(side_effect=RuntimeError("boom"))
    )

    with pytest.raises(RuntimeError, match="boom"):
        await cli._run_prompt(config, "default", None, "hello")

    client.aclose.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_tui_closes_only_clients_it_created(monkeypatch) -> None:
    owned = AsyncMock()
    external = AsyncMock()
    app = EviForgeApp(providers=[])
    app.client = external
    app._owned_clients.add(owned)

    await app._close_owned_clients()
    await app._close_owned_clients()

    owned.aclose.assert_awaited_once_with()
    external.aclose.assert_not_called()


@pytest.mark.asyncio
async def test_tui_shutdown_fallback_closes_owned_client(monkeypatch) -> None:
    owned = AsyncMock()
    app = EviForgeApp(providers=[])
    app._owned_clients.add(owned)
    base_shutdown = AsyncMock()
    monkeypatch.setattr("textual.app.App._shutdown", base_shutdown)

    await app._shutdown()

    owned.aclose.assert_awaited_once_with()
    base_shutdown.assert_awaited_once_with()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["completed", "failed", "cancelled"])
async def test_task_manager_closes_only_explicitly_owned_child_client(outcome) -> None:
    owned = AsyncMock()
    agent = SimpleNamespace(
        total_input_tokens=0,
        total_output_tokens=0,
        team_name="",
        _team_manager=None,
        _owned_llm_client=owned,
    )

    async def run(task):
        if outcome == "failed":
            raise RuntimeError("boom")
        if outcome == "cancelled":
            await asyncio.sleep(60)
        return "done"

    agent.run_to_completion = run
    manager = TaskManager()
    task_id = manager.launch(agent, "task")
    if outcome == "cancelled":
        await asyncio.sleep(0)
        assert manager.cancel(task_id)
    await asyncio.wait_for(
        asyncio.gather(*tuple(manager._async_tasks.values()), return_exceptions=True),
        timeout=2,
    )

    owned.aclose.assert_awaited_once_with()
    assert agent._owned_llm_client is None


@pytest.mark.asyncio
async def test_task_manager_does_not_close_inherited_or_mock_client() -> None:
    inherited = AsyncMock()
    agent = SimpleNamespace(
        client=inherited,
        total_input_tokens=0,
        total_output_tokens=0,
        team_name="",
        _team_manager=None,
        run_to_completion=AsyncMock(return_value="done"),
    )
    manager = TaskManager()

    manager.launch(agent, "task")
    await asyncio.wait_for(
        asyncio.gather(*tuple(manager._async_tasks.values()), return_exceptions=True),
        timeout=2,
    )

    inherited.aclose.assert_not_called()


def test_agent_tool_model_selection_reports_transport_ownership() -> None:
    parent_client = object()
    override_client = object()
    parent = SimpleNamespace(client=parent_client)
    tool = AgentTool(
        agent_loader=SimpleNamespace(),
        task_manager=SimpleNamespace(),
        trace_manager=SimpleNamespace(),
        parent_agent=parent,
    )
    definition = SimpleNamespace(model="inherit")

    inherited, inherited_owned = tool._select_llm_with_ownership(
        AgentToolParams(prompt="x", description="x"), definition
    )
    tool._create_client_for_model = lambda model: override_client
    overridden, overridden_owned = tool._select_llm_with_ownership(
        AgentToolParams(prompt="x", description="x", model="haiku"), definition
    )

    assert inherited is parent_client
    assert not inherited_owned
    assert overridden is override_client
    assert overridden_owned
