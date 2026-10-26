"""The announcement queue: results, reminders and alarms wait for the right moment.

Frames are replayed by hand through the ActivityTracker (no pipeline), with the timing of a
real conversation compressed to tens of milliseconds.
"""

from __future__ import annotations

import asyncio

import pytest
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    FunctionCallFromLLM,
    FunctionCallResultFrame,
    FunctionCallsStartedFrame,
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMMessagesAppendFrame,
    TTSSpeakFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection

from assistant import announcer as mod
from assistant.announcer import PRIORITY_ALARM, PRIORITY_REMINDER, ActivityTracker, Announcement, Announcer


class Sink:
    def __init__(self):
        self.frames = []

    async def queue_frame(self, frame, direction=None):
        self.frames.append(frame)


@pytest.fixture
def fast(monkeypatch):
    monkeypatch.setattr(mod, "QUIET_SECS", 0.1)
    monkeypatch.setattr(mod, "MAX_HOLD_SECS", 2.0)
    monkeypatch.setattr(mod, "SPEECH_HOLD_SECS", 0.5)
    monkeypatch.setattr(mod, "SPOKEN_TIMEOUT_SECS", 1.0)
    monkeypatch.setattr(ActivityTracker, "TURN_IDLE", 0.3)
    import pipecat.processors.frame_processor as fp

    async def base_noop(self, frame, direction):
        return None

    monkeypatch.setattr(fp.FrameProcessor, "process_frame", base_noop)


class Rig:
    """A tracker, an announcer and the two sinks it speaks into."""

    def __init__(self):
        self.tracker = ActivityTracker()
        self.pushed = []

        async def push(frame, direction=FrameDirection.DOWNSTREAM):
            self.pushed.append(frame)

        self.tracker.push_frame = push
        self.events = []
        self.announcer = Announcer(self.tracker, on_event=lambda kind, **f: self.events.append(f))
        self.worker, self.assistant = Sink(), Sink()
        self.announcer.attach(self.worker, self.assistant)
        self.outcomes = []

    async def frame(self, f, direction=FrameDirection.DOWNSTREAM):
        await self.tracker.process_frame(f, direction)

    async def user_turn(self, *, tool: bool = False, reply_secs: float = 0.05):
        """He asks something; she answers (optionally through a tool call) and finishes speaking."""
        await self.frame(UserStartedSpeakingFrame())
        await asyncio.sleep(0.05)
        await self.frame(UserStoppedSpeakingFrame())
        await asyncio.sleep(0.05)  # LLM time to first token
        await self.frame(LLMFullResponseStartFrame())
        if tool:
            call = FunctionCallFromLLM(function_name="create_alarm", tool_call_id="c1", arguments={}, context=None)
            await self.frame(FunctionCallsStartedFrame(function_calls=[call]))
            await self.frame(LLMFullResponseEndFrame())
            await asyncio.sleep(0.1)  # the tool runs
            await self.frame(FunctionCallResultFrame(function_name="create_alarm", tool_call_id="c1", arguments={}, result={}))
            await self.frame(LLMFullResponseStartFrame())
        await self.frame(LLMFullResponseEndFrame())
        await asyncio.sleep(0.05)  # TTS start-up
        await self.frame(BotStartedSpeakingFrame(), FrameDirection.UPSTREAM)
        await asyncio.sleep(reply_secs)
        await self.frame(BotStoppedSpeakingFrame(), FrameDirection.UPSTREAM)

    async def play_announcement(self, secs: float = 0.05):
        """The announcement's own speech, as the pipeline would report it."""
        await self.frame(BotStartedSpeakingFrame(), FrameDirection.UPSTREAM)
        await asyncio.sleep(secs)
        await self.frame(BotStoppedSpeakingFrame(), FrameDirection.UPSTREAM)

    def tell(self, text="Background task finished: rain tomorrow.", **kw):
        return self.announcer.tell(text, "hermes", on_done=lambda a, o: self.outcomes.append((a.source, o)), **kw)

    def say(self, text, source="reminder", **kw):
        return self.announcer.say(text, source, on_done=lambda a, o: self.outcomes.append((a.source, o)), **kw)

    async def close(self):
        await self.announcer.stop()


