"""v6 (wild) unit tests: voice gate, backchannels, ASR fix, <wait/> handling, LLM verdict, prompt rules, in-process MCP.
No network, no API keys."""

from __future__ import annotations

import asyncio

import numpy as np
import pytest

from assistant.services.mcp_stt import asr_fix, backchannel_only
from assistant.voice_gate import FLAGS, VoiceGate, speech_level_db, voice_print


def tone(db: float, secs: float = 1.5, f: float = 220.0, sr: int = 16000) -> bytes:
    t = np.arange(int(sr * secs)) / sr
    amp = 10 ** (db / 20) * np.sqrt(2)  # sine RMS = amp / sqrt(2)
    # a little vibrato and harmonics, so the voice print has a shape
    x = amp * (0.7 * np.sin(2 * np.pi * f * t) + 0.3 * np.sin(2 * np.pi * 3 * f * t + np.sin(5 * t)))
    return (np.clip(x, -1, 1) * 32767).astype(np.int16).tobytes()


def test_speech_level():
    assert speech_level_db(tone(-15)) == pytest.approx(-15 - 1.3, abs=1.5)
    assert speech_level_db(tone(-35)) < -33
    assert speech_level_db(tone(-15, secs=0.1)) is None  # too short to judge


def test_gate_far_and_near():
    g = VoiceGate(gap_db=10, seed_db=-16)
    assert g.judge(tone(-16)).far is False
    v = g.judge(tone(-36))
    assert v.far and "far-away" in v.reason
    off = VoiceGate(enabled=False)
    assert off.judge(tone(-36)).far is False


def test_gate_learns_within_bounds():
    g = VoiceGate(gap_db=10, seed_db=-16)
    for _ in range(6):
        g.learn(tone(-12))
    assert g.user_db == pytest.approx(-13.3, abs=1.5)
    for _ in range(12):
        g.learn(tone(-2))
    assert g.user_db <= -10  # clamped to 6 dB above the seed
    g2 = VoiceGate(gap_db=10, seed_db=-16)
    g2.learn(tone(-40))  # a far voice never teaches the level
    assert g2.user_db == -16


def test_voice_print_shadow_mode_never_rejects():
    g = VoiceGate(gap_db=10, seed_db=-16, speaker_mode="shadow", speaker_min=0.99)
    for _ in range(4):
        g.learn(tone(-16, f=200))
    v = g.judge(tone(-16, f=900))
    assert v.similarity is not None
    assert v.reason is None  # shadow: logged, not enforced
    assert voice_print(tone(-16)) is not None


def test_flags_match_transcript_with_note():
    FLAGS.set("Can we talk about something else?", passed="far")
    assert FLAGS.get("Can we talk about something else?")["passed"] == "far"
    assert FLAGS.get("can we talk about something else\n\n[now: x]")["passed"] == "far"
    assert FLAGS.get("Something else entirely") == {}


@pytest.mark.parametrize(
    "text,expected",
    [("Okay.", True), ("Mm-hmm.", True), ("Uh-huh, right.", True), ("Yeah, I see.", True), ("Okay, stop.", False),
     ("Wait, what about Paris?", False), ("", False), ("Turn it off.", False)],
)
def test_backchannel_only(text, expected):
    assert backchannel_only(text) is expected


def test_asr_warmer_fix():
    assert asr_fix("Make it a bit water in here.") == "Make it a bit warmer in here."
    assert asr_fix("Can I have a glass of water?") == "Can I have a glass of water?"


def test_passfilter_drops_wait():
    from pipecat.frames.frames import LLMFullResponseEndFrame, LLMFullResponseStartFrame, LLMTextFrame
    from pipecat.processors.frame_processor import FrameDirection

    from assistant.processors import PassFilter

    out = []
    pf = PassFilter()

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

    async def main():
        from pipecat.processors.frame_processor import FrameProcessor

        orig = FrameProcessor.process_frame

        async def noop(self, frame, direction):
            return None

        FrameProcessor.process_frame = noop
        try:
            assert (await run(["<wa", "it/>"])).strip() == ""
            assert (await run(["<pass/>"])).strip() == ""
            assert (await run(["Tok", "yo."])).strip() == "Tokyo."
        finally:
            FrameProcessor.process_frame = orig

    asyncio.run(main())


