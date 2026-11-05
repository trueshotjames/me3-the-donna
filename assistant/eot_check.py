"""v6 (EOT_CHECK=separate): "is the user cut off mid-sentence?" as its own tiny LLM request, next to the early reply.

The v6 Pi run offered <wait/> inside the answer request itself, and that request got worse at its real job: as a
system line after the user's message it suppressed tool calls ("Play some jazz" -> "Playing jazz." with no tool, 6/6
in a replay; "Hang up" 5/6), and inside the user's turn note it broke answers ("fifteen percent of eighty" -> "Twelve
point five" 6/6, "Wait, I meant Tokyo" -> Rome 10/10). It also fired wrongly on "Set a reminder" and "Dad,".

Here the answer request carries no <wait/> offer (the system prompt keeps the rule, which only applies when that offer
is present, so the prompt and its cache are the same as in the Pi run), and this check runs in parallel (text probe on 40 real early-reply
transcripts, 2 rounds: 28/30 cut-offs caught, 48/50 complete turns kept, median 405 ms with gpt-oss-120b on Cerebras).
Its verdict goes to the STT service's llm_verdict: "go" ends the turn locally, "wait" withdraws the early reply and
holds Flux's end of turn for LLM_WAIT_HOLD_SECS.
"""

from __future__ import annotations

import asyncio
import time

from loguru import logger

SYSTEM = (
    "You check live speech transcripts for a voice assistant. The user has just paused. Decide whether their words "
    "are cut off mid-sentence (more is clearly coming) or could be a complete request, question, statement or "
    "answer. Short commands and short answers are complete. Missing final punctuation does not matter. "
    "Reply with one word: cut or done."
)
# v7 (EOT_ADAPTIVE): v6 let her cut in on all three long, rambling fdbench Candor pauses ("Mhmm. But... yeah." -> done)
SYSTEM_V7 = (
    "You check live speech transcripts for a voice assistant. The user has just paused. Decide whether their words "
    "are cut off (more is clearly coming) or could be complete. Short commands, questions and short answers are "
    "complete, with or without final punctuation. But in conversation people pause mid-thought. A remark is cut "
    "if it ends on a word that needs more after it (and, but, or, so, because, since, if, when, then, like, um, uh, "
    "you know, I mean, the, a, to, with, of), if it trails off, or if it only opens a thought it has not made yet "
    "(\"So the thing is\", \"Well, honestly...\"). Reply with one word: cut or done."
)


def prompt(user_text: str, prev: str = "") -> str:
    # the trailing comma of a Flux partial ("Dad,", "Call Mom,") read as "more is coming" (probe, 2/2)
    text = user_text.split("\n\n[", 1)[0].strip().rstrip(",;:").strip()
    return (f'Assistant just said: "{prev[-300:]}"\n' if prev else "") + f'User (paused): "{text}"'


def parse(out: str | None) -> str | None:
    s = (out or "").strip().lower()
    return "wait" if s.startswith("cut") else "go" if s.startswith("done") else None


class CutOffCheck:
    def __init__(self, client, *, model: str, extra_body: dict | None = None, reasoning_effort: str = "",
                 timeout_secs: float = 1.5, adaptive: bool = False):
        self._client = client  # an openai.AsyncOpenAI (the fast LLM's own client)
        self._model = model
        self._extra = extra_body or {}
        self._reasoning = reasoning_effort
        self._timeout = timeout_secs
        self._system = SYSTEM_V7 if adaptive else SYSTEM
        self.stats = {"go": 0, "wait": 0, "unclear": 0, "timeout": 0, "error": 0, "ms": []}

    async def verdict(self, user_text: str, prev: str = "") -> str | None:
        """'go' (complete), 'wait' (cut off) or None (no verdict in time: Flux decides as in v5b)."""
        kw = {"reasoning_effort": self._reasoning} if self._reasoning and "gpt-oss" in self._model else {}
        t0 = time.perf_counter()
        try:
            r = await asyncio.wait_for(
                self._client.chat.completions.create(
                    model=self._model, max_tokens=200, temperature=0, extra_body=self._extra,
                    messages=[{"role": "system", "content": self._system}, {"role": "user", "content": prompt(user_text, prev)}],
                    **kw,
                ),
                self._timeout,
            )
        except asyncio.TimeoutError:
            self.stats["timeout"] += 1
            return None
        except Exception as e:  # noqa: BLE001
            self.stats["error"] += 1
            logger.warning(f"cut-off check failed: {e!r}")
            return None
        ms = (time.perf_counter() - t0) * 1000
        self.stats["ms"].append(round(ms))
        v = parse(r.choices[0].message.content if r.choices else None)
        self.stats[v or "unclear"] += 1
        logger.info(f"cut-off check: {v or 'unclear'} in {ms:.0f} ms: [{prompt(user_text)[16:-1][:80]}]")
        return v
