"""The conversation of this session, for the UI chat box.

Fed by UIEventObserver (final user transcripts, sentences handed to TTS, barge-ins, tool calls).
Kept in memory only, capped at ``max_items``; the UI gets the whole list on connect
(``chat_history``) and one ``chat`` event per added or updated entry. One assistant entry
collects the sentences of one reply; a new entry starts after a user turn, or when she starts
speaking again more than ``gap_secs`` after she stopped (a reminder, a Hermes result).
"""

from __future__ import annotations

import time
from collections import deque
from typing import Any

from assistant.ui_bus import UIBus


class ChatLog:
    def __init__(self, bus: UIBus, *, max_items: int = 200, gap_secs: float = 2.0):
        self._bus = bus
        self._items: deque[dict[str, Any]] = deque(maxlen=max_items)
        self._gap = gap_secs
        self._next_id = 1
        self._open: dict[str, Any] | None = None  # assistant entry still collecting sentences
        self._bot_stopped_at: float | None = None

    def _add(self, role: str, text: str, **extra: Any) -> dict[str, Any]:
        item = {"id": self._next_id, "role": role, "text": text, "t": round(time.time(), 3), **extra}
        self._next_id += 1
        self._items.append(item)
        self._bus.emit("chat", item=item)
        return item

    def user(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        self._open = None
        self._add("user", text)

    def assistant(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        stale = self._bot_stopped_at is not None and time.time() - self._bot_stopped_at > self._gap
        if self._open is None or stale or self._open not in self._items:
            self._open = self._add("assistant", text, interrupted=False)
        else:
            self._open["text"] = f"{self._open['text']} {text}"
            self._bus.emit("chat", item=self._open)
        self._bot_stopped_at = None

    def bot_stopped(self) -> None:
        self._bot_stopped_at = time.time()

    def interrupted(self) -> None:
        if self._open is not None and not self._open.get("interrupted"):
            self._open["interrupted"] = True
            self._bus.emit("chat", item=self._open)
        self._open = None

    def tool(self, name: str, ok: bool) -> None:
        self._add("tool", name, ok=ok)

    def clear(self) -> None:
        self._items.clear()
        self._open = None
        self._bus.emit("chat_history", items=[])

    @property
    def items(self) -> list[dict[str, Any]]:
        return list(self._items)

    async def snapshot(self) -> list[dict[str, Any]]:
        return [{"type": "chat_history", "ts": int(time.time() * 1000), "items": self.items}]

    async def handle_command(self, msg: dict[str, Any]) -> None:
        if msg.get("type") == "chat_clear":
            self.clear()
