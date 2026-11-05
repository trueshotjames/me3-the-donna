"""v4: re-prompt once after silence (scenario 15: "re-prompt once, then idle").

v3 asked "When should I remind you?" and then stayed silent for the rest of the 40 s tail. When her reply ends on a
question and nobody speaks for REPROMPT_SECS after she stops, she asks once more ("Are you still there? ..."); after
that she stays idle. Any voice, user turn or new reply in the meantime cancels it.
v5: only for questions that ask for a missing detail (what / when / which / who / how long ...), not optional offers.

No CPU cost on the Pi: one timer per question.
"""

from __future__ import annotations

import asyncio
import re
from typing import Awaitable, Callable

from loguru import logger
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    LLMFullResponseStartFrame,
    TTSTextFrame,
    UserStartedSpeakingFrame,
    VADUserStartedSpeakingFrame,
)
from pipecat.observers.base_observer import BaseObserver, FramePushed

PREFIX = "Are you still there?"
_QUESTION = re.compile(r"[^.!?]*\?\s*$")
# v5: only questions that ask for a missing detail ("When should I remind you?") are asked again; optional offers
# ("Would you like the forecast for tomorrow as well?") are left alone (scenario 11 asked one twice in v4).
_SLOT = re.compile(r"^\s*(what|when|which|who|whom|where|how (long|many|much|often)|at what)\b", re.IGNORECASE)
_OPTIONAL = re.compile(r"\b(as well|anything else|too|also)\s*\?\s*$", re.IGNORECASE)


def slot_question(question: str) -> bool:
    return bool(_SLOT.match(question)) and not _OPTIONAL.search(question)


class Reprompter(BaseObserver):
    def __init__(self, say: Callable[[str], Awaitable[None]], secs: float = 8.0, **kwargs):
        super().__init__(**kwargs)
        self._say = say
        self._secs = secs
        self._reply = ""  # text of the reply she is speaking now
        self._task: asyncio.Task | None = None
        self._seen: set[int] = set()
        self.count = 0

    def _cancel(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def on_push_frame(self, data: FramePushed):
        frame = data.frame
        if not self._secs or frame.id in self._seen:
            return
        if isinstance(frame, (UserStartedSpeakingFrame, VADUserStartedSpeakingFrame, BotStartedSpeakingFrame)):
            self._seen.add(frame.id)
            self._cancel()
            if isinstance(frame, UserStartedSpeakingFrame):
                self._reply = ""
        elif isinstance(frame, LLMFullResponseStartFrame):
            self._seen.add(frame.id)
            self._cancel()
            self._reply = ""
        elif isinstance(frame, TTSTextFrame):
            self._seen.add(frame.id)
            if frame.text.strip():
                self._reply = f"{self._reply} {frame.text.strip()}".strip()
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._seen.add(frame.id)
            m = _QUESTION.search(self._reply)
            if m and PREFIX not in self._reply and slot_question(m.group(0)):
                self._cancel()
                self._task = asyncio.create_task(self._later(m.group(0).strip()))
        if len(self._seen) > 2000:
            self._seen.clear()

    async def _later(self, question: str) -> None:
        try:
            await asyncio.sleep(self._secs)
        except asyncio.CancelledError:
            return
        self._task = None
        self.count += 1
        logger.info(f"no answer for {self._secs:.0f} s; asking once more: [{question}]")
        self._reply = f"{PREFIX} {question}"
        try:
            await self._say(f"{PREFIX} {question}")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"re-prompt failed: {e!r}")
