"""Show warnings and errors that would otherwise only reach the log as UI notifications.

A loguru sink at WARNING level forwards each new message to the UI bus as a `warning` or `error`
event (a 10 s toast in the app). Messages the UI already shows another way are skipped, and the
same message (numbers ignored) is sent at most once per 10 s so an error storm stays one toast.
Nothing is forwarded before `enable()` (startup noise such as the tools server not running yet).
"""

from __future__ import annotations

import asyncio
import re
import threading
import time

from loguru import logger

from assistant.ui_bus import UIBus

THROTTLE_S = 10.0

# Already shown by another event (pipeline `error`, `llm_wait`, the no-tool-call `warning`, turn
# problems) or harmless.
_SKIP = (
    "pipeline error:",  # ErrorFrame -> `error` event (ui_events.py)
    "Something went wrong: ErrorFrame",  # same ErrorFrame, logged by the worker
    " exception (",  # processor exception, pushed as an ErrorFrame too
    "Groq 429 rate limit",  # `llm_wait` countdown
    "openai sdk: Retrying",
    "LLM claimed a tool action",  # ui_events.py sends its own warning
    "turn problem:",  # turn_metrics sends its own warning
    "completed with no audio (",  # followed by an ErrorFrame
    "TTS failed:",  # followed by an ErrorFrame
    "DeprecationWarning",
)

_NUM = re.compile(r"\d+(\.\d+)?")


class LogNotifier:
    def __init__(self, bus: UIBus):
        self._bus = bus
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread = threading.get_ident()
        self._enabled = False
        self._last: dict[str, float] = {}
        self._sink_id: int | None = None

    def install(self) -> None:
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None
        self._thread = threading.get_ident()
        self._sink_id = logger.add(self._sink, level="WARNING", format="{message}", catch=True)

    def enable(self) -> None:
        self._enabled = True

    def remove(self) -> None:
        if self._sink_id is not None:
            logger.remove(self._sink_id)
            self._sink_id = None

    def _sink(self, message) -> None:
        if not self._enabled:
            return
        rec = message.record
        text = rec["message"].strip()
        if not text or any(s in text for s in _SKIP):
            return
        key = _NUM.sub("N", text)[:160]
        now = time.monotonic()
        if now - self._last.get(key, -1e9) < THROTTLE_S:
            return
        self._last[key] = now
        kind = "error" if rec["level"].no >= 40 else "warning"
        msg = text if len(text) <= 220 else text[:217] + "..."
        self._send(kind, msg)

    def _send(self, kind: str, msg: str) -> None:
        try:
            if self._loop is not None and threading.get_ident() != self._thread:
                self._loop.call_soon_threadsafe(lambda: self._bus.emit(kind, message=msg, source="log"))
            else:
                self._bus.emit(kind, message=msg, source="log")
        except Exception:  # noqa: BLE001  never let the UI path break logging
            pass
