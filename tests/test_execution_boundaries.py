"""Offline regressions for final execution and incomplete provider responses."""
from __future__ import annotations

import json

import httpx
from anthropic import AsyncAnthropic
from openai import AsyncOpenAI
from pydantic import BaseModel
import pytest

from eviforge.agent import Agent, PermissionRequest, PermissionResponse
from eviforge.client import LLMClient, LLMError, create_client
from eviforge.config import ProviderConfig
from eviforge.permissions import DangerousCommandDetector, PathSandbox, PermissionChecker, RuleEngine
from eviforge.tools import ToolRegistry, create_default_registry
from eviforge.tools.base import StreamEnd, TextDelta, Tool, ToolCallComplete, ToolResult
from eviforge.tools.write_file import WriteFile


class TruncatedClient(LLMClient):
    def __init__(self, reason):
        self.reason = reason
        self.calls = 0

    def set_max_output_tokens(self, _tokens):
        pass

    async def stream(self, conversation, system="", tools=None):
        self.calls += 1
        yield TextDelta("partial answer")
        yield StreamEnd(self.reason)


@pytest.mark.asyncio
@pytest.mark.parametrize("reason,status,calls", [("content_filter", "blocked", 1), ("incomplete", "failed", 1), ("max_tokens", "failed", 5)])
async def test_incomplete_terminal_cannot_report_success(tmp_path, reason, status, calls):
    client = TruncatedClient(reason)
    agent = Agent(client, ToolRegistry(), "anthropic", work_dir=str(tmp_path), max_iterations=10)
    events = []
    answer = await agent.run_to_completion("task", event_callback=events.append)
    assert "partial answer" in answer
    assert agent.last_run_status == status
    assert agent.last_run_error
    assert client.calls == calls
    assert any(event["type"] == "agent_error" for event in events)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["disable", "replace"])
async def test_registry_change_during_approval_prevents_execution(tmp_path, change):
    registry = create_default_registry()
    checker = PermissionChecker(DangerousCommandDetector(), PathSandbox(str(tmp_path)), RuleEngine())
    agent = Agent(TruncatedClient("end_turn"), registry, "anthropic", work_dir=str(tmp_path), permission_checker=checker)
    iterator = agent._execute_tool(ToolCallComplete("write", "WriteFile", {"file_path": "forbidden.txt", "content": "stale approval"}))
    request = await anext(iterator)
    assert isinstance(request, PermissionRequest)
    if change == "disable":
        registry.disable("WriteFile")
    else:
        registry.register(WriteFile())
    request.future.set_result(PermissionResponse.ALLOW)
    result, _, _ = await anext(iterator)
    await iterator.aclose()
    assert result.is_error and "disabled or replaced" in result.output
    assert not (tmp_path / "forbidden.txt").exists()


def malformed_sse(protocol, raw):
    if protocol == "anthropic":
        events = [
            {"type": "message_start", "message": {"id": "msg", "type": "message", "role": "assistant", "model": "test", "content": [], "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 0}}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "call", "name": "DefaultAction", "input": {}}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": raw}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "tool_use", "stop_sequence": None}, "usage": {"output_tokens": 1}},
            {"type": "message_stop"},
        ]
    elif protocol == "openai":
        events = [
            {"type": "response.output_item.added", "output_index": 0, "sequence_number": 0, "item": {"type": "function_call", "id": "item", "call_id": "call", "name": "DefaultAction", "arguments": "", "status": "in_progress"}},
            {"type": "response.function_call_arguments.delta", "output_index": 0, "item_id": "item", "delta": raw, "sequence_number": 1},
            {"type": "response.function_call_arguments.done", "output_index": 0, "item_id": "item", "arguments": raw, "sequence_number": 2},
            {"type": "response.completed", "sequence_number": 3, "response": {"id": "response", "object": "response", "created_at": 1, "model": "test", "status": "completed", "output": [], "error": None, "incomplete_details": None, "usage": None}},
        ]
    else:
        def chunk(delta, reason=None):
            return {"id": "chat", "object": "chat.completion.chunk", "created": 1, "model": "test", "choices": [{"index": 0, "delta": delta, "finish_reason": reason}]}
        events = [chunk({"tool_calls": [{"index": 0, "id": "call", "type": "function", "function": {"name": "DefaultAction", "arguments": raw}}]}), chunk({}, "tool_calls")]
    content = "".join((f"event: {event['type']}\n" if "type" in event else "") + "data: " + json.dumps(event) + "\n\n" for event in events)
    return (content + ("data: [DONE]\n\n" if protocol == "openai-compat" else "")).encode()


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["anthropic", "openai", "openai-compat"])
@pytest.mark.parametrize("raw", ['{"incomplete":', "[]", '{"value":NaN}'])
async def test_real_sdk_malformed_args_never_execute_default_action(tmp_path, monkeypatch, protocol, raw):
    requests = []
    def handler(request):
        assert request.url.host == "provider.invalid"
        requests.append(request)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=malformed_sse(protocol, raw))
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    sdk_type = AsyncAnthropic if protocol == "anthropic" else AsyncOpenAI
    sdk = sdk_type(api_key="offline-test", base_url="https://provider.invalid", max_retries=0, http_client=http_client)
    monkeypatch.setattr("eviforge.client.AsyncAnthropic" if protocol == "anthropic" else "eviforge.client.AsyncOpenAI", lambda **_kwargs: sdk)
    client = create_client(ProviderConfig("offline", protocol, "https://provider.invalid", "test", api_key="offline-test", context_window=32000))
    class DefaultParams(BaseModel):
        content: str = "default side effect"
    class DefaultAction(Tool):
        name = "DefaultAction"
        description = "A tool whose empty arguments would execute a default action"
        category = "write"
        params_model = DefaultParams
        async def execute(self, params):
            (tmp_path / "should-not-exist").write_text(params.content)
            return ToolResult("executed")
    registry = ToolRegistry()
    registry.register(DefaultAction())
    agent = Agent(client, registry, protocol, work_dir=str(tmp_path))
    try:
        with pytest.raises(LLMError):
            await agent.run_to_completion("perform the tool action")
        assert agent.last_run_status in {"ambiguous", "failed"}
        assert len(requests) == 1, "malformed partial output cannot be automatically replayed"
        assert not (tmp_path / "should-not-exist").exists()
    finally:
        await sdk.close()
