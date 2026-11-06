"""v11 (TOOL_COMMENT): a short LLM tail after a fixed tool reply, and the safeguards that drop it."""

import asyncio
import json
from types import SimpleNamespace

from pipecat.frames.frames import InterruptionFrame, LLMFullResponseEndFrame, LLMFullResponseStartFrame, LLMTextFrame
from pipecat.processors.frame_processor import FrameDirection

import assistant.fast_confirm as fc
import assistant.tool_comment as tc
from assistant.processors import PassFilter


class FakeClient:
    """An openai.AsyncOpenAI stand-in: answers `reply` after `delay` seconds and records the request."""

    def __init__(self, reply: str, delay: float = 0.05, error: Exception | None = None):
        self.reply, self.delay, self.error, self.requests = reply, delay, error, []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kw):
        self.requests.append(kw)
        await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=self.reply))])


class FakeLLM:
    def __init__(self):
        self.frames = []

    async def push_frame(self, frame, direction=FrameDirection.DOWNSTREAM):
        self.frames.append(frame)

    def text(self) -> list[str]:
        return [f.text for f in self.frames if isinstance(f, LLMTextFrame)]


def tail(reply: str, delay: float = 0.05, error=None) -> tc.Tail:
    t = tc.Tail()
    t.client, t.model = FakeClient(reply, delay, error), "openai/gpt-oss-120b"
    return t


async def run(t: tc.Tail, said: str, result: str = "{}", user: str = "Remind me to call the dentist at 3 PM.") -> FakeLLM:
    llm = FakeLLM()
    t.start(llm, said, result, user)
    if t.task:
        try:
            await t.task
        except asyncio.CancelledError:
            pass
    return llm


SAID = "I'll remind you to call the dentist at 3 PM tomorrow."


async def test_tail_is_spoken_after_the_fixed_reply():
    t = tail("Good luck with that")
    llm = await run(t, SAID)
    assert llm.text() == ["Good luck with that."]
    assert isinstance(llm.frames[0], LLMFullResponseStartFrame) and isinstance(llm.frames[-1], LLMFullResponseEndFrame)
    req = t.client.requests[0]
    assert "tools" not in req  # safeguard 5: the tail request cannot call a tool
    assert SAID in req["messages"][1]["content"]


async def test_pass_is_silent():
    t = tail("<pass/>")
    llm = await run(t, SAID)
    assert llm.frames == [] and t.stats["passed"] == 1


async def test_bad_tails_are_dropped():
    for reply, why in [
        ("Want an earlier one too?", "a question"),
        ("That is a lot of words for a tail that should be very short.", "words"),
        ("Good luck. Floss first.", "more than one sentence"),
        ("Reminder set to call the dentist tomorrow.", "repeats"),
        ("**Good luck**", "markup"),
    ]:
        t = tail(reply)
        llm = await run(t, SAID)
        assert llm.frames == [], reply
        assert why in t.events[-1]["why"], (reply, t.events[-1])


async def test_late_tail_is_dropped():
    t = tail("Good luck with that", delay=5.0)  # the fixed reply plays for about 4.6 s at speed 1.0
    llm = await run(t, SAID)
    assert llm.frames == [] and "not ready" in t.events[-1]["why"]


async def test_slow_speech_gives_more_time():
    assert tc.speak_secs(SAID, 0.7) > tc.speak_secs(SAID, 1.0) > tc.speak_secs(SAID, 1.3)


async def test_error_is_silent():
    t = tail("", error=RuntimeError("429"))
    llm = await run(t, SAID)
    assert llm.frames == [] and "error" in t.events[-1]["why"]


async def test_interruption_cancels_the_tail(monkeypatch):
    from pipecat.processors.frame_processor import FrameProcessor

    async def base(self, frame, direction):
        return None

    monkeypatch.setattr(FrameProcessor, "process_frame", base)
    t = tail("Good luck with that", delay=0.5)
    old, tc.TAIL = tc.TAIL, t
    try:
        llm = FakeLLM()
        t.start(llm, SAID, "{}", "Remind me")
        pf = PassFilter()

        async def push(*a, **k):
            return None

        pf.push_frame = push
        await pf.process_frame(InterruptionFrame(), FrameDirection.DOWNSTREAM)
        await asyncio.sleep(0.6)
        assert llm.frames == [] and t.events[-1]["why"] == "the user spoke"
    finally:
        tc.TAIL = old


async def test_fast_confirm_starts_a_tail_only_for_the_listed_tools(monkeypatch):
    started = []

    class Spy:
        def start(self, llm, said, result, user):
            started.append((said, user))

    monkeypatch.setattr(tc, "ENABLED", True)
    monkeypatch.setattr(tc, "TOOLS", {"create_reminder"})
    monkeypatch.setattr(tc, "TAIL", Spy())

    class Client:
        async def _call_tool_text(self, name, args):
            if name == "create_reminder":
                return json.dumps({"text": "call the dentist", "due_at": "2026-11-07T15:00:00"})
            return json.dumps({"lights": [{"room": "kitchen"}], "on": True})

    tools = [SimpleNamespace(name=n, description="", properties={}, required=[], handler=lambda p: None)
             for n in ("create_reminder", "set_light")]
    wrapped = {s.name: s.handler for s in fc.wrap_tools(tools, Client())}
    for name, args in (("create_reminder", {"text": "call the dentist"}), ("set_light", {"room": "kitchen", "on": True})):
        llm = FakeLLM()
        results = []

        async def cb(result, *a, **kw):
            results.append((result, kw.get("properties")))

        ctx = SimpleNamespace(get_messages=lambda: [{"role": "user", "content": f"please {name}"}])
        await wrapped[name](SimpleNamespace(arguments=args, result_callback=cb, llm=llm, context=ctx))
        assert results[0][1].run_llm is False  # the fixed reply path is unchanged: no second pipeline LLM call
        assert len(llm.text()) == 1
    assert started == [("I'll remind you to call the dentist at 3 PM tomorrow.", "please create_reminder")]
