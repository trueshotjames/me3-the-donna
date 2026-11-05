"""v8 (consolidation) switches: VAD hook filter, weather direct, speaking rate, tool-slow notice, side-talk rule."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

WEATHER = json.dumps({"city": "Berlin", "spoken": "Berlin now: mainly clear, 27 degrees, feels like 26 degrees."})


@pytest.mark.parametrize(
    "said, direct",
    [
        ("What's the weather in Berlin?", True),
        ("What's the weather in Tokyo tomorrow", True),
        ("Weather in Manila", True),
        ("What is the forecast for Paris?", True),
        ("Is it cold outside?", False),  # no weather word: the model judges
        ("How warm is it outside?", False),
        ("Is the weather nice today?", False),  # yes/no question
        ("What's the weather, do I need an umbrella?", False),
        ("Should I bring a jacket, what's the weather like?", False),
        ("Is Berlin warmer than Paris?", False),
    ],
)
def test_weather_direct_only_for_plain_questions(said, direct):
    from assistant.fast_confirm import weather_sentence

    got = weather_sentence(said, WEATHER)
    assert (got is not None) == direct
    if direct:
        assert got.startswith("Berlin now")


def test_weather_direct_never_on_error_or_missing_sentence():
    from assistant.fast_confirm import weather_sentence

    assert weather_sentence("What's the weather in Narnia?", json.dumps({"error": "no place called 'Narnia'"})) is None
    assert weather_sentence("What's the weather?", json.dumps({"city": "Berlin"})) is None
    assert weather_sentence("What's the weather?", "not json") is None


def _schema(name, handler):
    from pipecat.adapters.schemas.function_schema import FunctionSchema

    return FunctionSchema(name, "test tool", {}, [], handler=handler)


async def test_weather_direct_switch_wraps_get_weather(monkeypatch):
    import assistant.fast_confirm as fc

    monkeypatch.setattr(fc, "WEATHER_DIRECT", False)
    plain = fc.wrap_tools([_schema("get_weather", lambda p: None)], client=None)
    monkeypatch.setattr(fc, "WEATHER_DIRECT", True)
    direct = fc.wrap_tools([_schema("get_weather", lambda p: None)], client=None)
    # with the switch the handler goes through make() (client._call_tool_text), without it the MCP handler is kept
    assert plain[0].handler is not None and direct[0].handler is not None

    calls = []

    class Client:
        async def _call_tool_text(self, name, args):
            calls.append(name)
            return WEATHER

    pushed, results = [], []

    class LLM:
        async def push_frame(self, frame):
            pushed.append(frame)

    async def cb(result, *a, **kw):
        results.append((result, kw.get("properties")))

    params = SimpleNamespace(
        arguments={"city": "Berlin"}, result_callback=cb, llm=LLM(),
        context=SimpleNamespace(get_messages=lambda: [{"role": "user", "content": "What's the weather in Berlin?\n\n[now: x]"}]),
    )
    monkeypatch.setattr(fc.TURN_CALLS, "_calls", {})  # other tests leave get_weather(Berlin) in the duplicate guard
    monkeypatch.setattr(fc, "_last_user", lambda p: "What's the weather in Berlin?")  # test_v6 replaces it for good
    tools = fc.wrap_tools([_schema("get_weather", lambda p: None)], client=Client())
    await tools[0].handler(params)
    assert calls == ["get_weather"]
    assert results and results[0][1] is not None and results[0][1].run_llm is False
    texts = [getattr(f, "text", None) for f in pushed]
    assert "Berlin now: mainly clear, 27 degrees, feels like 26 degrees." in texts


async def test_tool_slow_notice(monkeypatch):
    import assistant.fast_confirm as fc

    said = []

    async def on_slow(name):
        said.append(name)

    monkeypatch.setattr(fc, "TOOL_SLOW_SECS", 0.05)
    monkeypatch.setattr(fc, "ON_SLOW", on_slow)

    async def slow_handler(params):
        await asyncio.sleep(0.15)
        await params.result_callback("{}")

    async def fast_handler(params):
        await params.result_callback("{}")

    async def cb(result, *a, **kw):
        pass

    tools = fc.wrap_tools([_schema("home_status", slow_handler), _schema("list_alarms", fast_handler)], client=None)
    await tools[0].handler(SimpleNamespace(arguments={}, result_callback=cb))
    await tools[1].handler(SimpleNamespace(arguments={}, result_callback=cb))
    await asyncio.sleep(0.1)
    assert said == ["home_status"]  # the fast tool never says "One moment."


def test_speaking_rate_steps():
    from assistant.speaking_rate import next_speed, sentence

    assert next_speed(1.0, "slower") == 0.85
    assert next_speed(0.85, "slower") == 0.7
    assert next_speed(0.7, "slower") == 0.7
    assert next_speed(1.0, "faster") == 1.15
    assert next_speed(1.3, "faster") == 1.3
    assert next_speed(0.7, "normal") == 1.0
    assert sentence(1.0, 0.85, "slower") == "Okay, I'll speak more slowly."
    assert sentence(0.7, 0.7, "slower") == "This is as slow as I can go."
    assert sentence(0.85, 1.0, "normal") == "Okay, back to my normal speed."


async def test_speaking_rate_tool_calls_tts_server():
    from assistant.speaking_rate import SpeakingRate

    sent = []

    class Bridge:
        async def call_json(self, tool, args, timeout=0):
            sent.append((tool, args))
            return {"speed": args["speed"], "changed": True}

    pushed, results = [], []

    class LLM:
        def register_function(self, name, fn):
            self.fn = fn

        async def push_frame(self, frame):
            pushed.append(frame)

    async def cb(result, *a, **kw):
        results.append(kw.get("properties"))

    llm = LLM()
    rate = SpeakingRate(SimpleNamespace(_bridge=Bridge()))
    schemas = rate.register(llm)
    assert schemas[0].name == "set_speaking_rate"
    await llm.fn(SimpleNamespace(arguments={"rate": "slower"}, result_callback=cb, llm=llm))
    assert sent == [("tts_set_speed", {"speed": 0.85})]
    assert rate.speed == 0.85
    assert results[0].run_llm is False
    assert any(getattr(f, "text", "") == "Okay, I'll speak more slowly." for f in pushed)


async def test_tts_server_speed(monkeypatch):
    import mcp_servers.tts.server as srv

    monkeypatch.setattr(srv, "SPEED", 1.0)
    assert "speed=" not in srv.AURA.url()
    k1 = srv._phrase_key("Kitchen light on.")
    reopened = []

    async def fake_reopen():
        reopened.append(srv.SPEED)

    monkeypatch.setattr(srv.AURA, "reopen", fake_reopen)
    out = await srv.tts_set_speed(0.8)
    assert out == {"speed": 0.8, "changed": True} and reopened == [0.8]
    assert srv.AURA.url().endswith("&speed=0.8")
    assert srv._phrase_key("Kitchen light on.") != k1  # cached audio at the old rate is not reused
    assert (await srv.tts_set_speed(0.8))["changed"] is False
    assert (await srv.tts_set_speed(5))["speed"] == 1.5  # clamped to Aura's range
    assert (await srv.tts_set_speed(1.0))["speed"] == 1.0
    assert "speed=" not in srv.AURA.url()


def test_prompt_rules():
    from assistant.prompts import system_prompt

    v7 = system_prompt("Pi", "King", 8)
    v8 = system_prompt("Pi", "King", 8, speaking_rate=True, side_talk=True)
    assert "you cannot change your voice speed" in v7 and "set_speaking_rate" not in v7
    assert "call set_speaking_rate" in v8 and "you cannot change your voice speed" not in v8
    assert "answer only the" in v8 and "answer only the" not in v7


class _Loop:
    def __init__(self):
        self.posted = 0

    def call_soon_threadsafe(self, *a):
        self.posted += 1


@pytest.mark.parametrize("filt, expected", [(False, 6), (True, 4)])
def test_vad_hook_filter(filt, expected):
    from assistant.services.mcp_stt import MCPSTTService

    loop = _Loop()
    fake = SimpleNamespace(_duck=object(), _loop=loop, _bot_speaking=True, _duck_run=0, vad_hook_filter=filt,
                           _duck_conf=0.6, _duck_min_vol=0.45, _duck_frame=None)
    frames = [(0.1, 0.2), (0.2, 0.1), (0.9, 0.9), (0.1, 0.1), (0.95, 0.8), (0.3, 0.9)]
    for conf, vol in frames:
        MCPSTTService.vad_frame(fake, 0.0, conf, vol)
        # the event loop side counts the run; mimic it so the filter sees a run in progress
        fake._duck_run = fake._duck_run + 1 if (conf >= 0.6 and vol >= 0.45) else 0
    # filtered: the two voiced frames, plus the unvoiced frame right after each (it resets the run)
    assert loop.posted == expected


def test_tool_slow_filler_is_never_repeated():
    from assistant.announcer import NO_RETRY

    assert "tool_slow" in NO_RETRY  # a cut-off "One moment." is stale after the user's next turn (s13 smoke)


def test_v8_fixes_music_play_runs_once(monkeypatch):
    import assistant.fast_confirm as fc

    tc = fc._TurnCalls()
    monkeypatch.setattr(tc, "_sync", lambda: None)
    play = {"action": "play", "query": "jazz"}
    tc.done("control_music", play, "Playing jazz")
    monkeypatch.setattr(fc, "V8_FIXES", False)
    assert tc.earlier("control_music", play) is None  # v7: every music call is repeatable
    monkeypatch.setattr(fc, "V8_FIXES", True)
    assert tc.earlier("control_music", play) == "Playing jazz"  # the same play again: not run twice
    tc.done("control_music", {"action": "next"}, "Next track")
    assert tc.earlier("control_music", {"action": "next"}) is None  # "next, next" is still real


async def test_v8_fixes_spoken_duplicate_is_silent(monkeypatch):
    import assistant.fast_confirm as fc

    monkeypatch.setattr(fc.TURN_CALLS, "_calls", {})
    monkeypatch.setattr(fc.TURN_CALLS, "_spoken", set())
    monkeypatch.setattr(fc.TURN_CALLS, "_sync", lambda: None)
    monkeypatch.setattr(fc, "WEATHER_DIRECT", True)
    monkeypatch.setattr(fc, "_last_user", lambda p: "What's the weather in Paris?")

    class Client:
        async def _call_tool_text(self, name, args):
            return WEATHER

    pushed, results = [], []

    class LLM:
        async def push_frame(self, frame):
            pushed.append(frame)

    async def cb(result, *a, **kw):
        results.append(kw.get("properties"))

    tools = fc.wrap_tools([_schema("get_weather", lambda p: None)], client=Client())
    for fixes, n_text in ((True, 1), (False, 1)):
        monkeypatch.setattr(fc, "V8_FIXES", fixes)
        fc.TURN_CALLS._calls, fc.TURN_CALLS._spoken = {}, set()
        pushed.clear(); results.clear()
        for _ in range(2):  # the same call twice in one turn (v8 s13 run)
            await tools[0].handler(SimpleNamespace(arguments={"city": "Paris"}, result_callback=cb, llm=LLM()))
        assert sum(1 for f in pushed if getattr(f, "text", None)) == n_text
        second = results[1]
        if fixes:
            assert second is not None and second.run_llm is False  # silent: no second reading
        else:
            assert second is None  # v7: the model is asked to answer again


# ---- v8 follow-ups (King, 5 Nov): played text only, Donna, fixed thermostat ----

def test_played_tap_reports_only_downstream_text_and_marks_observers():
    import asyncio
    from pipecat.frames.frames import TTSTextFrame
    from pipecat.processors.frame_processor import FrameDirection
    from assistant.processors import PlayedTextTap

    tap = PlayedTextTap()
    got = []
    tap.listeners.append(lambda f: got.append(f.text))

    async def push(frame, direction=FrameDirection.DOWNSTREAM):
        pass

    tap.push_frame = push

    async def go():
        from pipecat.processors.frame_processor import FrameProcessor

        async def noop(self, frame, direction):
            return None

        orig = FrameProcessor.process_frame
        FrameProcessor.process_frame = noop
        try:
            await tap.process_frame(TTSTextFrame("Heard sentence.", text_type="sentence"), FrameDirection.DOWNSTREAM)
            await tap.process_frame(TTSTextFrame("Upstream copy.", text_type="sentence"), FrameDirection.UPSTREAM)
            await tap.process_frame(TTSTextFrame("   ", text_type="sentence"), FrameDirection.DOWNSTREAM)
        finally:
            FrameProcessor.process_frame = orig

    asyncio.run(go())
    assert got == ["Heard sentence."]


def test_spoken_history_ignores_observer_text_when_the_tap_feeds_it():
    import asyncio
    from types import SimpleNamespace
    from pipecat.frames.frames import TTSTextFrame
    from assistant.spoken import SpokenHistory

    class Ctx:
        def get_messages(self):
            return [{"role": "user", "content": "tell me a story"}]

    sh = SpokenHistory(Ctx())
    sh.played_tap = True
    early = TTSTextFrame("Synthesised but never played.", text_type="sentence")
    asyncio.run(sh.on_push_frame(SimpleNamespace(frame=early)))
    assert sh._open is None
    sh.played(TTSTextFrame("Played sentence.", text_type="sentence"))
    assert sh._open is not None and sh._open.text == "Played sentence."


def test_default_name_is_donna(monkeypatch):
    monkeypatch.delenv("ASSISTANT_NAME", raising=False)
    from assistant.config import Config

    assert Config().assistant_name == "Donna"


def test_fixed_thermostat_never_drifts():
    from mcp_servers.tools.devices import sim

    assert sim.FIXED
    th = sim.SimThermostat()
    th.set(target=18, mode="cool")
    before = th.status()
    assert th.tick() is False
    assert th.status()["current"] == before["current"] == sim.FIXED_ROOM_C
    assert th.status()["target"] == 18 and th.status()["activity"] == "idle"
