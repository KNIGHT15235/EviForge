
from __future__ import annotations

from eviforge.hooks.conditions import ConditionParseError, parse_condition
from eviforge.hooks.events import LifecycleEvent
from eviforge.hooks.models import Action, Hook

_VALID_EVENTS = {e.value for e in LifecycleEvent}
_VALID_ACTION_TYPES = {"command", "prompt", "http", "agent", "builtin"}

_REQUIRED_FIELDS: dict[str, list[str]] = {
    "command": ["command"],
    "prompt": ["message"],
    "http": ["url"],
    "agent": ["prompt"],
    "builtin": ["builtin"],
}


class HookConfigError(Exception):
    pass


def _identify(entry: dict, index: int) -> str:
    hook_id = entry.get("id", "")
    return f"hook '{hook_id}'" if hook_id else f"hook #{index + 1}"


def load_hooks(raw_hooks: list[dict] | None) -> list[Hook]:
    if not raw_hooks:
        return []

    hooks: list[Hook] = []
    for i, entry in enumerate(raw_hooks):
        label = _identify(entry, i)

        if not isinstance(entry, dict):
            raise HookConfigError(f"{label}: must be a mapping")

        event = entry.get("event")
        if not event:
            raise HookConfigError(f"{label}: missing 'event' field")
        if event not in _VALID_EVENTS:
            raise HookConfigError(
                f"{label}: invalid event '{event}', "
                f"must be one of: {', '.join(sorted(_VALID_EVENTS))}"
            )

        raw_action = entry.get("action")
        if not isinstance(raw_action, dict):
            raise HookConfigError(f"{label}: missing or invalid 'action' field")

        action_type = raw_action.get("type")
        if action_type not in _VALID_ACTION_TYPES:
            raise HookConfigError(
                f"{label}: invalid action type '{action_type}', "
                f"must be one of: {', '.join(sorted(_VALID_ACTION_TYPES))}"
            )

        required = _REQUIRED_FIELDS[action_type]
        for field_name in required:
            if not raw_action.get(field_name):
                raise HookConfigError(
                    f"{label}: action type '{action_type}' requires "
                    f"'{field_name}' field"
                )

        reject = bool(entry.get("reject", False))
        scope = entry.get("scope", "all")
        if not isinstance(scope, str) or scope not in {"all", "main"}:
            raise HookConfigError(f"{label}: scope must be all or main")
        if action_type == "builtin":
            from eviforge.hooks.defaults import BUILTIN_EVENTS
            if not isinstance(raw_action["builtin"], str) or BUILTIN_EVENTS.get(raw_action["builtin"]) != event:
                raise HookConfigError(f"{label}: builtin does not support this event")
        if reject and event != "pre_tool_use":
            raise HookConfigError(
                f"{label}: 'reject' can only be used with 'pre_tool_use' event"
            )

        async_exec = bool(entry.get("async", False))
        if async_exec and event == "pre_tool_use":
            raise HookConfigError(
                f"{label}: 'async' cannot be used with 'pre_tool_use' event"
            )
        if async_exec and action_type == "builtin":
            raise HookConfigError(f"{label}: builtin checks must run synchronously")

        condition = None
        raw_if = entry.get("if")
        if raw_if:
            try:
                condition = parse_condition(str(raw_if))
            except ConditionParseError as e:
                raise HookConfigError(f"{label}: condition error: {e}") from e

        hook_id = entry.get("id", f"{event}_{i}")

        timeout = raw_action.get("timeout", 30)
        if not isinstance(timeout, int) or timeout <= 0:
            raise HookConfigError(f"{label}: timeout must be a positive integer")

        action = Action(
            type=action_type,
            command=raw_action.get("command", ""),
            message=raw_action.get("message", ""),
            url=raw_action.get("url", ""),
            method=raw_action.get("method", "POST"),
            body=raw_action.get("body", ""),
            headers=raw_action.get("headers", {}),
            prompt=raw_action.get("prompt", ""),
            timeout=timeout,
            builtin=raw_action.get("builtin", ""),
        )

        hooks.append(
            Hook(
                id=hook_id,
                event=event,
                action=action,
                condition=condition,
                reject=reject,
                once=bool(entry.get("once", False)),
                async_exec=async_exec,
                scope=scope,
            )
        )

    return hooks
