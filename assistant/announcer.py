"""One queue for everything the assistant says on her own (not in reply to the user).

Sources today: Hermes results, failures and "still working" nudges (assistant/tasks.py), timed
reminders and alarms (assistant/scheduler.py), and "One moment" while the LLM waits out a rate
limit. Anything added later (another model, a direct tool result, a notification) submits an
Announcement here instead of touching the pipeline itself.

Each announcement says how it is spoken and what it waits for:

    mode   say    the text goes to the TTS as is (TTSSpeakFrame)
           tell   the text is a message for the fast model, which puts it in her own words
                  (LLMMessagesAppendFrame, run_llm=True); used for Hermes results

    wait   turn   the user's current query comes first: wait until no user turn is open (he has
                  stopped talking AND her full reply, tool calls included, has finished playing)
                  and the conversation has been quiet for QUIET_SECS. Default.
           speech wait only until nobody is talking (alarms: they may cut into her reply, but
                  not into his sentence)
           none   speak at once ("One moment" while the LLM itself is stuck)

The queue is ordered by priority, then arrival. An announcement can expire (ttl), can be dropped
when it is no longer wanted (still_wanted: a nudge after the result is in), and is re-queued
once if the user barges in while it is being spoken (retry_text).

Whether anyone is talking comes from the ActivityTracker, a pass-through processor placed right
after the LLM service; it also tells the announcer when a spoken announcement has finished.
"""

from __future__ import annotations

import asyncio
import itertools
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

