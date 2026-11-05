import asyncio
import json

import pytest

from assistant.message_consent import MAX_AGE, MessageConsent, is_clear_yes


def test_clear_yes():
    for u in ("Yes.", "yeah", "Yep, send it", "Sure", "Go ahead.", "Send it.", "do it", "Okay", "oo", "Sige"):
        assert is_clear_yes(u), u
    for u in ("No.", "Yes but change it", "yes, wait", "Don't send it", "send it later, not now", "", "maybe", "Change it to hi"):
        assert not is_clear_yes(u), u


def test_same_turn_call_is_blocked():
    c = MessageConsent()
    assert c.decide("Love", "I'll be late", 1, "Text Love that I'll be late", now=0)[0] == "ask"
    assert c.decide("Love", "I'll be late", 1, "Text Love that I'll be late", now=1)[0] == "ask"  # same turn again
    assert c.pending is not None


def test_yes_next_turn_sends_exact_text():
    c = MessageConsent()
    verdict, say = c.decide("Love", "I'll be late.", 1, "Text Love that I'll be late", now=0)
    assert verdict == "ask" and say == 'To Love: "I\'ll be late." Send it?'
    assert c.decide("love", "I'll be late", 2, "Yes.", now=5) == ("send", "I'll be late.")
    assert c.pending is None
    assert c.decide("Love", "I'll be late", 3, "Yes.", now=6)[0] == "ask"  # already sent: a repeat needs a new yes


def test_no_does_not_send():
    c = MessageConsent()
    c.decide("Love", "Hi", 1, "Text Love hi", now=0)
    assert c.decide("Love", "Hi", 2, "No.", now=3)[0] == "ask"


def test_yes_but_change_does_not_send():
    c = MessageConsent()
    c.decide("Love", "Hi", 1, "Text Love hi", now=0)
    assert c.decide("Love", "Hi", 2, "Yes but change it", now=3)[0] == "ask"


def test_changed_text_reasks_then_yes_sends_revision_only():
    c = MessageConsent()
    c.decide("Love", "I'm on my way", 1, "Tell Love I'm on my way", now=0)
    verdict, say = c.decide("Love", "I'm on my way, I'll be late too", 2, "Add that I'll be late too", now=4)
    assert verdict == "ask" and "I'll be late too" in say
    assert c.decide("Love", "I'm on my way", 3, "Yes", now=8)[0] == "ask"  # the old text is no longer the draft
    c.decide("Love", "I'm on my way, I'll be late too", 4, "Add that I'll be late too", now=9)
    assert c.decide("Love", "I'm on my way, I'll be late too", 5, "Yes, send it", now=12) == ("send", "I'm on my way, I'll be late too")


def test_stale_draft_reasks():
    c = MessageConsent()
    c.decide("Love", "Hi", 1, "Text Love hi", now=0)
    assert c.decide("Love", "Hi", 2, "Yes", now=MAX_AGE + 1)[0] == "ask"


def test_yes_must_be_the_very_next_turn():
    c = MessageConsent()
    c.decide("Love", "Hi", 1, "Text Love hi", now=0)
    assert c.decide("Love", "Hi", 3, "Yes", now=10)[0] == "ask"  # a cancel turn in between


class _Params:
    def __init__(self, messages, args, llm):
        self.arguments = args
        self.llm = llm
        self.results = []
        self.context = type("Ctx", (), {"get_messages": lambda _self: messages})()

    async def result_callback(self, result, *a, **kw):
        self.results.append(result)


class _LLM:
    def __init__(self):
        self.said = []

    async def push_frame(self, frame):
        if hasattr(frame, "text"):
            self.said.append(frame.text)


def test_gate_dialogue(monkeypatch):
    pytest.importorskip("pipecat")
    import assistant.message_consent as mc
    from assistant import fast_confirm as fc

    monkeypatch.setattr(mc, "CONSENT", MessageConsent())
    llm = _LLM()
    hist = [{"role": "user", "content": "Tell Love I'm on my way"}]
    p = _Params(hist, {"contact": "Love", "text": "I'm on my way"}, llm)
    assert asyncio.run(fc._consent_gate(p)) is False
    assert json.loads(p.results[0])["sent"] is False and llm.said[-1] == 'To Love: "I\'m on my way." Send it?'
    hist += [{"role": "assistant", "content": llm.said[-1]}, {"role": "user", "content": "Cancel."}]
    hist += [{"role": "assistant", "content": "Okay."}, {"role": "user", "content": "Yes"}]
    p = _Params(hist, {"contact": "Love", "text": "I'm on my way"}, llm)
    assert asyncio.run(fc._consent_gate(p)) is False  # the yes came two turns later: read back again
    hist += [{"role": "assistant", "content": llm.said[-1]}, {"role": "user", "content": "Yes."}]
    p = _Params(hist, {"contact": "Love", "text": "I'm on my way"}, llm)
    assert asyncio.run(fc._consent_gate(p)) is True and p.arguments["text"] == "I'm on my way"
