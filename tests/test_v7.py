"""v7 (wild) switches: duck-and-decide barge-in, adaptive end of turn, hedged LLM requests, narration filter."""

from __future__ import annotations

import asyncio
import time

import pytest

import assistant.services.mcp_stt as stt_mod
from assistant.services.mcp_stt import backchannel_only, dangling, is_statement


@pytest.fixture
def adaptive():
    old = stt_mod.ADAPTIVE
    stt_mod.ADAPTIVE = True
    yield
    stt_mod.ADAPTIVE = old


@pytest.mark.parametrize(
    "text, expected",
    [
        # v6 fdbench held this 1.5 s on "about"
        ("There's nothing else to do, but now it's everything people are talking about.", False),
        ("I'm redecorating my living room. I think I'll paint the walls first and then", True),
        ("But what can be really discouraging is that, like, when you", True),
        ("If you wanna not be in, like", True),
        ("I went there because", True),
        ("I don't think so.", False),
        ("Thank you.", False),
        ("I'm not sure what to tell you.", False),
        ("Turn on the", True),  # v3 rule unchanged
        ("Remind me to", True),
        ("Turn on the kitchen light.", False),
        ("Set a timer for five minutes", False),
        ("Call Dad", False),
    ],
)
def test_dangling_adaptive(adaptive, text, expected):
    assert dangling(text) is expected


def test_dangling_v6_rules_without_adaptive():
    assert stt_mod.ADAPTIVE is False
    assert dangling("everything people are talking about.") is True  # the v6 behaviour, kept when the switch is off
    assert dangling("I'll paint the walls first and then") is False


@pytest.mark.parametrize(
    "text, prev, expected",
    [
        ("Regular house.", "", True),
        ("Mhmm. But... yeah.", "", True),
        ("I want to learn a new language.", "", True),
        ("What's the weather in Berlin?", "", False),
        ("Turn on the kitchen light.", "", False),
        ("Set a reminder.", "", False),
        ("Can we talk about something else?", "", False),
        ("Ten minutes.", "How long should the timer be?", False),  # an answer to her question
        ("Sam.", "Who would you like me to call?", False),
        ("Okay.", "", False),  # backchannel
        ("Goodbye.", "", False),
        ("Never mind.", "", False),
        ("Thanks.", "", False),
        ("Wait, I meant Tokyo.", "", False),
        ("Shorter please.", "", False),
        ("Now turn it off.", "", False),
        ("If it will rain tomorrow in London, remind me at eight.", "", False),
        ("At five in the afternoon.", "", False),
    ],
)
def test_is_statement(text, prev, expected):
    assert is_statement(text, prev) is expected


@pytest.mark.parametrize("text", ["Kay.", "Yes.", "Mhmm.", "Mm-hm.", "Okay.", "Uh-huh, right."])
def test_more_backchannels(text):
    assert backchannel_only(text)


def test_meta_filter_cuts_narration():
    from pipecat.frames.frames import LLMFullResponseEndFrame, LLMFullResponseStartFrame, LLMTextFrame
    from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

    import assistant.processors as proc

    out = []
    pf = proc.PassFilter()

    async def push(frame, direction=FrameDirection.DOWNSTREAM):
        out.append(frame)

    pf.push_frame = push

    async def run(chunks):
        out.clear()
        await pf.process_frame(LLMFullResponseStartFrame(), FrameDirection.DOWNSTREAM)
        for c in chunks:
            await pf.process_frame(LLMTextFrame(c), FrameDirection.DOWNSTREAM)
        await pf.process_frame(LLMFullResponseEndFrame(), FrameDirection.DOWNSTREAM)
        return "".join(f.text for f in out if isinstance(f, LLMTextFrame))

    leak = ["Twelve. We are done. (no further)Done. (no more)Okay. Stop. Done. We should stop. ",
            "The answer already given. We must output only once. Finished. Okay."]

    async def main():
        orig = FrameProcessor.process_frame

        async def noop(self, frame, direction):
            return None

        FrameProcessor.process_frame = noop
        old = proc.META
        try:
            proc.META = True
            assert (await run(leak)).strip() == "Twelve."
            # v7 full run s01: no space after the full stop, and a tag at the end
            assert (await run(["Twelve.We are done.<pass/>"])).strip() == "Twelve."
            assert (await run(["Tokyo.", "We're done. (no further)"])).strip() == "Tokyo."
            assert (await run(["I'll stop the music. ", "Done."])).strip() == "I'll stop the music. Done."
            assert (await run(["Alarm set for 7 AM. ", "I can answer more if you like."])).strip().endswith("like.")
            proc.META = False
            assert "(no further)" in await run(leak)  # v6 behaviour with the switch off
        finally:
            FrameProcessor.process_frame = orig
            proc.META = old

    asyncio.run(main())


def test_cut_check_adaptive_prompt():
    from assistant.eot_check import SYSTEM, SYSTEM_V7, CutOffCheck

    assert CutOffCheck(None, model="m")._system == SYSTEM
    assert CutOffCheck(None, model="m", adaptive=True)._system == SYSTEM_V7
    assert "trails off" in SYSTEM_V7


# --------------------------------------------------------------------------- duck