from loguru import logger
from pipecat.frames.frames import (
    BotSpeakingFrame,
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    FunctionCallCancelFrame,
    FunctionCallResultFrame,
    FunctionCallsStartedFrame,
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMMessagesAppendFrame,
    LLMTextFrame,
    TTSSpeakFrame,
    UserSpeakingFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
    VADUserStartedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

QUIET_SECS = 0.8  # the conversation must be idle this long before a "turn" announcement starts
MAX_HOLD_SECS = 20.0  # speak anyway after this long (a stuck flag must not hold an announcement)
SPEECH_HOLD_SECS = 4.0  # "speech" announcements (alarms) wait at most this long for his sentence
SPOKEN_TIMEOUT_SECS = 30.0  # an announcement that never produced speech counts as done after this

# priorities (higher first)
PRIORITY_ALARM = 100
PRIORITY_URGENT = 90  # "One moment"
PRIORITY_REMINDER = 50
PRIORITY_RESULT = 10
PRIORITY_LOW = 0  # nudges


class ActivityTracker(FrameProcessor):
    """Knows whether anyone is talking, whether the LLM is busy, and whether a user turn is open.

    Sits right after the LLM service: sees user turn and VAD frames (broadcast), the LLM's
    response and function call frames (downstream), TTSSpeakFrames queued by the announcer
    (downstream) and the bot speaking frames (upstream from the output transport).

    A user turn opens when he starts speaking and closes when her reply has finished playing
    (bot stopped, no LLM response or tool call in flight), or when nothing has happened for
    TURN_IDLE seconds (she answered <pass/>, or the ASR dropped the turn).

    Every flag also expires on its own: a frame that never comes (an early reply cancelled by an
    interruption has no LLMFullResponseEndFrame, a cancelled tool call no result) must not keep
    the conversation "busy" forever. The second laptop test held a result 120 s that way.
    """

    USER_STALE = 2.5  # UserSpeakingFrame repeats every ~1 s while the user talks
    BOT_STALE = 2.0  # BotSpeakingFrame repeats every ~0.2 s while she talks
    LLM_STALE = 10.0  # no LLM frame for this long = the response is over
    CALL_STALE = 30.0  # a tool call without a result after this long is forgotten
    TURN_IDLE = 3.0  # a user turn with no activity for this long is over (covers TTS start-up and <pass/>)
    TURN_STALE = 45.0  # a user turn is never held open longer than this

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        now = time.monotonic()
        self._user = self._bot = self._llm = None  # monotonic time of the last sign, or None when off
        self._calls: dict[str, float] = {}
        self._turn: float | None = None  # when the open user turn started
        self._last = now
        self._listeners: list[Callable[[Frame], None]] = []

    def add_listener(self, fn: Callable[[Frame], None]) -> None:
        """Called with every frame the tracker sees (after its own bookkeeping)."""
        self._listeners.append(fn)

    # flags with expiry
    def _on(self, t: float | None, stale: float) -> bool:
        return t is not None and time.monotonic() - t < stale

    @property
    def user_speaking(self) -> bool:
        return self._on(self._user, self.USER_STALE)

    @user_speaking.setter
    def user_speaking(self, v: bool) -> None:
        self._user = time.monotonic() if v else None
        if v:
            self._open_turn()

    @property
    def bot_speaking(self) -> bool:
        return self._on(self._bot, self.BOT_STALE)

    @bot_speaking.setter
    def bot_speaking(self, v: bool) -> None:
        self._bot = time.monotonic() if v else None

    @property
    def llm_active(self) -> bool:
        return self._on(self._llm, self.LLM_STALE)

    @llm_active.setter
    def llm_active(self, v: bool) -> None:
        self._llm = time.monotonic() if v else None

    @property
    def pending_calls(self) -> set[str]:
        now = time.monotonic()
        return {k for k, t in self._calls.items() if now - t < self.CALL_STALE}

    def add_call(self, call_id: str) -> None:
        self._calls[call_id] = time.monotonic()

    def clear_calls(self) -> None:
        self._calls.clear()

    @property
    def busy(self) -> bool:
        return self.user_speaking or self.bot_speaking or self.llm_active or bool(self.pending_calls)

    @property
    def turn_open(self) -> bool:
        """The user has spoken and his query has not been fully answered yet."""
        if self._turn is None:
            return False
        now = time.monotonic()
        if now - self._turn >= self.TURN_STALE:
            self._turn = None
            return False
        if self.busy:
            return True
        if now - self._last >= self.TURN_IDLE:
            self._turn = None
            return False
        return True

    def _open_turn(self) -> None:
        if self._turn is None:
            self._turn = time.monotonic()

    def close_turn(self) -> None:
        self._turn = None

    def state(self) -> str:
        return (
            f"user={self.user_speaking} bot={self.bot_speaking} llm={self.llm_active} "
            f"calls={sorted(self.pending_calls)} turn={self.turn_open}"
        )

    def idle_for(self) -> float:
        """Seconds since the last sign of activity (0 while busy)."""
        if self.busy:
            return 0.0
        return time.monotonic() - self._last

    def _touch(self) -> None:
        self._last = time.monotonic()

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        now = time.monotonic()
        activity = True
        if isinstance(frame, (UserStartedSpeakingFrame, VADUserStartedSpeakingFrame)):
            self._user = now
            self._open_turn()
        elif isinstance(frame, UserSpeakingFrame):
            self._user = now
        elif isinstance(frame, UserStoppedSpeakingFrame):
            self._user = None
        elif isinstance(frame, BotStartedSpeakingFrame):
            self._bot = now
        elif isinstance(frame, BotSpeakingFrame):
            self._bot = now
            activity = False  # arrives 5x a second; the start/stop frames are the events
            self._last = now
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._bot = None
            if not self.llm_active and not self.pending_calls:
                self._turn = None  # her reply has finished playing: the user's turn is answered
        elif isinstance(frame, (LLMFullResponseStartFrame, LLMTextFrame, TTSSpeakFrame)):
            self._llm = now
        elif isinstance(frame, LLMFullResponseEndFrame):
            self._llm = None
        elif isinstance(frame, FunctionCallsStartedFrame):
            for fc in frame.function_calls:
                self._calls[fc.tool_call_id] = now
        elif isinstance(frame, (FunctionCallResultFrame, FunctionCallCancelFrame)):
            self._calls.pop(frame.tool_call_id, None)
        elif isinstance(frame, InterruptionFrame):
            # The user barged in: whatever the LLM was producing is gone, and so are its tool calls.
            self._llm = None
            self._calls.clear()
            self._open_turn()
        else:
            activity = False
        if activity:
            self._touch()
        for fn in self._listeners:
            try:
                fn(frame)
            except Exception as e:  # noqa: BLE001
                logger.debug(f"activity listener failed: {e!r}")
        await self.push_frame(frame, direction)


Mode = Literal["say", "tell"]
Wait = Literal["turn", "speech", "none"]


@dataclass
class Announcement:
    text: str
    source: str  # "hermes", "reminder", "alarm", "llm_wait", ... (for the log and the UI)
    mode: Mode = "say"
    wait: Wait = "turn"
    priority: int = PRIORITY_RESULT
    interruptible: bool = True
    append_to_context: bool = True  # "say" only: whether the spoken text joins the conversation history
    ttl: float | None = None  # seconds from submission after which it is dropped unspoken
    still_wanted: Callable[[], bool] | None = None  # checked just before speaking
    retry_text: str | None = None  # re-queued once with this text if the user cuts it off
    max_hold: float | None = None  # default by wait mode
    on_done: Callable[["Announcement", str], None] | None = None  # outcome: spoken | dropped:<why> | interrupted
    meta: dict[str, Any] = field(default_factory=dict)
    # filled in by the announcer
    seq: int = 0
    t_submitted: float = 0.0
    t_spoken: float | None = None
    retries: int = 0

    @property
    def expired(self) -> bool:
        return self.ttl is not None and time.monotonic() - self.t_submitted > self.ttl


class Announcer:
    """Speaks announcements one at a time, each at the right moment. attach() before use."""

    def __init__(self, activity: ActivityTracker | None = None, *, on_event=None):
        self._activity = activity
        self._emit = on_event or (lambda kind, **f: None)
        self._queue: list[Announcement] = []
        self._seq = itertools.count(1)
        self._wake = asyncio.Event()
        self._worker = None  # PipelineWorker: TTSSpeakFrames
        self._assistant = None  # assistant context aggregator: LLMMessagesAppendFrames
        self._runner: asyncio.Task | None = None
        self._current: Announcement | None = None
        self._phase = ""  # sent | speaking
        self._outcome: asyncio.Future | None = None
        self._muted = False
        if activity is not None:
            activity.add_listener(self._on_frame)

    def attach(self, worker, assistant_aggregator) -> None:
        self._worker = worker
        self._assistant = assistant_aggregator

    def set_muted(self, muted: bool) -> None:
        self._muted = muted

    def start(self) -> None:
        if self._runner is None:
            self._runner = asyncio.create_task(self._run(), name="announcer")

    async def stop(self) -> None:
        if self._runner:
            self._runner.cancel()
            try:
                await self._runner
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._runner = None

    @property
    def pending(self) -> list[Announcement]:
        return sorted(self._queue, key=lambda a: (-a.priority, a.seq))

    @property
    def current(self) -> Announcement | None:
        return self._current

    def submit(self, ann: Announcement) -> Announcement:
        ann.seq = next(self._seq)
        ann.t_submitted = time.monotonic()
        self._queue.append(ann)
        self._emit("announcement", id=ann.seq, source=ann.source, state="queued", mode=ann.mode, wait=ann.wait)
        logger.debug(f"announcement #{ann.seq} queued ({ann.source}, {ann.mode}/{ann.wait}, prio {ann.priority})")
        self._wake.set()
        self.start()
        return ann

    # convenience
    def say(self, text: str, source: str, **kw) -> Announcement:
        return self.submit(Announcement(text=text, source=source, mode="say", **kw))

    def tell(self, text: str, source: str, **kw) -> Announcement:
        return self.submit(Announcement(text=text, source=source, mode="tell", **kw))

    # ------------------------------------------------------------------ worker

    async def _run(self) -> None:
        while True:
            if not self._queue:
                self._wake.clear()
                await self._wake.wait()
                continue
            ann = self.pending[0]
            self._queue.remove(ann)
            try:
                await self._speak(ann)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.warning(f"announcement #{ann.seq} ({ann.source}) failed: {e!r}")
                self._finish(ann, f"dropped:error")

    def _finish(self, ann: Announcement, outcome: str) -> None:
        self._emit("announcement", id=ann.seq, source=ann.source, state=outcome)
        if ann.on_done:
            try:
                ann.on_done(ann, outcome)
            except Exception as e:  # noqa: BLE001
                logger.debug(f"announcement callback failed: {e!r}")

    async def _speak(self, ann: Announcement) -> None:
        if self._muted and ann.mode == "say":
            self._finish(ann, "dropped:muted")
            return
        waited = await self._wait_for(ann)
        if ann.expired:
            self._finish(ann, "dropped:expired")
            return
        if ann.still_wanted is not None and not ann.still_wanted():
            self._finish(ann, "dropped:unwanted")
            return
        if waited > 0.05:
            logger.info(f"announcement #{ann.seq} ({ann.source}): waited {waited:.1f} s for its moment")
        ann.t_spoken = time.time()
        self._current, self._phase = ann, "sent"
        self._outcome = asyncio.get_running_loop().create_future()
        await self._dispatch(ann)
        try:
            outcome = await asyncio.wait_for(self._outcome, SPOKEN_TIMEOUT_SECS)
        except asyncio.TimeoutError:
            outcome = "spoken"  # nothing was heard (e.g. the model answered <pass/>); do not block the queue
        finally:
            self._current, self._phase, self._outcome = None, "", None
        if outcome == "interrupted":
            retry = ann.retry_text if ann.mode == "tell" else (ann.retry_text or ann.text)
            if retry and ann.retries < 1:
                ann.retries += 1
                ann.text = retry
                logger.info(f"announcement #{ann.seq} ({ann.source}) was cut off; saying it again after his query")
                self._queue.append(ann)
                self._emit("announcement", id=ann.seq, source=ann.source, state="requeued")
                return
        self._finish(ann, outcome)

    async def _wait_for(self, ann: Announcement) -> float:
        t0 = time.monotonic()
        act = self._activity
        if act is None or ann.wait == "none":
            return 0.0
        if ann.wait == "speech":
            hold = ann.max_hold if ann.max_hold is not None else SPEECH_HOLD_SECS
            while time.monotonic() - t0 < hold and act.user_speaking:
                await asyncio.sleep(0.1)
            return time.monotonic() - t0
        hold = ann.max_hold if ann.max_hold is not None else MAX_HOLD_SECS
        while time.monotonic() - t0 < hold:
            if not act.turn_open and act.idle_for() >= QUIET_SECS:
                return time.monotonic() - t0
            if ann.expired:
                return time.monotonic() - t0
            await asyncio.sleep(0.1)
        logger.warning(f"conversation busy for {hold:.0f} s ({act.state()}); speaking announcement #{ann.seq} anyway")
        return time.monotonic() - t0

    async def _dispatch(self, ann: Announcement) -> None:
        if ann.mode == "tell":
            if self._assistant is None:
                raise RuntimeError("no assistant aggregator attached")
            # A user-role message: small models follow it more reliably than a system message mid-history.
            frame = LLMMessagesAppendFrame(messages=[{"role": "user", "content": ann.text}], run_llm=True)
            await self._assistant.queue_frame(frame)
        else:
            if self._worker is None:
                raise RuntimeError("no pipeline worker attached")
            frame = TTSSpeakFrame(text=ann.text, append_to_context=ann.append_to_context)
            frame.interruptible = ann.interruptible
            await self._worker.queue_frame(frame)
        self._emit("announcement", id=ann.seq, source=ann.source, state="speaking")

    # ------------------------------------------------------------- outcome

    def _on_frame(self, frame: Frame) -> None:
        """Watches the pipeline after a dispatch to learn how the announcement ended."""
        ann, fut = self._current, self._outcome
        if ann is None or fut is None or fut.done():
            return
        if isinstance(frame, BotStartedSpeakingFrame):
            self._phase = "speaking"
        elif isinstance(frame, BotStoppedSpeakingFrame):
            act = self._activity
            if self._phase == "speaking" and act is not None and not act.llm_active and not act.pending_calls:
                fut.set_result("spoken")
        elif isinstance(frame, InterruptionFrame):
            # The pipeline drops queued and playing speech on an interruption (unless the
            # TTSSpeakFrame is marked un-interruptible, which keeps playing).
            if ann.mode == "say" and not ann.interruptible:
                return
            if self._phase == "speaking" or ann.mode == "say":
                fut.set_result("interrupted")
            else:
                # A "tell" not spoken yet: its message stays in the context and is answered
                # together with his query once his turn ends, so there is nothing to repeat.
                fut.set_result("spoken")
