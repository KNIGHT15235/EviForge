from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import mewcode.client as client_module
from mewcode.client import (
    AmbiguousStreamError,
    AuthenticationError,
    LLMClient,
    NetworkError,
    OpenAIClient,
    RateLimitError,
    ResilientLLMClient,
    RetryPolicy,
)
from mewcode.config import ProviderConfig
from mewcode.conversation import ConversationManager
from mewcode.tools.base import StreamEnd, TextDelta


class ScriptedClient(LLMClient):
    def __init__(self, attempts: list[list[object]]) -> None:
        self.attempts = attempts
        self.calls = 0
        self.closed = 0

    async def stream(self, conversation, system="", tools=None):
        script = self.attempts[self.calls]
        self.calls += 1
        for item in script:
            if isinstance(item, BaseException):
                raise item
            yield item

    async def _close_transport(self) -> None:
        self.closed += 1


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.delays: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.delays.append(delay)
        self.now += delay


@pytest.mark.asyncio
async def test_network_failures_retry_with_bounded_exponential_backoff() -> None:
    inner = ScriptedClient(
        [
            [NetworkError("first")],
            [NetworkError("second")],
            [TextDelta("ok"), StreamEnd("end_turn")],
        ]
    )
    clock = FakeClock()
    client = ResilientLLMClient(
        inner,
        RetryPolicy(
            max_attempts=3,
            base_delay=1,
            max_delay=5,
            max_elapsed=10,
            jitter_ratio=0,
        ),
        sleep=clock.sleep,
        clock=clock.monotonic,
    )

    events = [event async for event in client.stream(ConversationManager())]

    assert [event.text for event in events if isinstance(event, TextDelta)] == ["ok"]
    assert inner.calls == 3
    assert clock.delays == [1, 2]
    assert [
        event["decision"] for event in client.drain_diagnostic_events()
    ] == ["retry_scheduled", "retry_scheduled"]


@pytest.mark.asyncio
async def test_retry_after_is_a_floor_for_backoff() -> None:
    inner = ScriptedClient(
        [
            [RateLimitError("limited", retry_after=4.0)],
            [TextDelta("ok")],
        ]
    )
    clock = FakeClock()
    client = ResilientLLMClient(
        inner,
        RetryPolicy(
            max_attempts=2,
            base_delay=0.1,
            max_delay=1,
            max_elapsed=10,
            jitter_ratio=0,
        ),
        sleep=clock.sleep,
        clock=clock.monotonic,
    )

    _ = [event async for event in client.stream(ConversationManager())]

    assert clock.delays == [4.0]


@pytest.mark.asyncio
async def test_retry_stops_when_total_time_budget_cannot_fit_delay() -> None:
    error = RateLimitError("limited", retry_after=5.0)
    inner = ScriptedClient([[error]])
    clock = FakeClock()
    client = ResilientLLMClient(
        inner,
        RetryPolicy(max_attempts=3, max_elapsed=2, jitter_ratio=0),
        sleep=clock.sleep,
        clock=clock.monotonic,
    )

    with pytest.raises(RateLimitError) as raised:
        _ = [event async for event in client.stream(ConversationManager())]

    assert raised.value is error
    assert inner.calls == 1
    assert clock.delays == []


@pytest.mark.asyncio
async def test_partial_stream_is_never_replayed() -> None:
    error = NetworkError("ambiguous after bytes")
    inner = ScriptedClient(
        [
            [TextDelta("visible"), error],
            [TextDelta("must-not-run")],
        ]
    )
    client = ResilientLLMClient(
        inner,
        RetryPolicy(base_delay=0, max_delay=0, jitter_ratio=0),
    )
    visible: list[str] = []

    with pytest.raises(AmbiguousStreamError) as raised:
        async for event in client.stream(ConversationManager()):
            if isinstance(event, TextDelta):
                visible.append(event.text)

    assert raised.value.__cause__ is error
    assert raised.value.error_code == "provider.partial_stream_ambiguous"
    assert raised.value.emitted_events == 1
    assert "do not replay" in raised.value.recommendation.casefold()
    assert client.drain_diagnostic_events()[0]["decision"] == (
        "blocked_partial_stream"
    )
    assert visible == ["visible"]
    assert inner.calls == 1


@pytest.mark.asyncio
async def test_non_retryable_errors_are_propagated_immediately() -> None:
    error = AuthenticationError("bad credential")
    inner = ScriptedClient([[error]])
    client = ResilientLLMClient(inner)

    with pytest.raises(AuthenticationError) as raised:
        _ = [event async for event in client.stream(ConversationManager())]

    assert raised.value is error
    assert inner.calls == 1


@pytest.mark.asyncio
async def test_resilient_wrapper_closes_owned_inner_once() -> None:
    inner = ScriptedClient([[]])
    client = ResilientLLMClient(inner)

    await client.close()
    await client.aclose()

    assert inner.closed == 1


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_attempts": 0},
        {"base_delay": -1},
        {"base_delay": 2, "max_delay": 1},
        {"max_elapsed": -1},
        {"jitter_ratio": 1.1},
    ],
)
def test_retry_policy_rejects_unbounded_or_invalid_values(kwargs: dict) -> None:
    with pytest.raises(ValueError):
        RetryPolicy(**kwargs)


class EmptyResponseStream:
    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


@pytest.mark.asyncio
async def test_openai_responses_request_consumes_configured_max_output_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    create = AsyncMock(return_value=EmptyResponseStream())
    transport = SimpleNamespace(responses=SimpleNamespace(create=create))
    monkeypatch.setattr(
        client_module, "_create_openai_transport", lambda config: transport
    )
    provider = ProviderConfig(
        name="openai",
        protocol="openai",
        base_url="https://api.openai.com/v1",
        model="gpt-4.1",
        api_key="fixture",
        max_output_tokens=3210,
    )
    client = OpenAIClient(provider)

    _ = [event async for event in client.stream(ConversationManager())]

    assert create.await_args.kwargs["max_output_tokens"] == 3210
