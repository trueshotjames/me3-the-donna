"""v11 (HOLD_TOOL_CALLS): a tool call from an early (speculative) reply waits for the turn instead of being dropped.

Pipecat 1.13 (``LLMService.run_function_calls``) withdraws the whole early reply when it asks for a tool, because a
tool runs inside the service and its side effects cannot be undone if the guess about the end of turn was wrong. The
committed turn then runs the LLM a second time, which asks for the same tool, ~0.3-0.8 s later.

With the flag on, the call (every tool, ``delegate`` included) is kept here, unexecuted, and the early reply stays
held by Pipecat's SpeculationGate as it would for a plain-text answer. The same signals that settle the gate settle
the held call:

- ``UserStoppedSpeakingFrame`` after an eager end of turn that matched the committed transcript (Pipecat's
  ``EagerUserTurnStopStrategy`` with its match policy): the turn is confirmed, so the call runs, once, against the
  real context. Pipecat does not run the LLM again for a confirmed turn (``run_llm=not confirms_speculation``).
- ``EagerEndOfTurnCancelFrame`` (the user resumed, the committed words differ, the cut-off check said "cut", or the
  5 s speculation timeout), ``InterruptionFrame``, ``UserStartedSpeakingFrame``, a new inference, or shutdown: the
  call is dropped without running, and the committed turn is answered by a fresh inference as before.

Safeguards (each one a reason a held call never runs early):
1. The call is only released by a ``UserStoppedSpeakingFrame`` seen *after* it was held, and only after the
   gate has stopped speculating.
2. A second, local check: the committed transcript (from the STT service) must match the early text. A mismatch
   drops the call and asks the LLM again, which is what happens without the flag.
3. Anything that ends or replaces the speculation drops it, including an interruption between the confirmation and
   the start of the call.

This module holds the bookkeeping only; ``assistant/services/timed_llm.py`` wires it into the LLM service.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from loguru import logger

from assistant.voice_gate import norm


@dataclass
class HeldCalls:
    calls: list  # FunctionCallFromLLM
    user_text: str  # the early (speculative) transcript the calls answer
    t_held: float = field(default_factory=time.time)
    record: dict | None = None  # the LLM call entry in the turn record (turn_metrics.py)
    user_msgs_at_hold: int = 0  # user messages in the real context when held (the committed one comes next)

    @property
    def names(self) -> list[str]:
        return [c.function_name for c in self.calls]


class ToolHold:
    def __init__(self, enabled: bool = False):
        self.enabled = enabled
        self.held: HeldCalls | None = None
        self.stats = {"held": 0, "released": 0, "dropped": 0, "mismatch": 0}
        self.events: list[dict] = []  # every outcome, for tests and the A/B run

    def hold(self, held: HeldCalls) -> None:
        if self.held is not None:
            self.drop("superseded by a newer held call")
        self.held = held
        self.stats["held"] += 1
        if held.record is not None:
            held.record["tool_held"] = held.names
            held.record["t_tool_held"] = held.t_held
        self._event("held", held)
        logger.info(f"tool hold: early reply asked for {held.names}; holding until the turn is confirmed: [{held.user_text}]")

    def drop(self, reason: str) -> HeldCalls | None:
        held, self.held = self.held, None
        if held is None:
            return None
        self.stats["dropped"] += 1
        if held.record is not None:
            held.record["tool_hold"] = f"dropped: {reason}"
        self._event("dropped", held, reason=reason)
        logger.info(f"tool hold: dropped {held.names} without running ({reason})")
        return held

    def take(self) -> HeldCalls | None:
        """The turn was confirmed: hand the held calls over to run (the caller still checks the transcript)."""
        held, self.held = self.held, None
        return held

    def released(self, held: HeldCalls, wait_ms: float) -> None:
        self.stats["released"] += 1
        if held.record is not None:
            held.record["tool_hold"] = "released"
            held.record["t_tool_released"] = time.time()
        self._event("released", held, wait_ms=round(wait_ms, 1))
        logger.info(f"tool hold: turn confirmed; running {held.names} (held {wait_ms:.0f} ms)")

    def mismatch(self, held: HeldCalls, committed: str) -> None:
        self.stats["mismatch"] += 1
        self.stats["dropped"] += 1
        if held.record is not None:
            held.record["tool_hold"] = "dropped: committed transcript differs"
        self._event("dropped", held, reason="committed transcript differs", committed=committed)
        logger.info(f"tool hold: committed [{committed}] differs from early [{held.user_text}]; dropped {held.names}")

    @staticmethod
    def matches(early: str, committed: str | None) -> bool:
        return committed is None or norm(early) == norm(committed)

    def _event(self, kind: str, held: HeldCalls, **extra) -> None:
        self.events.append({"t": time.time(), "kind": kind, "tools": held.names, "text": held.user_text, **extra})
