"""v11 (TOOL_COMMENT): a short tail of Donna's own after a fixed tool reply, or nothing.

For create_reminder, send_message (only the real send, after the user's yes) and a plain weather question
(WEATHER_DIRECT), the fixed reply from fast_confirm.py is spoken at once, exactly as before. At the same moment a
small, separate LLM request (no tools, no history: the user's words, the fixed reply and the tool result) asks for at
most TAIL_MAX_WORDS words that continue the fixed reply, or <pass/>. It runs while the fixed reply plays, so a tail
that is ready follows it like the rest of one sentence.

Why a separate request: Pipecat's assistant aggregator holds a run_llm=True tool result until the bot has stopped
speaking, so a tail from the normal pipeline would always come after a pause (and could call tools again).

Safeguards (each one drops the tail; the fixed reply is never touched):
1. <pass/>, empty, more than TAIL_MAX_WORDS words, more than one sentence, a question, or markup.
2. It repeats the fixed reply (most of its words are already in it).
3. It is not back before the fixed reply should have finished playing (estimated from its length and her speaking
   rate): a late tail would sound like a second, separate answer. The request is cancelled at that moment.
4. The user starts talking (interruption) before it is ready.
5. The request has no tools, so it cannot call get_weather or send_message a second time.
6. Any error or rate limit: no tail, nothing said about it.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any

from loguru import logger

ENABLED = False  # set by main.py (TOOL_COMMENT)
TOOLS = {"create_reminder", "send_message", "get_weather"}  # set by main.py (TOOL_COMMENT_TOOLS)
TAIL_MAX_WORDS = 8
WORDS_PER_SEC = 2.7  # Aura-2 at speed 1.0 speaks about 2.5-3 words a second
LEAD_SECS = 0.5  # TTS time to first audio, before the fixed reply starts playing
PASS_TOKEN = "<pass"

SYSTEM = (
    "You are Donna, {user}'s voice assistant: confident, quick, warm underneath, with a dry wit. You just said a short "
    "fixed reply aloud. You may continue it with at most {n} words that follow on naturally, as the end of the same "
    "reply: a small tip or a warm or witty word that fits the moment. Use only facts given here. If nothing natural "
    "fits, reply exactly <pass/>. When unsure, pass. Never repeat or rephrase what you said, never ask a question, "
    "never claim to have done anything else. Reply with the words only."
)

_WORD = re.compile(r"[a-z0-9']+")


def _words(text: str) -> list[str]:
    return _WORD.findall((text or "").lower())


def speak_secs(text: str, speed: float = 1.0) -> float:
    """Roughly how long the fixed reply takes until its last word has played."""
    return LEAD_SECS + len(_words(text)) / (WORDS_PER_SEC * max(speed, 0.5))


def verdict(said: str, tail: str) -> str | None:
    """Why this tail must not be spoken, or None when it is fine."""
    words = _words(tail)
    if not words:
        return "empty"
    if len(words) > TAIL_MAX_WORDS:
        return f"{len(words)} words"
    if "?" in tail:
        return "a question"
    if "<" in tail or "{" in tail or "*" in tail:
        return "markup"
    if re.search(r"[.!;](?=\s+\S)", tail):
        return "more than one sentence"
    have = set(_words(said))
    content = [w for w in words if len(w) > 3]
    if content and sum(w in have for w in content) / len(content) >= 0.6:
        return "repeats the fixed reply"
    return None


def clean(text: str | None) -> str:
    return " ".join((text or "").split()).strip().strip('"').strip()


def user_message(user_text: str, said: str, result_text: str) -> str:
    try:
        result: Any = json.loads(result_text)
    except (TypeError, ValueError):
        result = result_text
    if isinstance(result, dict):  # keep it small: the UI-only fields are not needed
        result = {k: v for k, v in result.items() if k not in ("icon", "days", "hourly")}
    return (
        f'User said: "{(user_text or "").strip()[:300]}"\n'
        f"Tool result: {json.dumps(result, default=str)[:800]}\n"
        f'You said aloud: "{said}"\n'
        f"Your continuation (at most {TAIL_MAX_WORDS} words) or <pass/>:"
    )


class Tail:
    """At most one tail at a time. Started by fast_confirm.py, cancelled by PassFilter on an interruption."""

    def __init__(self):
        self.client = None  # openai.AsyncOpenAI (the fast LLM's own client), set by main.py
        self.model = ""
        self.extra_body: dict = {}
        self.reasoning_effort = ""
        self.user_name = "the user"
        self.speed_of = lambda: 1.0  # her current speaking rate (set_speaking_rate changes it), set by main.py
        self.task: asyncio.Task | None = None
        self.stats = {"started": 0, "spoken": 0, "passed": 0, "dropped": 0}
        self.events: list[dict] = []  # every outcome, for tests and the test run

    def start(self, llm, said: str, result_text: str, user_text: str) -> None:
        """Ask for a tail in the background; `llm` is the Pipecat LLM service the tail is pushed from."""
        if self.client is None:
            return
        self.cancel("a newer tool reply")
        deadline = time.time() + speak_secs(said, float(self.speed_of() or 1.0))
        self.stats["started"] += 1
        self.task = asyncio.get_running_loop().create_task(self._run(llm, said, result_text, user_text, deadline))

    def cancel(self, why: str) -> None:
        task, self.task = self.task, None
        if task is not None and not task.done():
            task.cancel()
            self._end("dropped", why, "", "")

    def _end(self, outcome: str, why: str, text: str, said: str, ms: float | None = None) -> None:
        self.stats[outcome] += 1
        self.events.append({"outcome": outcome, "why": why, "text": text, "said": said, "ms": ms})
        logger.info(
            f"tool comment {outcome}{' (' + why + ')' if why else ''}"
            f"{f' in {ms:.0f} ms' if ms is not None else ''}: {text!r} after {said!r}"
        )

    async def ask(self, said: str, result_text: str, user_text: str, timeout: float) -> str | None:
        """The raw tail text from the LLM. Raises asyncio.TimeoutError when it is not back in time."""
        kw = {"reasoning_effort": self.reasoning_effort} if self.reasoning_effort and "gpt-oss" in self.model else {}
        r = await asyncio.wait_for(
            self.client.chat.completions.create(
                model=self.model, max_tokens=300, temperature=0.7, extra_body=self.extra_body,
                messages=[
                    {"role": "system", "content": SYSTEM.format(user=self.user_name, n=TAIL_MAX_WORDS)},
                    {"role": "user", "content": user_message(user_text, said, result_text)},
                ],
                **kw,
            ),
            max(timeout, 0.05),
        )
        return r.choices[0].message.content if r.choices else ""

    async def _run(self, llm, said: str, result_text: str, user_text: str, deadline: float) -> None:
        t0 = time.time()
        try:
            raw = await self.ask(said, result_text, user_text, deadline - t0)
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            self.task = None
            self._end("dropped", "not ready before the fixed reply ended", "", said, (time.time() - t0) * 1000)
            return
        except Exception as e:  # noqa: BLE001
            self.task = None
            self._end("dropped", f"error {type(e).__name__}", "", said)
            return
        ms = (time.time() - t0) * 1000
        self.task = None
        text = clean(raw)
        if not text or text.lower().startswith(PASS_TOKEN):
            self._end("passed", "", text, said, ms)
            return
        why = verdict(said, text)
        if why is None and time.time() > deadline:
            why = "not ready before the fixed reply ended"
        if why is not None:
            self._end("dropped", why, text, said, ms)
            return
        if text[-1:] not in ".!":
            text += "."
        self._end("spoken", "", text, said, ms)
        from pipecat.frames.frames import LLMFullResponseEndFrame, LLMFullResponseStartFrame, LLMTextFrame

        await llm.push_frame(LLMFullResponseStartFrame())
        await llm.push_frame(LLMTextFrame(text))
        await llm.push_frame(LLMFullResponseEndFrame())


TAIL = Tail()
