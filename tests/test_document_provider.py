"""Exercise the real provider SDKs over in-memory HTTP/SSE, never the network."""

from __future__ import annotations

import json

import httpx
import pytest
from anthropic import AsyncAnthropic
from openai import AsyncOpenAI

from eviforge.client import (
    AuthenticationError, LLMError, NetworkError, RateLimitError, create_client,
)
from eviforge.config import ProviderConfig
from eviforge.conversation import ConversationManager
from eviforge.tools.base import StreamEnd, TextDelta, ToolCallComplete, ToolCallStart


PROVIDERS = ("anthropic", "openai", "openai-compat")


def sse(events, *, done=False):
    chunks = []
    for event in events:
        if "type" in event and not event.get("object"):
            chunks.append(f"event: {event['type']}\n")
        chunks.append(f"data: {json.dumps(event)}\n\n")
    if done:
        chunks.append("data: [DONE]\n\n")
    return "".join(chunks).encode()


def anthropic_events(interleaved=False):
    events = [{"type": "message_start", "message": {
        "id": "msg_test", "type": "message", "role": "assistant", "model": "test-model",
        "content": [], "stop_reason": None, "stop_sequence": None,
        "usage": {"input_tokens": 12, "output_tokens": 0, "cache_read_input_tokens": 4},
    }}]
    if interleaved:
        for index, name in enumerate(("Alpha", "Beta")):
            events.append({"type": "content_block_start", "index": index, "content_block": {
                "type": "tool_use", "id": f"call_{index}", "name": name, "input": {},
            }})
        for index, partial in [(0, '{"left":'), (1, '{"right":'), (0, '1}'), (1, '2}')]:
            events.append({"type": "content_block_delta", "index": index,
                           "delta": {"type": "input_json_delta", "partial_json": partial}})
        events.extend({"type": "content_block_stop", "index": index} for index in (0, 1))
    else:
        events.extend([
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "hello"}},
            {"type": "content_block_stop", "index": 0},
        ])
    events.extend([
        {"type": "message_delta", "delta": {"stop_reason": "tool_use" if interleaved else "end_turn", "stop_sequence": None},
         "usage": {"output_tokens": 6}},
        {"type": "message_stop"},
    ])
    return events


def responses_end(kind="completed", usage=True):
    response = {
        "id": "resp_test", "object": "response", "created_at": 1, "model": "test-model",
        "status": kind, "output": [], "error": None,
        "incomplete_details": {"reason": "max_output_tokens"} if kind == "incomplete" else None,
        "usage": {"input_tokens": 12, "output_tokens": 6, "total_tokens": 18,
                  "input_tokens_details": {"cached_tokens": 4},
                  "output_tokens_details": {"reasoning_tokens": 0}} if usage else None,
    }
    return {"type": f"response.{kind}", "response": response, "sequence_number": 100}


def responses_tool_events(*, deltas=True):
    events = []
    for index, name in enumerate(("Alpha", "Beta")):
        events.append({"type": "response.output_item.added", "output_index": index, "sequence_number": index,
                       "item": {"type": "function_call", "id": f"item_{index}", "call_id": f"call_{index}",
                                "name": name, "arguments": "", "status": "in_progress"}})
    if deltas:
        for index, partial in [(0, '{"left":'), (1, '{"right":'), (0, '1}'), (1, '2}')]:
            events.append({"type": "response.function_call_arguments.delta", "output_index": index,
                           "item_id": f"item_{index}", "delta": partial, "sequence_number": len(events)})
    for index, args in [(0, '{"left":1}'), (1, '{"right":2}')]:
        events.append({"type": "response.function_call_arguments.done", "output_index": index,
                       "item_id": f"item_{index}", "arguments": args, "sequence_number": len(events)})
    events.append(responses_end())
    return events


def chat_chunk(delta=None, finish_reason=None, usage=None, *, choices=True):
    return {"id": "chat_test", "object": "chat.completion.chunk", "created": 1, "model": "test-model",
            "choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish_reason}] if choices else [],
            "usage": usage}


def chat_usage():
    return {"prompt_tokens": 12, "completion_tokens": 6, "total_tokens": 18,
            "prompt_tokens_details": {"cached_tokens": 4}}


