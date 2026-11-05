"""v5: ASR post-fix, engagement check, re-prompt filter, reminder capitals, invented-dialogue filter."""

import asyncio

from pipecat.frames.frames import LLMFullResponseEndFrame, LLMFullResponseStartFrame, LLMTextFrame
from pipecat.processors.frame_processor import FrameDirection

from assistant.engagement import addressed_elsewhere
from assistant.fast_confirm import _reminder
from assistant.processors import PassFilter
from assistant.reprompt import slot_question
from assistant.services.mcp_stt import asr_fix, dangling


def test_asr_fix():
    assert asr_fix("Cold ad.") == "Call Dad."
    assert asr_fix("Coal mom.") == "Call Mom."
    assert asr_fix("Is it cold outside?") == "Is it cold outside?"
    assert dangling(asr_fix("Remind me too!"))


def test_engagement():
    known = ("Mom", "Dad", "Alex", "Sam")
    assert addressed_elsewhere("Kayla, you're talking to the radio.", "Pi", known)
    assert addressed_elsewhere("Stay tuned for the weather after the break.", "Pi", known)
    for t in ("Okay, set a timer.", "Sam.", "Pi, what time is it?", "Can we talk about something else?"):
        assert addressed_elsewhere(t, "Pi", known) is None


def test_slot_question():
    assert slot_question("When should I remind you?")
    assert not slot_question("Would you like the forecast for tomorrow as well?")
    assert not slot_question("Do you want me to delete both reminders?")


def test_reminder_capitals():
    r = {"text": "Take My Medicine", "due_at": "2026-11-04T18:00:00"}
    assert "take my medicine" in _reminder({}, r)
    assert "call Mom" in _reminder({}, {"text": "Call Mom", "due_at": "2026-11-04T18:00:00"})


def _spoken(text: str) -> str:
    out: list[str] = []
    f = PassFilter()

    async def push(frame, direction=FrameDirection.DOWNSTREAM):
        if isinstance(frame, LLMTextFrame):
            out.append(frame.text)

    f.push_frame = push

    async def run():
        f._sentence, f._said = "", set()
        f._sentences, f._asked, f._cut = 0, False, False
        await f._emit(text + " ", FrameDirection.DOWNSTREAM)
        await f._emit_sentence(f._sentence, FrameDirection.DOWNSTREAM)

    asyncio.run(run())
    return "".join(out).strip()


def test_invented_dialogue_cut():
    s = _spoken("Which alarm should I delete? Confirm delete alarm wake up at seven AM? Yes. Alarm deleted.")
    assert s == "Which alarm should I delete?"
    assert _spoken("Twelve. We are done.") == "Twelve."
    assert _spoken("Tokyo. It is the capital.") == "Tokyo. It is the capital."


def test_glued_second_question_cut():
    assert _spoken("What time should the alarm be set for?When would you like the alarm?") == (
        "What time should the alarm be set for?"
    )
