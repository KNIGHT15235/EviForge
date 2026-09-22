import asyncio
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import pytest

from eviforge.client import (
    AmbiguousStreamError, AuthenticationError, LLMClient, NetworkError,
    RateLimitError, RetryExhaustedError, _retry_after_seconds,
)
from eviforge.conversation import ConversationManager
from eviforge.reliability import RetryPolicy, stream_with_recovery
from eviforge.tools.base import StreamEnd, TextDelta, ToolCallStart, ToolCallComplete


class ScriptedClient(LLMClient):
    def __init__(self, attempts):
        self.attempts = attempts
        self.calls = 0
        self.closed_streams = 0

    async def stream(self, *args, **kwargs):
        script = self.attempts[min(self.calls, len(self.attempts) - 1)]
        self.calls += 1
        try:
            for item in script:
                if isinstance(item, BaseException):
                    raise item
                yield item
        finally:
            self.closed_streams += 1


async def consume(client, **kwargs):
    return [event async for event in stream_with_recovery(client, ConversationManager(), **kwargs)]


@pytest.mark.asyncio
async def test_retry_before_output_honors_count_and_closes_streams():
    client = ScriptedClient([[NetworkError("offline")], [TextDelta("done"), StreamEnd("end_turn")]])
    events = []
    output = await consume(client, policy=RetryPolicy(initial_delay=0), on_retry=events.append)
    assert client.calls == client.closed_streams == 2
    assert output[-1].stop_reason == "end_turn"
    assert events[0]["attempt"] == 1


@pytest.mark.asyncio
async def test_retry_after_is_not_shortened_when_it_exceeds_budget():
    client = ScriptedClient([[RateLimitError("throttled", retry_after=600)]])
    with pytest.raises(RetryExhaustedError) as exc:
        await consume(client, policy=RetryPolicy(max_elapsed=1))
    assert exc.value.attempts == client.calls == client.closed_streams == 1


@pytest.mark.asyncio
async def test_retry_after_wait_and_attempt_limit():
    client = ScriptedClient([[RateLimitError("throttled", retry_after=0.01)]])
    events = []
    with pytest.raises(RetryExhaustedError) as exc:
        await consume(client, policy=RetryPolicy(max_attempts=2, initial_delay=0), on_retry=events.append)
    assert events[0]["delay"] == 0.01
    assert exc.value.attempts == client.calls == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", [[TextDelta("partial")], [ToolCallStart("WriteFile", "t1")],
    [ToolCallComplete("t1", "WriteFile", {"file_path": "x", "content": "y"})]])
async def test_partial_stream_failure_is_ambiguous_and_never_replayed(prefix):
    client = ScriptedClient([prefix + [NetworkError("connection lost")], [TextDelta("duplicate"), StreamEnd("end_turn")]])
    with pytest.raises(AmbiguousStreamError):
        await consume(client, policy=RetryPolicy(initial_delay=0))
    assert client.calls == client.closed_streams == 1


@pytest.mark.asyncio
async def test_silent_partial_eof_is_ambiguous():
    client = ScriptedClient([[TextDelta("partial")]])
    with pytest.raises(AmbiguousStreamError) as exc:
        await consume(client)
    assert exc.value.partial_text == "partial"
    assert client.calls == 1


@pytest.mark.asyncio
async def test_empty_eof_exhausts_bounded_retry_instead_of_success():
    client = ScriptedClient([[]])
    with pytest.raises(RetryExhaustedError):
        await consume(client, policy=RetryPolicy(max_attempts=2, initial_delay=0))
    assert client.calls == 2


@pytest.mark.asyncio
async def test_nonretryable_authentication_error_and_cancellation_propagate():
    for error in (AuthenticationError("invalid key"), asyncio.CancelledError()):
        client = ScriptedClient([[error]])
        with pytest.raises(type(error)):
            await consume(client)
        assert client.calls == client.closed_streams == 1


@pytest.mark.asyncio
async def test_elapsed_budget_covers_waiting_for_first_chunk():
    closed = asyncio.Event()
    class HangingClient(LLMClient):
        async def stream(self, *args, **kwargs):
            try:
                await asyncio.Event().wait()
                yield TextDelta("unreachable")
            finally:
                closed.set()
    with pytest.raises(RetryExhaustedError):
        await consume(HangingClient(), policy=RetryPolicy(max_elapsed=0.01, initial_delay=0))
    assert closed.is_set()


def test_retry_after_http_date_and_invalid_values():
    future = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=30), usegmt=True)
    assert 28 <= _retry_after_seconds(future) <= 31
    for invalid in ("NaN", "inf", "-1", "invalid", None):
        assert _retry_after_seconds(invalid) is None
