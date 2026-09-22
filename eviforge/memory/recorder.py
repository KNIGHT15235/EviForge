"""Persist newly appended messages without depending on mutable history offsets."""

from __future__ import annotations

import weakref
from typing import Iterable

from eviforge.conversation import ConversationManager, Message
from eviforge.memory.session import Session, make_compact_boundary


class SessionRecorder:
    def __init__(self, session: Session, already_saved: Iterable[Message] = ()) -> None:
        self.session = session
        self._saved: weakref.WeakValueDictionary[int, Message] = weakref.WeakValueDictionary()
        self.mark_saved(already_saved)

    def mark_saved(self, messages: Iterable[Message]) -> None:
        for message in messages:
            self._saved[id(message)] = message

    def flush(self, conversation: ConversationManager) -> int:
        written = 0
        for message in conversation.history:
            if self._saved.get(id(message)) is message:
                continue
            if not getattr(message, "_generated_context", False):
                self.session.append(message)
                written += 1
            self._saved[id(message)] = message
        return written

    def compacted(self, boundary, conversation: ConversationManager) -> None:
        keep = [message for message in boundary.keep if not getattr(message, "_generated_context", False)]
        self.session.append_record(make_compact_boundary(boundary.summary, keep))
        self.mark_saved(conversation.history)
