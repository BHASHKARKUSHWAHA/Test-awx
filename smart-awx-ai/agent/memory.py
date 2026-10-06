"""Conversation memory (spec sections 29 and 32).

Kept deliberately small: an in-process ring buffer per conversation, redacted
on the way in, never written to disk.  Secrets, tokens and keys therefore
cannot leak from memory into a later prompt.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .audit import redact, redact_text

MAX_CONVERSATIONS = 50
MAX_MESSAGES = 40
MAX_MESSAGE_CHARS = 6000
IDLE_TIMEOUT_SECONDS = 6 * 3600


@dataclass
class Message:
    role: str  # user | assistant | system
    content: str
    created_at: float = field(default_factory=time.time)
    meta: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {"role": self.role, "content": self.content, "created_at": self.created_at, "meta": self.meta}


@dataclass
class Conversation:
    id: str
    user: str
    messages: List[Message] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    # Short-lived facts the planner may reuse within one conversation, e.g. the
    # pod name the user is currently asking about.  Never credentials.
    context: Dict[str, Any] = field(default_factory=dict)

    def add(self, role: str, content: str, meta: Optional[Dict[str, Any]] = None) -> Message:
        message = Message(
            role=role,
            content=redact_text(str(content))[:MAX_MESSAGE_CHARS],
            meta=redact(meta or {}),
        )
        self.messages.append(message)
        if len(self.messages) > MAX_MESSAGES:
            del self.messages[: len(self.messages) - MAX_MESSAGES]
        self.updated_at = time.time()
        return message

    def history(self, limit: int = 12) -> List[Dict[str, str]]:
        return [{"role": m.role, "content": m.content} for m in self.messages[-limit:]]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "user": self.user,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "messages": [m.to_dict() for m in self.messages],
        }


class MemoryStore:
    def __init__(self) -> None:
        self._items: "OrderedDict[str, Conversation]" = OrderedDict()
        self._guard = threading.Lock()

    def get_or_create(self, conversation_id: Optional[str], user: str) -> Conversation:
        with self._guard:
            self._expire()
            if conversation_id and conversation_id in self._items:
                conversation = self._items[conversation_id]
                self._items.move_to_end(conversation_id)
                return conversation
            new_id = conversation_id or uuid.uuid4().hex[:16]
            conversation = Conversation(id=new_id, user=user)
            self._items[new_id] = conversation
            while len(self._items) > MAX_CONVERSATIONS:
                self._items.popitem(last=False)
            return conversation

    def get(self, conversation_id: str) -> Optional[Conversation]:
        with self._guard:
            return self._items.get(conversation_id)

    def list_for(self, user: str) -> List[Dict[str, Any]]:
        with self._guard:
            return [
                {
                    "id": c.id,
                    "updated_at": c.updated_at,
                    "message_count": len(c.messages),
                    "preview": next((m.content[:80] for m in c.messages if m.role == "user"), ""),
                }
                for c in reversed(self._items.values())
                if c.user == user
            ]

    def clear(self, conversation_id: str) -> bool:
        with self._guard:
            return self._items.pop(conversation_id, None) is not None

    def _expire(self) -> None:
        cutoff = time.time() - IDLE_TIMEOUT_SECONDS
        for key in [k for k, c in self._items.items() if c.updated_at < cutoff]:
            del self._items[key]


store = MemoryStore()
