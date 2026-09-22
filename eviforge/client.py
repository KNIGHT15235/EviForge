from __future__ import annotations

import json
import math
from abc import ABC, abstractmethod
from typing import Any, AsyncIterator

from anthropic import AsyncAnthropic
from openai import AsyncOpenAI

from eviforge.config import ProviderConfig
from eviforge.conversation import ConversationManager
from eviforge.serialization import (
    build_anthropic_messages,
    build_chat_completion_messages,
    build_openai_input,
)
from eviforge.tools.base import (
    StreamEnd,
    StreamEvent,
    TextDelta,
    ThinkingComplete,
    ThinkingDelta,
    ToolCallComplete,
    ToolCallDelta,
    ToolCallStart,
)


# 限制自动拉取模型元数据的超时时间，防止慢响应或挂起的
# /v1/models 端点拖延启动。超时后降级为 None（即"未知"），
# 由下一层 context window 解析逻辑接管。
ANTHROPIC_MODEL_FETCH_TIMEOUT = 3.0


_EPHEMERAL = {"type": "ephemeral"}


def _mark_last_user_tail_for_cache(messages: list[dict[str, Any]]) -> None:
    """给最后一条 user 消息的最后一个 block 附加 cache_control。

    会原地修改 `messages`。Anthropic 会缓存到（且包含）这个 block 为止的前缀；
    后续请求只要前缀逐字节相同，缓存命中的 token 只需支付 10% 的费用。
    仅适用于 Anthropic 协议的消息。
    """
    if not messages:
        return
    # 从后往前找到最后一条 user 角色消息；assistant 尾部不能锚定 cache。
    for msg in reversed(messages):
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            # 把字符串 content 升级为 block 形式，以便附加 cache_control。
            msg["content"] = [{
                "type": "text",
                "text": content,
                "cache_control": _EPHEMERAL,
            }]
        elif isinstance(content, list) and content:
            last = content[-1]
            if isinstance(last, dict):
                last["cache_control"] = _EPHEMERAL
        return