def compat_tool_events(usage=True):
    events = [chat_chunk({"tool_calls": [
        {"index": 0, "id": "call_0", "type": "function", "function": {"name": "Alpha", "arguments": ""}},
        {"index": 1, "id": "call_1", "type": "function", "function": {"name": "Beta", "arguments": ""}},
    ]})]
    for index, partial in [(0, '{"left":'), (1, '{"right":'), (0, '1}'), (1, '2}')]:
        events.append(chat_chunk({"tool_calls": [{"index": index, "function": {"arguments": partial}}]}))
    events.append(chat_chunk(finish_reason="tool_calls"))
    if usage:
        events.append(chat_chunk(usage=chat_usage(), choices=False))
    return events


@pytest.fixture
def make_wire_client(monkeypatch):
    def make(protocol, handler):
        requests = []
        def transport_handler(request):
            assert request.url.host == "provider.invalid"
            requests.append(request)
            return handler(request)
        http_client = httpx.AsyncClient(transport=httpx.MockTransport(transport_handler))
        real_sdk = AsyncAnthropic if protocol == "anthropic" else AsyncOpenAI
        factory_name = "AsyncAnthropic" if protocol == "anthropic" else "AsyncOpenAI"
        monkeypatch.setattr(f"eviforge.client.{factory_name}",
            lambda **kwargs: real_sdk(**{**kwargs, "http_client": http_client, "max_retries": 0}))
        base_url = "https://provider.invalid" if protocol == "anthropic" else "https://provider.invalid/v1"
        config = ProviderConfig("offline", protocol, base_url, "test-model",
                                api_key="test-placeholder-key", max_output_tokens=1234)
        return create_client(config), requests
    return make


async def collect(client):
    conversation = ConversationManager()
    conversation.add_user_message("Inspect these two inputs.")
    tools = [{"name": "Alpha", "description": "first tool", "input_schema": {"type": "object", "properties": {}}}]
    if not client.__class__.__name__.startswith("Anthropic"):
        tools = [{"type": "function", "name": "Alpha", "description": "first tool", "parameters": {"type": "object", "properties": {}}}]
    try:
        return [event async for event in client.stream(conversation, system="System instruction", tools=tools)]
    finally:
        await client._client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", PROVIDERS)
async def test_real_sdk_interleaved_tool_arguments_stay_with_their_call(protocol, make_wire_client):
    events = (anthropic_events(True) if protocol == "anthropic" else
              responses_tool_events() if protocol == "openai" else compat_tool_events())
    client, requests = make_wire_client(protocol, lambda _: httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse(events, done=protocol == "openai-compat")))
    output = await collect(client)
    calls = [event for event in output if isinstance(event, ToolCallComplete)]
    assert [(call.tool_id, call.tool_name, call.arguments) for call in calls] == [
        ("call_0", "Alpha", {"left": 1}), ("call_1", "Beta", {"right": 2}),
    ]
    assert [(event.tool_id, event.tool_name) for event in output if isinstance(event, ToolCallStart)] == [("call_0", "Alpha"), ("call_1", "Beta")]
    assert len([event for event in output if isinstance(event, StreamEnd)]) == 1
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_responses_uses_final_argument_payload_when_no_deltas_arrive(make_wire_client):
    client, _ = make_wire_client("openai", lambda _: httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse(responses_tool_events(deltas=False))))
    output = await collect(client)
    assert [event.arguments for event in output if isinstance(event, ToolCallComplete)] == [{"left": 1}, {"right": 2}]


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", PROVIDERS)
async def test_real_sdk_text_usage_and_configured_output_limit(protocol, make_wire_client):
    events = (anthropic_events() if protocol == "anthropic" else
              [{"type": "response.output_text.delta", "delta": "hello", "output_index": 0, "content_index": 0, "item_id": "msg_1", "sequence_number": 0}, responses_end()] if protocol == "openai" else
              [chat_chunk({"content": "hello"}), chat_chunk(finish_reason="stop"), chat_chunk(usage=chat_usage(), choices=False)])
    client, requests = make_wire_client(protocol, lambda _: httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse(events, done=protocol == "openai-compat")))
    output = await collect(client)
    assert "".join(event.text for event in output if isinstance(event, TextDelta)) == "hello"
    ends = [event for event in output if isinstance(event, StreamEnd)]
    assert len(ends) == 1
    assert ends[0].stop_reason == "end_turn"
    assert ends[0].input_tokens == (12 if protocol == "anthropic" else 8)
    assert ends[0].output_tokens == 6 and ends[0].cache_read == 4
    request_body = json.loads(requests[0].content)
    key = "max_output_tokens" if protocol == "openai" else "max_tokens"
    assert request_body[key] == 1234
    expected_path = "/v1/messages" if protocol == "anthropic" else "/v1/responses" if protocol == "openai" else "/v1/chat/completions"
    assert requests[0].url.path == expected_path


