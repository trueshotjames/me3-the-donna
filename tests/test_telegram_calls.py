"""Direct Telegram calls (telegram_calls.py) and the consent turn count across the history trim."""

import asyncio
from types import SimpleNamespace as NS

import pytest

from assistant.message_consent import MessageConsent
from mcp_servers.tools.devices.telegram_calls import DirectCaller, end_reason, pick_device


def _devs(*names):
    return [NS(name=n) for n in names]


def test_pick_device_prefers_jabra_and_skips_monitors():
    devs = _devs("Monitor of Jabra", "Built-in Audio Stereo", "Jabra SPEAK 510 USB Mono")
    assert pick_device(devs, None).name == "Jabra SPEAK 510 USB Mono"
    assert pick_device(_devs("Monitor of X", "Laptop Mic"), "").name == "Laptop Mic"
    assert pick_device(devs, "built-in").name == "Built-in Audio Stereo"
    assert pick_device(devs, "nope") is None
    assert pick_device(_devs("Monitor of X"), None) is None


def test_end_reasons():
    class CallDeclined(Exception):
        pass

    class TimedOutAnswer(Exception):
        pass

    assert end_reason(CallDeclined()) == "declined"
    assert end_reason(TimedOutAnswer()) == "no answer"
    assert end_reason(ValueError("boom")) == "failed: boom"


class _Tg:
    """play() rings until cancelled, as py-tgcalls does while it holds its per-chat lock."""

    def __init__(self, fail=None):
        self.fail, self.left, self.recorded = fail, [], []

    async def play(self, peer, stream, config):
        if self.fail:
            raise self.fail
        await asyncio.sleep(60)

    async def record(self, peer, stream):
        self.recorded.append(peer)

    async def leave_call(self, peer):
        self.left.append(peer)


def _caller(tg, ended):
    c = DirectCaller.__new__(DirectCaller)
    c._tg, c._on_end, c._task, c._peer, c.incoming = tg, ended.append, None, None, None
    c._streams = lambda: ("send", "play")
    return c


def test_hang_up_while_ringing_stops_the_dial_then_leaves():
    async def go():
        tg, ended = _Tg(), []
        c = _caller(tg, ended)
        await c.dial(5)
        await asyncio.sleep(0)
        task = c._task
        assert await c.hang_up() is True
        await asyncio.sleep(0)
        assert task.cancelled() and tg.left == [5] and ended == []
        assert await c.hang_up() is False  # nothing left to end

    asyncio.run(go())


def test_declined_call_reports_the_reason():
    class CallDeclined(Exception):
        pass

    async def go():
        ended = []
        c = _caller(_Tg(fail=CallDeclined()), ended)
        await c.dial(5)
        await c._task
        assert ended == ["declined"] and c._peer is None

    asyncio.run(go())


def test_answer_picks_up_only_a_waiting_call():
    async def go():
        c = _caller(_Tg(), [])
        assert await c.answer() is None
        c.incoming = 7
        assert await c.answer() == 7 and c.incoming is None and c._peer == 7
        c._task.cancel()

    asyncio.run(go())


def test_yes_sends_after_the_history_trim():
    """The bug on the Pi: past LLM_HISTORY_MESSAGES the user count stood still, so "yes" never counted as the next turn."""
    pytest.importorskip("pipecat")
    from assistant import fast_confirm as fc
    from assistant.services.timed_llm import trim_history

    class Ctx:
        def __init__(self, msgs):
            self.msgs = msgs

        def get_messages(self):
            return self.msgs

        def set_messages(self, msgs):
            self.msgs = msgs

    def trim(ctx):  # what TimedGroqLLMService does before every call
        msgs, kept = ctx.msgs, trim_history(list(ctx.msgs), 12)
        ctx.trimmed_user_turns = getattr(ctx, "trimmed_user_turns", 0) + sum(m["role"] == "user" for m in msgs) - sum(m["role"] == "user" for m in kept)
        ctx.set_messages(kept)

    ctx = Ctx([m for i in range(8) for m in ({"role": "user", "content": f"q{i}"}, {"role": "assistant", "content": "a"})])
    ctx.msgs.append({"role": "user", "content": "Tell Love I love her"})
    trim(ctx)
    c = MessageConsent()
    assert c.decide("Love", "I love you", fc._user_turn(NS(context=ctx)), "Tell Love I love her")[0] == "ask"
    ctx.msgs += [{"role": "assistant", "content": 'To Love: "I love you." Send it?'}, {"role": "user", "content": "Yes."}]
    trim(ctx)
    assert c.decide("Love", "I love you", fc._user_turn(NS(context=ctx)), "Yes.") == ("send", "I love you")