def _mark_last_tool_for_cache(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """返回一个浅拷贝的 tools 列表，并在最后一个 tool 上标记 cache_control。

    tool schema 在多轮对话之间是稳定的，因此标记列表尾部即可缓存整个 tool block。
    我们不直接修改调用方传入的列表，因为这些 tool schema 往往是注册表里的
    模块级单例。
    """
    if not tools:
        return tools
    marked = list(tools)
    last = dict(marked[-1])
    last["cache_control"] = _EPHEMERAL
    marked[-1] = last
    return marked


class LLMError(Exception):
    pass


def _parse_tool_arguments(raw: Any, tool_name: str) -> dict[str, Any]:
    """A malformed provider payload must never become an executable {} call."""
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(parsed, dict):
            raise ValueError("Tool arguments must be a JSON object")
        # Reject non-JSON NaN/Infinity accepted by Python's permissive decoder.
        json.dumps(parsed, allow_nan=False)
    except (ValueError, TypeError) as exc:
        raise LLMError(f"Invalid JSON tool arguments for {tool_name}") from exc
    return parsed


class AuthenticationError(LLMError):
    pass


class RateLimitError(LLMError):


    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class NetworkError(LLMError):
    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class AmbiguousStreamError(LLMError):
    """A response started, but its completion cannot be established."""

    def __init__(self, message: str, *, partial_text: str = "", attempts: int = 1):
        super().__init__(message)
        self.partial_text = partial_text
        self.attempts = attempts


class RetryExhaustedError(LLMError):
    def __init__(self, message: str, *, attempts: int, elapsed: float):
        super().__init__(message)
        self.attempts = attempts
        self.elapsed = elapsed


def _retry_after_seconds(value: str | None) -> float | None:
    """An unparseable optional header must not change the mapped error type."""
    try:
        seconds = float(value) if value is not None else None
    except ValueError:
        from datetime import datetime, timezone
        from email.utils import parsedate_to_datetime
        try:
            when = parsedate_to_datetime(value)
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None
    return seconds if seconds is not None and math.isfinite(seconds) and seconds >= 0 else None


class LLMClient(ABC):
    @abstractmethod
    async def stream(
        self,
        conversation: ConversationManager,
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        yield TextDelta("")

    def set_max_output_tokens(self, tokens: int) -> None:
        pass

    async def aclose(self) -> None:
        """Close the owned SDK client; fake clients need no special cleanup."""
        sdk = getattr(self, "_client", None)
        if sdk is not None:
            await sdk.close()


async def _close_response_stream(response: Any) -> None:
    import inspect
    close = getattr(response, "close", None)
    if close is not None:
        result = close()
        if inspect.isawaitable(result):
            await result


def _supports_adaptive_thinking(model: str) -> bool:
    for family in ("claude-opus-4-", "claude-sonnet-4-"):
        if model.startswith(family):
            rest = model[len(family):]
            if rest and rest[0].isdigit() and int(rest[0]) >= 6:
                return True
    return False


class AnthropicClient(LLMClient):
    def __init__(self, config: ProviderConfig) -> None:
        self.model = config.model
        self.thinking = config.thinking
        self.max_output_tokens = config.get_max_output_tokens()
        api_key = config.resolve_api_key()
        if not api_key:
            raise AuthenticationError(
                "Anthropic API key not found. "
                "Set it in .eviforge/config.yaml or via ANTHROPIC_API_KEY env var."
            )
        self._client = AsyncAnthropic(api_key=api_key, base_url=config.base_url, max_retries=0)

    def set_max_output_tokens(self, tokens: int) -> None:
        self.max_output_tokens = tokens

    async def fetch_model_context_window(self) -> int | None:
        """向 Anthropic 兼容的 /v1/models/{model} 端点查询模型的
        max_input_tokens（context window 解析的第 2 层）。

        采用尽力而为策略：遇到任何错误——非 anthropic 端点、网络故障、
        超时、字段缺失——都返回 ``None`` 而非抛出异常，以便调用方降级到
        下一层。它的阻塞时间不会超过 ANTHROPIC_MODEL_FETCH_TIMEOUT，也不会
        向外传播异常，因此在启动时调用是安全的。
        """
        try:
            info = await self._client.models.retrieve(
                self.model, timeout=ANTHROPIC_MODEL_FETCH_TIMEOUT
            )
            window = getattr(info, "max_input_tokens", None)
            if isinstance(window, int) and window > 0:
                return window
            return None
        except Exception:
            return None

    async def stream(
        self,
        conversation: ConversationManager,
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        import anthropic as _anthropic

        messages = build_anthropic_messages(conversation.get_messages())

        # 在最长稳定前缀上标记 prompt cache 断点：system、tools
        # 以及最后一条 user 消息的尾部。Anthropic 会缓存到每个断点，
        # 并在下次请求时按字节比对——context.manager 中的
        # ContentReplacementState 保证断点之后的 tool_result 内容保持稳定。
        _mark_last_user_tail_for_cache(messages)

        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_output_tokens,
            "messages": messages,
        }
        if system:
            kwargs["system"] = [{
                "type": "text",
                "text": system,
                "cache_control": {"type": "ephemeral"},
            }]
        if tools:
            kwargs["tools"] = _mark_last_tool_for_cache(tools)

        if self.thinking:
            if _supports_adaptive_thinking(self.model):
                kwargs["thinking"] = {"type": "enabled", "budget_tokens": 0}
            else:
                kwargs["thinking"] = {
                    "type": "enabled",
                    "budget_tokens": max(self.max_output_tokens - 1, 1024),
                }

        active_calls: dict[int, dict[str, Any]] = {}
        thinking_blocks: dict[int, dict[str, str]] = {}
        terminal_seen = False

        try:
            async with self._client.messages.stream(**kwargs) as stream:
                async for event in stream:
                    if event.type == "content_block_start":
                        block = event.content_block
                        if block.type == "thinking":
                            thinking_blocks[event.index] = {"text": "", "signature": ""}
                        elif block.type == "tool_use":
                            active_calls[event.index] = {
                                "name": block.name, "id": block.id, "args": "", "input": block.input,
                            }
                            yield ToolCallStart(
                                tool_name=block.name,
                                tool_id=block.id,
                            )
                    elif event.type == "content_block_delta":
                        delta = event.delta
                        if delta.type == "text_delta":
                            yield TextDelta(text=delta.text)
                        elif delta.type == "thinking_delta":
                            thinking_blocks[event.index]["text"] += delta.thinking
                            yield ThinkingDelta(text=delta.thinking)
                        elif delta.type == "signature_delta":
                            thinking_blocks[event.index]["signature"] = delta.signature
                        elif delta.type == "input_json_delta":
                            active_calls[event.index]["args"] += delta.partial_json
                            yield ToolCallDelta(text=delta.partial_json)
                    elif event.type == "content_block_stop":
                        thinking = thinking_blocks.pop(event.index, None)
                        if thinking is not None:
                            yield ThinkingComplete(
                                thinking=thinking["text"],
                                signature=thinking["signature"],
                            )
                        call = active_calls.pop(event.index, None)
                        if call is not None:
                            args = _parse_tool_arguments(call["args"] if call["args"] else call["input"], call["name"])
                            yield ToolCallComplete(
                                tool_id=call["id"],
                                tool_name=call["name"],
                                arguments=args,
                            )
                    elif event.type == "message_stop":
                        terminal_seen = True

                if not terminal_seen:
                    raise NetworkError("Anthropic stream ended without message_stop")
                final = await stream.get_final_message()
                usage = final.usage
                yield StreamEnd(
                    stop_reason=final.stop_reason or "end_turn",
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    cache_read=getattr(usage, "cache_read_input_tokens", 0) or 0,
                    cache_creation=getattr(
                        usage, "cache_creation_input_tokens", 0
                    ) or 0,
                )

        except _anthropic.AuthenticationError as e:
            raise AuthenticationError(f"Invalid API key: {e}") from e
        except _anthropic.RateLimitError as e:
            retry = e.response.headers.get("retry-after") if e.response else None
            raise RateLimitError(
                f"Rate limited. {f'Retry after {retry}s.' if retry else 'Please wait.'}",
                retry_after=_retry_after_seconds(retry),
            ) from e
        except _anthropic.APIConnectionError as e:
            raise NetworkError(f"Network error: {e}") from e
        except _anthropic.APIStatusError as e:
            if e.status_code >= 500:
                raise NetworkError(f"Provider unavailable ({e.status_code})", _retry_after_seconds(e.response.headers.get("retry-after"))) from e
            raise LLMError(f"API error ({e.status_code}): {e.message}") from e
        except _anthropic.APIError as e:
            raise LLMError(f"API stream error: {e}") from e


class OpenAIClient(LLMClient):
    def __init__(self, config: ProviderConfig) -> None:
        self.model = config.model
        self.max_output_tokens = config.get_max_output_tokens()
        api_key = config.resolve_api_key()
        if not api_key:
            raise AuthenticationError(
                "OpenAI API key not found. "
                "Set it in .eviforge/config.yaml or via OPENAI_API_KEY env var."
            )
        self._client = AsyncOpenAI(api_key=api_key, base_url=config.base_url, max_retries=0)

    def set_max_output_tokens(self, tokens: int) -> None:
        self.max_output_tokens = tokens

    async def stream(
        self,
        conversation: ConversationManager,
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        import openai as _openai

        input_messages = build_openai_input(conversation.get_messages())

        kwargs: dict[str, Any] = {
            "model": self.model,
            "input": input_messages,
            "stream": True,
            "max_output_tokens": self.max_output_tokens,
        }
        if system:
            kwargs["instructions"] = system
        if tools:
            kwargs["tools"] = tools

        active_calls: dict[int | str, dict[str, str]] = {}
        response_stream = None
        terminal_seen = False

        try:
            response_stream = await self._client.responses.create(**kwargs)
            async for event in response_stream:
                key = getattr(event, "output_index", None)
                if key is None:
                    item = getattr(event, "item", None)
                    key = getattr(event, "item_id", None) or getattr(item, "id", "")
                if event.type == "response.output_text.delta":
                    yield TextDelta(text=event.delta)
                elif event.type == "response.function_call_arguments.delta":
                    if key not in active_calls:
                        active_calls[key] = {
                            "name": getattr(event, "name", "") or "",
                            "id": getattr(event, "call_id", "") or "", "args": "",
                        }
                        if active_calls[key]["name"]:
                            yield ToolCallStart(
                                tool_name=active_calls[key]["name"],
                                tool_id=active_calls[key]["id"],
                            )
                    active_calls[key]["args"] += event.delta
                    yield ToolCallDelta(text=event.delta)
                elif event.type == "response.function_call_arguments.done":
                    call = active_calls.pop(key, {
                        "name": getattr(event, "name", "") or "",
                        "id": getattr(event, "call_id", "") or "", "args": "",
                    })
                    arguments = getattr(event, "arguments", None)
                    if arguments is None:
                        arguments = call["args"]
                    args = _parse_tool_arguments(arguments, call["name"])
                    yield ToolCallComplete(
                        tool_id=call["id"],
                        tool_name=call["name"],
                        arguments=args,
                    )
                elif event.type == "response.output_item.added":
                    item = getattr(event, "item", None)
                    if item and getattr(item, "type", "") == "function_call":
                        active_calls[key] = {
                            "name": getattr(item, "name", ""),
                            "id": getattr(item, "call_id", ""), "args": "",
                        }
                        yield ToolCallStart(
                            tool_name=active_calls[key]["name"],
                            tool_id=active_calls[key]["id"],
                        )
                elif event.type in ("response.completed", "response.incomplete"):
                    terminal_seen = True
                    resp = getattr(event, "response", None)
                    usage = getattr(resp, "usage", None) if resp else None
                    # Responses API 通过 input_tokens_details.cached_tokens
                    # 暴露 cache 命中数，没有 creation 计数。注意这里的
                    # input_tokens *包含*了缓存 token，所以需要减去它们，
                    # 保持 input + cache_read 可加性，与 Anthropic 对齐。
                    details = getattr(usage, "input_tokens_details", None)
                    cache_read = getattr(details, "cached_tokens", 0) or 0
                    input_tokens = getattr(usage, "input_tokens", 0) or 0
                    reason = "end_turn"
                    if event.type == "response.incomplete":
                        incomplete = getattr(resp, "incomplete_details", None)
                        reason = getattr(incomplete, "reason", None) or "incomplete"
                        if reason == "max_output_tokens":
                            reason = "max_tokens"
                    yield StreamEnd(
                        stop_reason=reason,
                        input_tokens=max(input_tokens - cache_read, 0),
                        output_tokens=getattr(usage, "output_tokens", 0) or 0,
                        cache_read=cache_read,
                        cache_creation=0,
                    )
                elif event.type == "response.failed":
                    error = getattr(getattr(event, "response", None), "error", None)
                    raise LLMError(f"API response failed: {getattr(error, 'message', None) or 'unknown error'}")

            if not terminal_seen:
                raise NetworkError("Responses stream ended without completion")

        except _openai.AuthenticationError as e:
            raise AuthenticationError(f"Invalid API key: {e}") from e
        except _openai.RateLimitError as e:
            retry = None
            if hasattr(e, "response") and e.response is not None:
                retry = e.response.headers.get("retry-after")
            raise RateLimitError(
                f"Rate limited. {f'Retry after {retry}s.' if retry else 'Please wait.'}",
                retry_after=_retry_after_seconds(retry),
            ) from e
        except _openai.APIConnectionError as e:
            raise NetworkError(f"Network error: {e}") from e
        except _openai.APIStatusError as e:
            if e.status_code >= 500:
                raise NetworkError(f"Provider unavailable ({e.status_code})", _retry_after_seconds(e.response.headers.get("retry-after"))) from e
            raise LLMError(f"API error ({e.status_code}): {e.message}") from e
        except _openai.APIError as e:
            raise LLMError(f"API stream error: {e}") from e
        finally:
            await _close_response_stream(response_stream)


class OpenAICompatClient(LLMClient):
    """面向 OpenAI 兼容 provider 的客户端，使用 Chat Completions API。

    与面向较新的 Responses API（``/responses``）的 ``OpenAIClient`` 不同，
    本客户端使用受广泛支持的 Chat Completions 端点（``/chat/completions``），
    因此能兼容任何暴露 OpenAI 兼容接口的 provider（例如 vLLM、Ollama、
    Together、Azure OpenAI 等）。
    """

    def __init__(self, config: ProviderConfig) -> None:
        self.model = config.model
        self.max_output_tokens = config.get_max_output_tokens()
        api_key = config.resolve_api_key()
        if not api_key:
            raise AuthenticationError(
                "OpenAI-compatible API key not found. "
                "Set it in .eviforge/config.yaml or via OPENAI_API_KEY env var."
            )
        self._client = AsyncOpenAI(api_key=api_key, base_url=config.base_url, max_retries=0)

    def set_max_output_tokens(self, tokens: int) -> None:
        self.max_output_tokens = tokens

    @staticmethod
    def _convert_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """把 tool schema 转换成 Chat Completions 格式。

        tool 注册表为 ``openai`` 系列输出的是 Responses API 风格的 dict::

            {"type": "function", "name": "...", "description": "...",
             "parameters": {...}}

        而 Chat Completions 要求把 name/description/parameters 嵌套在
        ``function`` 键下::

            {"type": "function", "function": {"name": "...",
             "description": "...", "parameters": {...}}}
        """
        converted: list[dict[str, Any]] = []
        for t in tools:
            converted.append({
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t.get("parameters", t.get("input_schema", {})),
                },
            })
        return converted

    async def stream(
        self,
        conversation: ConversationManager,
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        import openai as _openai

        messages = build_chat_completion_messages(conversation.get_messages())

        # 如果有 system 消息则插入到消息列表头部。
        if system:
            messages = [{"role": "system", "content": system}] + messages

        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": self.max_output_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            kwargs["tools"] = self._convert_tools(tools)

        # 用于累积 streaming tool call 的状态。Chat Completions 流按
        # tool_calls 列表中的位置索引下发 delta，我们按索引跟踪每个进行中的调用。
        active_calls: dict[int, dict[str, str]] = {}  # 索引 -> {id, name, args}
        stop_reason = "end_turn"
        usage = None
        response = None
        terminal_seen = False

        try:
            response = await self._client.chat.completions.create(**kwargs)
            async for chunk in response:
                if chunk.usage is not None:
                    usage = chunk.usage
                if not chunk.choices:
                    continue

                choice = chunk.choices[0]
                delta = choice.delta

                # --- 文本内容 ---
                if delta and delta.content:
                    yield TextDelta(text=delta.content)

                # --- tool call 增量 ---
                if delta and delta.tool_calls:
                    for tc in delta.tool_calls:
                        idx = tc.index
                        if idx not in active_calls:
                            active_calls[idx] = {"id": "", "name": "", "args": ""}
                        call = active_calls[idx]

                        if tc.id:
                            call["id"] = tc.id
                        if tc.function and tc.function.name:
                            call["name"] = tc.function.name
                            yield ToolCallStart(
                                tool_name=call["name"],
                                tool_id=call["id"],
                            )
                        if tc.function and tc.function.arguments:
                            call["args"] += tc.function.arguments
                            yield ToolCallDelta(text=tc.function.arguments)

                # --- 结束原因 ---
                if choice.finish_reason:
                    terminal_seen = True
                    stop_reason = {
                        "stop": "end_turn", "tool_calls": "tool_use", "length": "max_tokens",
                    }.get(choice.finish_reason, choice.finish_reason)
                    if choice.finish_reason == "tool_calls":
                        for _idx, call in sorted(active_calls.items()):
                            args = _parse_tool_arguments(call["args"], call["name"])
                            yield ToolCallComplete(
                                tool_id=call["id"],
                                tool_name=call["name"],
                                arguments=args,
                            )
                        active_calls.clear()

            # Usage may arrive alongside a choice, in a trailing chunk, or not
            # at all. Emit one terminal event after consuming the entire stream.
            if not terminal_seen:
                raise NetworkError("Chat Completions stream ended without finish_reason")
            details = getattr(usage, "prompt_tokens_details", None)
            cache_read = getattr(details, "cached_tokens", 0) or 0
            prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
            yield StreamEnd(
                stop_reason=stop_reason,
                input_tokens=max(prompt_tokens - cache_read, 0),
                output_tokens=getattr(usage, "completion_tokens", 0) or 0,
                cache_read=cache_read,
                cache_creation=0,
            )

        except _openai.AuthenticationError as e:
            raise AuthenticationError(f"Invalid API key: {e}") from e
        except _openai.RateLimitError as e:
            retry = None
            if hasattr(e, "response") and e.response is not None:
                retry = e.response.headers.get("retry-after")
            raise RateLimitError(
                f"Rate limited. {f'Retry after {retry}s.' if retry else 'Please wait.'}",
                retry_after=_retry_after_seconds(retry),
            ) from e
        except _openai.APIConnectionError as e:
            raise NetworkError(f"Network error: {e}") from e
        except _openai.APIStatusError as e:
            if e.status_code >= 500:
                raise NetworkError(f"Provider unavailable ({e.status_code})", _retry_after_seconds(e.response.headers.get("retry-after"))) from e
            raise LLMError(f"API error ({e.status_code}): {e.message}") from e
        except _openai.APIError as e:
            raise LLMError(f"API stream error: {e}") from e
        finally:
            await _close_response_stream(response)


def create_client(config: ProviderConfig) -> LLMClient:
    if config.protocol == "anthropic":
        return AnthropicClient(config)
    elif config.protocol == "openai":
        return OpenAIClient(config)
    elif config.protocol == "openai-compat":
        return OpenAICompatClient(config)
    raise ValueError(f"Unknown protocol: {config.protocol}")


async def resolve_context_window(config: ProviderConfig) -> None:
    """context window 解析的第 2 层：对于 anthropic 协议的 provider，
    从 {base_url}/v1/models/{model} 自动拉取一次模型的 max_input_tokens，
    并通过 set_fetched_context_window 缓存到 ``config`` 上，这样后续
    config.get_context_window() 调用就能直接使用、无需再次访问网络。

    完全尽力而为，绝不抛出异常：非 anthropic provider、客户端构造失败
    （例如缺少 API key）、拉取失败或超时，都会让缓存保持不变，从而让
    get_context_window() 降级到内置映射表 / 默认值。在启动时调用是安全的——
    阻塞时间不会超过拉取自身的超时，也不会导致崩溃。
    """
    # 配置中显式指定的 window 在 get_context_window() 中优先级最高，
    # 上次调用已缓存的值也不需要重新拉取——直接跳过网络请求。
    if config.context_window > 0 or config._fetched_context_window > 0:
        return
    if config.protocol != "anthropic":
        return

    try:
        client = create_client(config)
    except Exception:
        return
    fetch = getattr(client, "fetch_model_context_window", None)
    if fetch is None:
        return

    try:
        window = await fetch()
    except Exception:
        window = None
    if window:
        config.set_fetched_context_window(window)