def test_timed_stream_verdicts():
    from openai.types.chat import ChatCompletionChunk

    from assistant.services.timed_llm import _TimedStream

    def chunk(content=None, tool=False):
        delta = {"role": "assistant"}
        if content is not None:
            delta["content"] = content
        if tool:
            delta["tool_calls"] = [{"index": 0, "id": "c1", "type": "function", "function": {"name": "x", "arguments": "{}"}}]
        return ChatCompletionChunk.model_validate(
            {"id": "t", "object": "chat.completion.chunk", "created": 0, "model": "m",
             "choices": [{"index": 0, "delta": delta, "finish_reason": None}]}
        )

    class S:
        def __init__(self, chunks):
            self.chunks = chunks

        async def __aiter__(self):
            for c in self.chunks:
                yield c

    async def verdict_of(chunks):
        got = []

        async def cb(kind):
            got.append(kind)

        call = {"content_t": [], "chunks": 0, "chars": 0}
        async for _ in _TimedStream(S(chunks), call, on_verdict=cb):
            pass
        await asyncio.sleep(0)
        return got

    async def main():
        assert await verdict_of([chunk("<"), chunk("wait/>")]) == ["wait"]
        assert await verdict_of([chunk("Tokyo"), chunk(".")]) == ["go"]
        assert await verdict_of([chunk(tool=True)]) == ["tool"]  # v6 smoke: never ends the turn (t6 race)
        assert await verdict_of([chunk("<pass/>")]) == ["go"]
        assert await verdict_of([chunk("<"), chunk("w"), chunk("a"), chunk("it/>")]) == ["wait"]

    asyncio.run(main())


def test_prompt_rules_switch():
    from assistant.prompts import system_prompt

    base = system_prompt("Pi", "King", 8)
    full = system_prompt("Pi", "King", 8, wait_rule=True, fixes=True)
    assert "<wait/>" not in base and "<wait/>" in full
    assert "list_alarms or list_reminders first" in full and "list_alarms or list_reminders first" not in base


def test_edit_task_text(monkeypatch):
    from assistant import tasks as tasks_mod

    assert tasks_mod.FIXES is True
    t = tasks_mod.Task(id="t2", text="x")
    assert t.check_text == ""


def test_inproc_mcp_bridge_lists_tools(monkeypatch):
    """The ASR and TTS servers answer real MCP requests in this process (no child process, no network)."""
    from assistant.mcp_bridge import InProcParams, MCPBridge, server_params

    assert isinstance(server_params("mcp_servers.asr.server", "inproc"), InProcParams)
    assert not isinstance(server_params("mcp_servers.asr.server", "stdio"), InProcParams)

    async def main():
        for name, module, want in (("asr", "mcp_servers.asr.server", "asr_push"), ("tts", "mcp_servers.tts.server", "tts_read")):
            b = MCPBridge(name, stdio=InProcParams(module))
            await b.start(timeout=20)
            try:
                assert want in await b.list_tools()
            finally:
                await b.close()

    asyncio.run(main())


# ------------------------------------------------------------- v6 smoke fixes (Tokyo answered with Rome, t6, s07)


class _Ctx:
    def __init__(self, messages):
        self._m = list(messages)

    def get_messages(self):
        return self._m

    def set_messages(self, m):
        self._m[:] = m


def _spoken(ctx, *utts):
    from assistant.spoken import SpokenHistory, Utterance

    sh = SpokenHistory(ctx)
    for after, text, cut in utts:
        sh.utterances.append(Utterance(after=after, parts=[text], interrupted=cut, closed=True))
    return sh


def test_spoken_history_inserts_cut_off_reply_before_the_correction():
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "Describe the history of Rome in detail."},
            {"role": "user", "content": "Wait. I meant Tokyo.\n\n[now: x]"}]
    sh = _spoken(_Ctx(msgs), ("describe the history of rome in detail", "Rome began as a small settlement.", True))
    fixed = sh.repair(msgs)
    assert [m["role"] for m in fixed] == ["system", "user", "assistant", "user"]
    assert fixed[2]["content"].startswith("Rome began") and fixed[2]["content"].endswith("…")
    assert sh.repair(fixed) is None  # idempotent


def test_spoken_history_completes_a_partial_copy_and_skips_a_full_one():
    msgs = [{"role": "user", "content": "Tell me a story."}, {"role": "assistant", "content": "The sea was calm."},
            {"role": "user", "content": "Stop."}, {"role": "assistant", "content": "Okay, stopping."}]
    sh = _spoken(_Ctx(msgs), ("tell me a story", "The sea was calm. A keeper lived there.", True),
                 ("stop", "Okay, stopping.", False))
    fixed = sh.repair(msgs)
    assert len(fixed) == 4 and fixed[1]["content"] == "The sea was calm. A keeper lived there. …"