async def settle(secs: float = 0.25):
    await asyncio.sleep(secs)


@pytest.mark.asyncio
async def test_result_waits_for_his_open_turn_even_before_he_finishes(fast):
    """He starts asking while the result arrives: his query is answered first, then the result."""
    rig = Rig()
    await rig.frame(UserStartedSpeakingFrame())  # mid-sentence
    rig.tell()
    await settle()
    assert rig.assistant.frames == []
    # he finishes, she answers through a tool call, and only then does the result go out
    await rig.frame(UserStoppedSpeakingFrame())
    await asyncio.sleep(0.05)
    await rig.frame(LLMFullResponseStartFrame())
    call = FunctionCallFromLLM(function_name="create_alarm", tool_call_id="c1", arguments={}, context=None)
    await rig.frame(FunctionCallsStartedFrame(function_calls=[call]))
    await rig.frame(LLMFullResponseEndFrame())
    await settle()
    assert rig.assistant.frames == []  # tool call in flight
    await rig.frame(FunctionCallResultFrame(function_name="create_alarm", tool_call_id="c1", arguments={}, result={}))
    await rig.frame(LLMFullResponseStartFrame())
    await rig.frame(LLMFullResponseEndFrame())
    await settle()
    assert rig.assistant.frames == []  # her confirmation has not played yet
    await rig.frame(BotStartedSpeakingFrame(), FrameDirection.UPSTREAM)
    await rig.frame(BotStoppedSpeakingFrame(), FrameDirection.UPSTREAM)
    await settle()
    assert len(rig.assistant.frames) == 1 and isinstance(rig.assistant.frames[0], LLMMessagesAppendFrame)
    await rig.play_announcement()
    await settle(0.05)
    assert rig.outcomes == [("hermes", "spoken")]
    await rig.close()


@pytest.mark.asyncio
async def test_result_during_a_quiet_moment_goes_out_at_once(fast):
    rig = Rig()
    await rig.user_turn()
    await settle(0.15)
    rig.tell()
    await settle(0.1)
    assert len(rig.assistant.frames) == 1
    await rig.close()


@pytest.mark.asyncio
async def test_turn_without_a_reply_closes_on_its_own(fast):
    """She answered <pass/> (nothing spoken): the turn must not hold announcements forever."""
    rig = Rig()
    await rig.frame(UserStartedSpeakingFrame())
    await rig.frame(UserStoppedSpeakingFrame())
    await rig.frame(LLMFullResponseStartFrame())
    await rig.frame(LLMFullResponseEndFrame())
    rig.tell()
    await settle(0.15)
    assert rig.assistant.frames == []
    await settle(0.4)  # TURN_IDLE
    assert len(rig.assistant.frames) == 1
    await rig.close()


@pytest.mark.asyncio
async def test_interrupted_result_is_repeated_once_after_his_query(fast):
    rig = Rig()
    rig.tell(retry_text="Background task reminder: give the result again.")
    await settle(0.15)
    assert len(rig.assistant.frames) == 1
    await rig.frame(BotStartedSpeakingFrame(), FrameDirection.UPSTREAM)
    await rig.frame(InterruptionFrame())  # he barges in while she reads the result
    await rig.user_turn()
    await settle(0.2)
    assert len(rig.assistant.frames) == 2
    assert rig.assistant.frames[1].messages[0]["content"].startswith("Background task reminder")
    assert "requeued" in [e.get("state") for e in rig.events]
    await rig.frame(BotStartedSpeakingFrame(), FrameDirection.UPSTREAM)
    await rig.frame(InterruptionFrame())  # a second interruption is not retried again
    await rig.user_turn()
    await settle(0.2)
    assert len(rig.assistant.frames) == 2 and rig.outcomes == [("hermes", "interrupted")]
    await rig.close()