class _Frame:
    def __init__(self, ms=40, rate=16000):
        self.audio = b"\0\0" * (rate * ms // 1000)
        self.num_channels = 1
        self.sample_rate = rate


class _Out:
    """Stands in for a Pipecat output transport: records when each frame was written."""

    def __init__(self):
        self.written = []

    async def write_audio_frame(self, frame):
        self.written.append(time.perf_counter())
        return True

    async def process_frame(self, frame, direction):
        return None


def test_duck_pauses_resumes_and_paces():
    from assistant.duck import Duck

    async def main():
        out = _Out()
        duck = Duck(pace=True, max_secs=2.5)
        duck.install(out)
        t0 = time.perf_counter()
        for _ in range(5):
            await out.write_audio_frame(_Frame())
        # 5 x 40 ms at sound-card pace, ~40 ms ahead
        assert time.perf_counter() - t0 >= 0.12
        duck.pause("test")
        task = asyncio.create_task(out.write_audio_frame(_Frame()))
        await asyncio.sleep(0.15)
        assert len(out.written) == 5 and not task.done()  # paused: the frame waits
        duck.resume("backchannel")
        await task
        assert len(out.written) == 6 and duck.stats["resumed"] == 1

    asyncio.run(main())


def test_duck_drops_the_waiting_frame_on_interruption():
    from pipecat.frames.frames import InterruptionFrame

    from assistant.duck import Duck

    async def main():
        out = _Out()
        duck = Duck(pace=False)
        duck.install(out)
        duck.pause("test")
        task = asyncio.create_task(out.write_audio_frame(_Frame()))
        await asyncio.sleep(0.05)
        await out.process_frame(InterruptionFrame(), None)
        assert await task is True
        assert out.written == [] and duck.stats["dropped_frames"] == 1 and duck.stats["interrupted"] == 1
        await out.write_audio_frame(_Frame())  # the next reply plays
        assert len(out.written) == 1

    asyncio.run(main())


def test_duck_watchdog_resumes():
    from assistant.duck import Duck

    async def main():
        duck = Duck(pace=False, max_secs=-0.9)  # watchdog after 0.1 s
        duck.pause("test")
        await asyncio.sleep(0.25)
        assert not duck.paused and duck.stats["watchdog"] == 1

    asyncio.run(main())


# --------------------------------------------------------------------------- hedge


class _Delta:
    def __init__(self, content=None):
        self.content = content
        self.tool_calls = None


class _Choice:
    def __init__(self, content):
        self.delta = _Delta(content)
        self.finish_reason = None


class _Chunk:
    def __init__(self, content):
        self.choices = [_Choice(content)]


class _Stream:
    def __init__(self, delay, text):
        self.delay, self.text, self.closed = delay, text, False

    def __aiter__(self):
        return self._it()

    async def _it(self):
        await asyncio.sleep(self.delay)
        yield _Chunk(self.text)

    async def close(self):
        self.closed = True


def _hedge_llm(delays, hedge=0.1):
    from assistant.services.timed_llm import TimedGroqLLMService

    llm = TimedGroqLLMService.__new__(TimedGroqLLMService)
    llm._name = "TimedGroqLLMService#test"
    llm._hedge_secs = hedge
    llm._swap_once = False
    llm.hedges = {"fired": 0, "won_by_hedge": 0, "won_by_first": 0, "both_failed": 0}
    streams = []

    async def fake_base(self, context):
        swapped = self._swap_once
        self._swap_once = False
        s = _Stream(delays[len(streams)], "B" if swapped else "A")
        streams.append(s)
        return s

    return llm, streams, fake_base


def test_hedge_second_request_wins_when_first_stalls(monkeypatch):
    from pipecat.services.groq.llm import GroqLLMService

    llm, streams, fake = _hedge_llm([1.0, 0.05])
    monkeypatch.setattr(GroqLLMService, "get_chat_completions", fake)

    async def main():
        call = {}
        s = await llm._hedged(None, call)
        text = [c.choices[0].delta.content async for c in s]
        assert text == ["B"] and call["hedge_won_by"] == "hedge" and llm.hedges["fired"] == 1

    asyncio.run(main())


def test_hedge_not_fired_when_first_is_fast(monkeypatch):
    from pipecat.services.groq.llm import GroqLLMService

    llm, streams, fake = _hedge_llm([0.01, 0.01])
    monkeypatch.setattr(GroqLLMService, "get_chat_completions", fake)

    async def main():
        s = await llm._hedged(None, {})
        assert [c.choices[0].delta.content async for c in s] == ["A"]
        assert llm.hedges["fired"] == 0 and len(streams) == 1

    asyncio.run(main())


def test_tts_phrase_cache(monkeypatch):
    import json

    import mcp_servers.tts.server as srv

    monkeypatch.setattr(srv, "PHRASE_CACHE", True)
    srv.CACHE.clear()
    spoken = []

    async def fake_speak(job):
        spoken.append(job.text)
        srv.AURA.in_flight.clear()
        job.queue.put_nowait(b"\1\0" * 8000)
        if job.pcm is not None:
            job.pcm.extend(b"\1\0" * 8000)
        job.bytes_out = 16000
        job.finish()

    monkeypatch.setattr(srv.AURA, "speak", fake_speak)

    async def main():
        a = await srv.tts_start("Kitchen light on.")
        assert "cached" not in a and spoken == ["Kitchen light on."]
        b = await srv.tts_start("kitchen  light on.")  # same words, case and spacing ignored
        assert b.get("cached") is True and spoken == ["Kitchen light on."]
        got = b""
        while True:
            out = await srv.tts_read(b["stream"], 10)
            if out[0].type == "text" and json.loads(out[0].text).get("eof"):
                break
            got += __import__("base64").b64decode(out[0].data)
        assert got == b"\1\0" * 8000
        long = "x" * (srv.PHRASE_MAX_CHARS + 1)
        await srv.tts_start(long)
        await srv.tts_start(long)
        assert spoken.count(long) == 2  # long sentences are never cached

    asyncio.run(main())
