"""One retry budget for both interactive and non-interactive Agent requests."""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable

from eviforge.client import (
    AmbiguousStreamError, LLMClient, NetworkError, RateLimitError,
    RetryExhaustedError,
)
from eviforge.conversation import ConversationManager
from eviforge.tools.base import (
    StreamEnd, StreamEvent, TextDelta, ThinkingDelta, ThinkingComplete,
    ToolCallStart, ToolCallDelta, ToolCallComplete, RetryNotice,
)


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    max_elapsed: float = 30.0
    initial_delay: float = 0.25
    max_delay: float = 5.0

    def __post_init__(self) -> None:
        import math
        if self.max_attempts < 1 or self.max_elapsed <= 0:
            raise ValueError("Retry attempts and elapsed budget must be positive")
        if any(not math.isfinite(value) for value in (self.max_elapsed, self.initial_delay, self.max_delay)):
            raise ValueError("Retry budgets must be finite")
        if self.initial_delay < 0 or self.max_delay < 0:
            raise ValueError("Retry delays cannot be negative")


async def stream_with_recovery(
    client: LLMClient, conversation: ConversationManager, *, system: str = "",
    tools: list[dict[str, Any]] | None = None, policy: RetryPolicy | None = None,
    on_retry: Callable[[dict[str, Any]], None] | None = None,
) -> AsyncIterator[StreamEvent]:
    """Retry only before observable output; incomplete output is never replayed.

    The elapsed limit includes opening a request, reading its stream and waiting
    between attempts. SDK retries must be disabled so budgets do not multiply.
    """
    policy = policy or RetryPolicy()
    started = time.monotonic()
    for attempt in range(1, policy.max_attempts + 1):
        remaining = policy.max_elapsed - (time.monotonic() - started)
        if remaining <= 0:
            raise RetryExhaustedError("Provider elapsed-time budget exhausted", attempts=attempt - 1, elapsed=time.monotonic() - started)
        partial = False
        partial_text = ""
        terminal = False
        stream = None
        try:
            async with asyncio.timeout(remaining):
                stream = (client.stream(conversation, system=system, tools=tools) if tools is not None
                          else client.stream(conversation, system=system))
                async for event in stream:
                    if isinstance(event, StreamEnd):
                        terminal = True
                    elif isinstance(event, TextDelta):
                        partial |= bool(event.text)
                        partial_text += event.text
                    elif isinstance(event, (ThinkingDelta, ThinkingComplete, ToolCallStart, ToolCallDelta, ToolCallComplete)):
                        partial = True
                    yield event
                if not terminal:
                    raise NetworkError("Provider stream ended without a terminal event")
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if partial:
                raise AmbiguousStreamError(
                    "Provider stream interrupted after partial output; automatic replay is disabled",
                    partial_text=partial_text, attempts=attempt,
                ) from exc
            if not isinstance(exc, (NetworkError, RateLimitError, TimeoutError)):
                raise
            elapsed = time.monotonic() - started
            delay = min(policy.initial_delay * 2 ** (attempt - 1), policy.max_delay)
            retry_after = getattr(exc, "retry_after", None)
            if retry_after is not None:
                # A server's minimum wait is never shortened to fit our budget.
                delay = max(delay, retry_after)
            if attempt >= policy.max_attempts or elapsed + delay >= policy.max_elapsed:
                raise RetryExhaustedError(
                    "Provider retry budget exhausted", attempts=attempt, elapsed=elapsed,
                ) from exc
            if on_retry:
                on_retry({"type": "provider_retry", "attempt": attempt, "delay": delay, "reason": type(exc).__name__})
            if stream is not None:
                close = getattr(stream, "aclose", None)
                if close is not None:
                    await close()
                stream = None
            yield RetryNotice(type(exc).__name__, attempt, delay)
            await asyncio.sleep(delay)
        finally:
            if stream is not None:
                close = getattr(stream, "aclose", None)
                if close is not None:
                    await close()
