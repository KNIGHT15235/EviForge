"""Interrupted real SDK streams must never be replayed or executed as complete."""
import httpx
import pytest

from test_document_provider import (
    make_wire_client, anthropic_events, responses_tool_events, chat_chunk,
    compat_tool_events, sse,
)
from eviforge.agent import Agent
from eviforge.client import AmbiguousStreamError
from eviforge.conversation import ConversationManager
from eviforge.tools import create_default_registry
from eviforge.tools.base import StreamEnd
from eviforge.reliability import RetryPolicy, stream_with_recovery


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["anthropic-text", "responses-tool", "compat-text", "compat-tool", "responses-text"])
async def test_five_sdk_sse_interruptions_are_typed_ambiguous(case, make_wire_client, tmp_path):
    if case == "anthropic-text":
        protocol, events = "anthropic", anthropic_events()[:-1]
    elif case == "responses-tool":
        protocol, events = "openai", responses_tool_events()[:-1]
    elif case == "responses-text":
        protocol, events = "openai", [{"type": "response.output_text.delta", "delta": "partial",
             "item_id": "message_1", "output_index": 0, "content_index": 0, "sequence_number": 1}]
    elif case == "compat-tool":
        protocol, events = "openai-compat", compat_tool_events()[:-2]
    else:
        protocol, events = "openai-compat", [chat_chunk({"content": "partial"})]
    client, requests = make_wire_client(protocol, lambda _: httpx.Response(200,
        headers={"content-type": "text/event-stream"}, content=sse(events)))
    agent = Agent(client, create_default_registry(), protocol, work_dir=str(tmp_path))
    agent.retry_policy = RetryPolicy(initial_delay=0)
    try:
        with pytest.raises(AmbiguousStreamError):
            await agent.run_to_completion("Inspect the current project")
        assert len(requests) == 1
        assert agent.last_run_status == "ambiguous"
        assert not any(message.tool_results for message in agent._current_conversation.history)
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_real_sdk_429_retries_once_then_completes(make_wire_client):
    calls = 0
    def response(_):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"retry-after": "0"},
                json={"error": {"message": "retry", "type": "rate_limit_error"}})
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
            content=sse([chat_chunk({"content": "done"}), chat_chunk(finish_reason="stop")], done=True))
    client, requests = make_wire_client("openai-compat", response)
    try:
        output = [item async for item in stream_with_recovery(client, ConversationManager(), policy=RetryPolicy(initial_delay=0))]
        assert sum(isinstance(item, StreamEnd) for item in output) == 1
        assert len(requests) == 2
        assert client._client.max_retries == 0
    finally:
        await client.aclose()