@pytest.mark.asyncio
async def test_tell_interrupted_before_it_was_spoken_is_not_repeated(fast):
    """The appended message is still in the context and is answered with his query."""
    rig = Rig()
    rig.tell(retry_text="again")
    await settle(0.15)
    assert len(rig.assistant.frames) == 1
    await rig.frame(InterruptionFrame())  # before any bot speech
    await rig.user_turn()
    await settle(0.2)
    assert len(rig.assistant.frames) == 1 and rig.outcomes == [("hermes", "spoken")]
    await rig.close()


@pytest.mark.asyncio
async def test_two_announcements_go_one_at_a_time_by_priority(fast):
    rig = Rig()
    await rig.frame(UserStartedSpeakingFrame())
    rig.tell()
    rig.say("Reminder: water the plants.", priority=PRIORITY_REMINDER)
    await rig.frame(UserStoppedSpeakingFrame())
    await settle(0.4)
    # the reminder (higher priority) first, and the result only after it has been spoken
    assert len(rig.worker.frames) == 1 and isinstance(rig.worker.frames[0], TTSSpeakFrame)
    assert rig.assistant.frames == []
    await rig.play_announcement()
    await settle(0.2)
    assert len(rig.assistant.frames) == 1
    await rig.close()


@pytest.mark.asyncio
async def test_alarm_waits_for_his_sentence_but_not_for_her_reply(fast):
    rig = Rig()
    await rig.frame(UserStartedSpeakingFrame())
    rig.say("Alarm. It's seven.", source="alarm", wait="speech", priority=PRIORITY_ALARM, interruptible=False)
    await settle(0.1)
    assert rig.worker.frames == []
    await rig.frame(UserStoppedSpeakingFrame())
    await rig.frame(LLMFullResponseStartFrame())  # she is answering; the alarm does not wait for that
    await settle(0.15)
    assert len(rig.worker.frames) == 1 and rig.worker.frames[0].interruptible is False
    await rig.close()


@pytest.mark.asyncio
async def test_nudge_is_dropped_once_unwanted_and_ttl_expires(fast):
    rig = Rig()
    wanted = {"v": True}
    await rig.frame(UserStartedSpeakingFrame())
    rig.say("still working", source="nudge", still_wanted=lambda: wanted["v"])
    rig.say("old news", source="x", ttl=0.05)
    wanted["v"] = False
    await rig.frame(UserStoppedSpeakingFrame())
    await settle(0.5)
    assert rig.worker.frames == []
    assert sorted(rig.outcomes) == [("nudge", "dropped:unwanted"), ("x", "dropped:expired")]
    await rig.close()


@pytest.mark.asyncio
async def test_wait_none_speaks_immediately_and_muted_drops_speech(fast):
    rig = Rig()
    await rig.frame(UserStartedSpeakingFrame())
    rig.say("One moment.", source="llm_wait", wait="none", append_to_context=False)
    await settle(0.05)
    assert len(rig.worker.frames) == 1 and rig.worker.frames[0].append_to_context is False
    await rig.play_announcement()
    rig.announcer.set_muted(True)
    rig.say("Reminder: x", wait="none")
    await settle(0.05)
    assert len(rig.worker.frames) == 1 and rig.outcomes[-1] == ("reminder", "dropped:muted")
    await rig.close()


@pytest.mark.asyncio
async def test_busy_forever_still_speaks_after_max_hold(fast, monkeypatch):
    monkeypatch.setattr(mod, "MAX_HOLD_SECS", 0.2)
    rig = Rig()
    rig.tracker.llm_active = True
    rig.tracker.LLM_STALE = 100
    rig.tell()
    await settle(0.4)
    assert len(rig.assistant.frames) == 1
    await rig.close()