@pytest.mark.asyncio
@pytest.mark.parametrize("reason,expected", [("stop", "end_turn"), ("length", "max_tokens"), ("content_filter", "content_filter")])
@pytest.mark.parametrize("include_usage", [False, True])
async def test_compat_emits_one_terminal_event_with_actual_finish_reason(reason, expected, include_usage, make_wire_client):
    events = [chat_chunk({"content": "partial"}), chat_chunk(finish_reason=reason)]
    if include_usage:
        events.append(chat_chunk(usage=chat_usage(), choices=False))
    client, _ = make_wire_client("openai-compat", lambda _: httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse(events, done=True)))
    ends = [event for event in await collect(client) if isinstance(event, StreamEnd)]
    assert len(ends) == 1
    assert ends[0].stop_reason == expected
    assert ends[0].output_tokens == (6 if include_usage else 0)


@pytest.mark.asyncio
async def test_compat_usage_in_finish_chunk_is_not_lost(make_wire_client):
    events = [chat_chunk({"content": "hello"}, finish_reason="stop", usage=chat_usage())]
    client, _ = make_wire_client("openai-compat", lambda _: httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse(events, done=True)))
    ends = [event for event in await collect(client) if isinstance(event, StreamEnd)]
    assert len(ends) == 1
    assert (ends[0].input_tokens, ends[0].output_tokens, ends[0].cache_read) == (8, 6, 4)


@pytest.mark.asyncio
async def test_compat_tool_calls_without_usage_still_finish(make_wire_client):
    client, _ = make_wire_client("openai-compat", lambda _: httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse(compat_tool_events(usage=False), done=True)))
    output = await collect(client)
    assert len([event for event in output if isinstance(event, ToolCallComplete)]) == 2
    ends = [event for event in output if isinstance(event, StreamEnd)]
    assert len(ends) == 1 and ends[0].stop_reason == "tool_use"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,expected", [("completed", "end_turn"), ("incomplete", "max_tokens")])
async def test_responses_terminal_without_usage(kind, expected, make_wire_client):
    client, _ = make_wire_client("openai", lambda _: httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse([responses_end(kind, usage=False)])))
    ends = [event for event in await collect(client) if isinstance(event, StreamEnd)]
    assert len(ends) == 1
    assert ends[0].stop_reason == expected


@pytest.mark.asyncio
async def test_responses_failed_terminal_event_maps_to_llm_error(make_wire_client):
    event = responses_end("failed")
    event["response"]["error"] = {"code": "server_error", "message": "stream failed"}
    client, _ = make_wire_client("openai", lambda _: httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse([event])))
    with pytest.raises(LLMError, match="stream failed"):
        await collect(client)


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", PROVIDERS)
async def test_sse_error_event_maps_to_llm_error(protocol, make_wire_client):
    event = {"type": "error", "error": {"type": "api_error", "message": "stream failed"}}
    client, _ = make_wire_client(protocol, lambda _: httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse([event])))
    with pytest.raises(LLMError):
        await collect(client)


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", PROVIDERS)
@pytest.mark.parametrize("status,error_type", [(401, AuthenticationError), (429, RateLimitError), (503, LLMError)])
async def test_http_errors_are_mapped_after_real_sdk_parsing(protocol, status, error_type, make_wire_client):
    body = {"type": "error", "error": {"type": "api_error", "message": "offline failure", "code": "test_failure"}}
    client, requests = make_wire_client(protocol, lambda _: httpx.Response(status, json=body, headers={"retry-after": "2.5"}))
    with pytest.raises(error_type) as exc:
        await collect(client)
    if status == 429:
        assert exc.value.retry_after == 2.5
    assert len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", PROVIDERS)
async def test_non_numeric_retry_after_keeps_rate_limit_error_type(protocol, make_wire_client):
    client, _ = make_wire_client(protocol, lambda _: httpx.Response(429, json={"error": {"message": "slow down", "type": "rate_limit_error"}}, headers={"retry-after": "not-a-number"}))
    with pytest.raises(RateLimitError) as exc:
        await collect(client)
    assert exc.value.retry_after is None


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", PROVIDERS)
async def test_transport_timeout_maps_to_network_error(protocol, make_wire_client):
    def timeout(request):
        raise httpx.ReadTimeout("offline timeout", request=request)
    client, requests = make_wire_client(protocol, timeout)
    with pytest.raises(NetworkError):
        await collect(client)
    assert len(requests) == 1
