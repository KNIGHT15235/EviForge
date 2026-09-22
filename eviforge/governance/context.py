"""Exclude re-injectable governed sources from summaries and copied skill context."""

from __future__ import annotations

import copy
import re

from eviforge.conversation import Message

SKILL_BLOCK = re.compile(
    r'<eviforge-governed-skill name="(?P<name>[a-z][a-z0-9-]*)" hash="(?P<hash>[0-9a-f]{64})">\n'
    r'.*?\n</eviforge-governed-skill>\n\[end-governed-skill (?P=hash)\]', re.DOTALL,
)


def strip_governed_context(messages: list[Message]) -> list[Message]:
    result = []
    for original in messages:
        message = copy.deepcopy(original)
        if message.role == "user" and not message.tool_uses and not message.tool_results:
            if (message.content.startswith("<eviforge-governed-memory>\n")
                    and message.content.endswith("\n</eviforge-governed-memory>")):
                continue
            if SKILL_BLOCK.fullmatch(message.content):
                continue
            if message.content.startswith("Current working directory: "):
                message.content = SKILL_BLOCK.sub("[Governed skill omitted; runtime reloads the current approved version.]", message.content)
        result.append(message)
    return result
