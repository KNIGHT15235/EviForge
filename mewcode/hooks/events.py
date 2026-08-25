
from __future__ import annotations

from enum import StrEnum


class LifecycleEvent(StrEnum):
    # 会话（Session）级别
    SESSION_START = "session_start"
    SESSION_END = "session_end"


    # 轮次（Turn）级别
    TURN_START = "turn_start"
    TURN_END = "turn_end"


    # 工具（Tool）级别
    PRE_TOOL_USE = "pre_tool_use"
    POST_TOOL_USE = "post_tool_use"

    # 消息（Message）级别
    PRE_SEND = "pre_send"
    POST_RECEIVE = "post_receive"

    # 系统（System）级别
    STARTUP = "startup"
    SHUTDOWN = "shutdown"
    ERROR = "error"
    COMPACT = "compact"
    PERMISSION_REQUEST = "permission_request"
    FILE_CHANGE = "file_change"
    COMMAND_EXECUTE = "command_execute"


# Keep the complete vocabulary so older configuration files receive a precise
# "known but unsupported" diagnostic instead of an ambiguous spelling error.
# Only events in this set have a concrete dispatcher in the current runtime.
SUPPORTED_LIFECYCLE_EVENTS: frozenset[LifecycleEvent] = frozenset(
    {
        LifecycleEvent.SESSION_START,
        LifecycleEvent.SESSION_END,
        LifecycleEvent.TURN_START,
        LifecycleEvent.TURN_END,
        LifecycleEvent.PRE_TOOL_USE,
        LifecycleEvent.POST_TOOL_USE,
        LifecycleEvent.PRE_SEND,
        LifecycleEvent.POST_RECEIVE,
        LifecycleEvent.STARTUP,
        LifecycleEvent.SHUTDOWN,
    }
)

UNSUPPORTED_LIFECYCLE_EVENTS: frozenset[LifecycleEvent] = frozenset(
    set(LifecycleEvent) - set(SUPPORTED_LIFECYCLE_EVENTS)
)


class HookRuntimeProfile(StrEnum):
    """Execution entrypoint whose lifecycle dispatcher owns the HookEngine."""

    INTERACTIVE = "interactive"
    HEADLESS = "headless"
    DAG = "dag"


SUPPORTED_EVENTS_BY_RUNTIME: dict[
    HookRuntimeProfile, frozenset[LifecycleEvent]
] = {
    HookRuntimeProfile.INTERACTIVE: SUPPORTED_LIFECYCLE_EVENTS,
    # Agent.run_to_completion(), used by headless and DAG nodes, currently
    # dispatches only turn and tool boundaries. Keeping this explicit lets the
    # CLI reject a config that would otherwise be accepted and silently skipped.
    HookRuntimeProfile.HEADLESS: frozenset(
        {
            LifecycleEvent.TURN_START,
            LifecycleEvent.TURN_END,
            LifecycleEvent.PRE_TOOL_USE,
            LifecycleEvent.POST_TOOL_USE,
        }
    ),
    HookRuntimeProfile.DAG: frozenset(
        {
            LifecycleEvent.TURN_START,
            LifecycleEvent.TURN_END,
            LifecycleEvent.PRE_TOOL_USE,
            LifecycleEvent.POST_TOOL_USE,
        }
    ),
}


def supported_events_for(
    runtime: HookRuntimeProfile | str,
) -> frozenset[LifecycleEvent]:
    """Return the exact event contract for one runtime entrypoint."""

    return SUPPORTED_EVENTS_BY_RUNTIME[HookRuntimeProfile(runtime)]