def test_spoken_history_ignores_trimmed_turns():
    msgs = [{"role": "user", "content": "Hi."}]
    assert _spoken(_Ctx(msgs), ("long gone", "Old reply.", False)).repair(msgs) is None


def test_wait_offer_is_a_system_line_not_in_the_turn_note():
    from assistant.services import timed_llm

    assert "<wait/>" in timed_llm.WAIT_SYSTEM
    from assistant.prompts import WAIT_RULE

    assert "system line" in WAIT_RULE and "Wait, I meant" in WAIT_RULE


def test_set_thermostat_change(monkeypatch, tmp_path):
    import json

    from mcp_servers.tools import server as srv

    before = float(srv.devices.thermostat.status()["target"])
    out = json.loads(srv.set_thermostat(change=1))
    assert float(out["target"]) == pytest.approx(before + 1, abs=0.11)
    srv.set_thermostat(target=before)


def test_thermostat_guard_accepts_change():
    from assistant import fast_confirm as fc

    class P:
        arguments = {"change": 1}

    class Client:
        async def _call_tool_text(self, *a):
            raise AssertionError("the guard must not read the status for a right-direction change")

    fc._last_user = lambda params: "Make it a bit warmer in here."
    asyncio.run(fc.relative_thermostat(P(), Client()))
    assert P.arguments == {"change": 1}
    assert fc._thermostat({"change": 1}, {"target": 23.0}) == "Thermostat set to 23 degrees."


def test_spoken_history_repairs_real_and_provisional_context():
    real = _Ctx([{"role": "user", "content": "Describe Rome."}])
    prov = _Ctx(real.get_messages() + [{"role": "user", "content": "Wait. I meant Tokyo."}])
    sh = _spoken(real, ("describe rome", "Rome began small.", True))
    for ctx in {id(c): c for c in (sh.context, prov)}.values():  # as TimedGroqLLMService.get_chat_completions does
        fixed = sh.repair(ctx.get_messages())
        if fixed is not None:
            ctx.set_messages(fixed)
    assert [m["role"] for m in real.get_messages()] == ["user", "assistant"]
    assert [m["role"] for m in prov.get_messages()] == ["user", "assistant", "user"]


# ------------------------------------------------------------- EOT_CHECK=separate (after the Pi run)


def test_cut_check_prompt_and_parse():
    from assistant import eot_check as ec

    assert ec.prompt("Dad,\n\n[now: x]", "Who should I text?") == 'Assistant just said: "Who should I text?"\nUser (paused): "Dad"'
    assert ec.parse("cut") == "wait" and ec.parse(" Done.") == "go" and ec.parse("maybe") is None


def test_cut_check_verdict_and_timeout():
    from assistant.eot_check import CutOffCheck

    class Msg:
        def __init__(self, t):
            self.message = type("M", (), {"content": t})()

    class Comp:
        def __init__(self, out, delay=0.0):
            self.out, self.delay, self.kw = out, delay, None

        async def create(self, **kw):
            self.kw = kw
            await asyncio.sleep(self.delay)
            return type("R", (), {"choices": [Msg(self.out)]})()

    def client(comp):
        return type("C", (), {"chat": type("Ch", (), {"completions": comp})()})()

    comp = Comp("cut")
    chk = CutOffCheck(client(comp), model="openai/gpt-oss-120b", reasoning_effort="low", timeout_secs=1.0)
    assert asyncio.run(chk.verdict("Can you tell me about the")) == "wait"
    assert comp.kw["reasoning_effort"] == "low" and comp.kw["max_tokens"] == 200
    slow = CutOffCheck(client(Comp("done", delay=0.3)), model="m", timeout_secs=0.05)
    assert asyncio.run(slow.verdict("Play some jazz")) is None and slow.stats["timeout"] == 1


def test_spoken_history_cuts_an_unspoken_tail_back_to_what_was_heard():
    msgs = [{"role": "user", "content": "tell me a story"},
            {"role": "assistant", "content": "The sea was calm. A keeper lived there. He loved the light."}]
    sh = _spoken(_Ctx(msgs), ("tell me a story", "The sea was calm.", True))
    out = sh.repair(msgs)
    assert out is not None and out[1]["content"] == "The sea was calm. …"
    assert "keeper" not in str(out)
