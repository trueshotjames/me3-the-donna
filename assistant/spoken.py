"""v6 (SPOKEN_HISTORY): what she actually said goes into the conversation history, interrupted replies included.

Pipecat writes her reply to the context from the TTS text frames that reach the assistant aggregator. Two cases lose
it: a speculative reply (released all at once when the turn is confirmed, so its end frame passes before any spoken
text) and any reply the user cuts off after the response's end frame has passed (the aggregator resets on the
interruption). v5b kept 1 of 3 spoken sentences of the lighthouse story and none of the Rome answer; v6, where nearly
every reply is speculative, kept none, so "Wait, I meant Tokyo" reached the model as two user messages in a row.

This observer records each spoken utterance (the TTS text between her starting and stopping or being cut off) with
the user message it answered; ``repair(messages)`` puts a missing one back as an assistant message right after that
user message, or completes a shorter copy of it, before each LLM request.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from loguru import logger
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    InterruptionFrame,
    TTSTextFrame,
)
from pipecat.observers.base_observer import BaseObserver, FramePushed

CUT = " …"  # marks a reply the user cut off


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def _user_text(m: dict) -> str | None:
    if m.get("role") != "user" or not isinstance(m.get("content"), str):
        return None
    return m["content"].split("\n\n[", 1)[0]  # without the per-request turn note


@dataclass
class Utterance:
    after: str  # normalised user message it answered ("" = none yet)
    parts: list[str] = field(default_factory=list)
    interrupted: bool = False
    closed: bool = False
    repaired: int = 0

    @property
    def text(self) -> str:
        return " ".join(p.strip() for p in self.parts if p.strip())


class SpokenHistory(BaseObserver):
    def __init__(self, context, *, keep: int = 8, **kwargs):
        super().__init__(observe_every_push=False, **kwargs)
        self._context = context  # the real LLMContext
        self._keep = keep
        self._open: Utterance | None = None
        self.utterances: list[Utterance] = []
        self._seen: set[int] = set()
        self.stats = {"inserted": 0, "completed": 0}
        self.played_tap = False  # v8: set by main.py when PlayedTextTap feeds played()

    @property
    def context(self):
        return self._context

    def _last_user(self) -> str:
        for m in reversed(self._context.get_messages()):
            t = _user_text(m) if isinstance(m, dict) else None
            if t is not None:
                return _norm(t)
        return ""

    def _close(self, interrupted: bool) -> None:
        u, self._open = self._open, None
        if u is None:
            return
        u.interrupted = interrupted
        u.closed = True
        if u.parts:
            self.utterances.append(u)
            del self.utterances[: -self._keep]

    async def on_push_frame(self, data: FramePushed):
        frame = data.frame
        if id(frame) in self._seen:  # broadcast frames reach observers more than once
            return
        if isinstance(frame, (BotStartedSpeakingFrame, BotStoppedSpeakingFrame, InterruptionFrame, TTSTextFrame)):
            self._seen.add(id(frame))
            if len(self._seen) > 2000:
                self._seen.clear()
        if isinstance(frame, TTSTextFrame):
            if not self.played_tap:
                self.played(frame)
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._close(False)
        elif isinstance(frame, InterruptionFrame):
            self._close(self._open is not None)

    def played(self, frame) -> None:
        """A sentence of hers: from the observer, or (v8 PLAYED_TEXT) from the tap once it has been heard."""
        if not frame.text.strip() or not getattr(frame, "append_to_context", True):
            return
        if self._open is None:
            self._open = Utterance(after=self._last_user())
        self._open.parts.append(frame.text)

    def repair(self, messages: list) -> list | None:
        """The messages with every closed utterance present after the user message it answered; None if unchanged."""
        out = list(messages)
        changed = False
        for u in self.utterances:
            text = u.text
            want = _norm(text)
            if not want:
                continue
            # the user message it answered: the last one with that text (a repeated sentence may pick the later one)
            idx = None
            for i in range(len(out) - 1, -1, -1):
                t = _user_text(out[i]) if isinstance(out[i], dict) else None
                if t is not None and _norm(t) == u.after:
                    idx = i
                    break
            if idx is None:
                continue  # trimmed out of the history, or spoken before any user message
            end = idx + 1
            while end < len(out) and not (isinstance(out[end], dict) and out[end].get("role") == "user"):
                end += 1
            window = [m for m in out[idx + 1:end] if isinstance(m, dict) and m.get("role") == "assistant"]
            have = " ".join(_norm(m.get("content")) for m in window if isinstance(m.get("content"), str))
            if u.interrupted:
                # a copy in the history that runs past what was heard (the unspoken tail of a cut-off reply) is cut
                # back to what was heard, so the model is never told words she did not say
                longer = next((m for m in window if isinstance(m.get("content"), str) and _norm(m["content"]) != want
                               and _norm(m["content"]).startswith(want)), None)
                if longer is not None:
                    longer["content"] = text + CUT
                    self.stats["trimmed"] = self.stats.get("trimmed", 0) + 1
                    u.repaired += 1
                    changed = True
                    logger.info(f"spoken history: cut her reply to [{u.after[:40]}] back to what she said: [{text[:80]}]")
                    continue
            if want in have:
                continue
            spoken = text + (CUT if u.interrupted else "")
            short = next((m for m in window if isinstance(m.get("content"), str) and _norm(m["content"])
                          and want.startswith(_norm(m["content"]))), None)
            if short is not None:
                short["content"] = spoken  # Pipecat kept only the first part of what she said
                self.stats["completed"] += 1
            else:
                out.insert(end, {"role": "assistant", "content": spoken})
                self.stats["inserted"] += 1
            u.repaired += 1
            changed = True
            logger.info(f"spoken history: {'completed' if short is not None else 'added'} her reply to "
                        f"[{u.after[:40]}]{' (cut off)' if u.interrupted else ''}: [{text[:80]}]")
        return out if changed else None
