"""Long-term memory: transcripts on disk, the note refreshed by a (fake) model, recall."""

from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest

from assistant.announcer import ActivityTracker
from assistant.memory import Memory, MemoryStore, Summarizer, _transcript


class FakeModel:
    """OpenAI-compatible /chat/completions that writes a note from the transcript it was given."""

    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self.calls: list[dict] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.calls.append(body)
        if self.fail:
            return httpx.Response(500, text="boom")
        user = body["messages"][1]["content"]
        transcript = user.split("TRANSCRIPT:\n", 1)[1]
        people = "King" if "King" in transcript else "someone"
        text = f"People\n- {people} mentioned: {' / '.join(line for line in transcript.splitlines() if line.startswith('User:'))[:200]}\n---\n{people} talked about {len(transcript.splitlines())} lines."
        return httpx.Response(200, json={"choices": [{"message": {"content": text}}]})


class FakeContext:
    def __init__(self, messages):
        self.messages = messages

    def get_messages(self):
        return self.messages

    def set_messages(self, messages):
        self.messages = list(messages)


def make_memory(tmp_path, *, fake: FakeModel | None = None, **kw) -> tuple[Memory, MemoryStore, list]:
    store = MemoryStore(tmp_path / "memory")
    summarizer = None
    if fake is not None:
        summarizer = Summarizer("k", "http://model.test/v1", "test-model", transport=httpx.MockTransport(fake))
    notes = []
    mem = Memory(store, summarizer, user_name="King", assistant_name="Pi", on_note=notes.append, **kw)
    return mem, store, notes


def convo(*pairs: tuple[str, str]) -> list[dict]:
    msgs = [{"role": "system", "content": "prompt"}]
    for u, a in pairs:
        msgs.append({"role": "user", "content": u})
        msgs.append({"role": "assistant", "content": a})
    return msgs


def test_observe_records_once_and_skips_pass(tmp_path):
    mem, store, _ = make_memory(tmp_path)
    msgs = convo(("I'm King, I like my tea without sugar", "Noted."), ("tv noise", "<pass/>"))
    msgs.append({"role": "assistant", "content": None, "tool_calls": [{"function": {"name": "create_alarm"}}]})
    msgs.append({"role": "tool", "content": '{"created": true}'})
    msgs.append({"role": "user", "content": "Background task finished: rain tomorrow"})
    mem.observe(msgs)
    mem.observe(msgs)  # same dicts again: nothing new
    mem.observe([dict(m) for m in msgs])  # rebuilt dicts with the same content: nothing new either
    roles = [r["role"] for r in mem.episode.records]
    assert roles == ["user", "assistant", "user", "assistant", "tool", "background"]
    assert mem.episode.records[1]["content"] == "Noted." and mem.episode.records[3]["tool_calls"] == ["create_alarm"]
    on_disk = store.read(mem.episode.path)
    assert len(on_disk) == 6 and on_disk[0]["content"].startswith("I'm King")
    text = _transcript(mem.episode.records, "Pi")
    assert "User: I'm King" in text and "[Pi called create_alarm]" in text and "[background assistant:" in text


@pytest.mark.asyncio
async def test_long_episode_closes_and_the_note_is_updated_in_the_background(tmp_path):
    fake = FakeModel()
    mem, store, notes = make_memory(tmp_path, fake=fake, episode_messages=4)
    ctx = FakeContext(convo(("I'm King and I work at the university", "Nice to meet you."), ("my dentist is on Friday", "Okay.")))
    mem.attach(ctx)
    mem.observe(ctx.messages)
    assert await _wait(lambda: notes)
    assert "King" in notes[0] and store.note() == notes[0]
    rows = store.index()
    assert len(rows) == 1 and rows[0]["summarized"] and rows[0]["reason"] == "long" and rows[0]["turns"] == 2
    assert rows[0]["summary"].startswith("King talked about")
    assert len(ctx.messages) == 5  # a "long" close keeps the context
    assert mem.episode.records == []  # a new episode has started
    assert "CURRENT NOTE:\n(empty)" in fake.calls[0]["messages"][1]["content"]
    assert fake.calls[0]["model"] == "test-model"
    await mem.stop()


@pytest.mark.asyncio
async def test_failed_summary_is_folded_into_the_next_episode(tmp_path):
    fake = FakeModel(fail=True)
    mem, store, notes = make_memory(tmp_path, fake=fake, episode_messages=2)
    mem.observe(convo(("first conversation about the garden", "ok")))
    await _wait(lambda: len(fake.calls) == 1)
    await asyncio.sleep(0.05)
    assert store.unsummarized() and notes == []
    fake.fail = False
    mem.observe(convo(("second conversation about the car", "ok")))
    assert await _wait(lambda: notes)
    assert "garden" in fake.calls[1]["messages"][1]["content"] and "car" in fake.calls[1]["messages"][1]["content"]
    assert all(r["summarized"] for r in store.index()) and len(store.index()) == 2
    await mem.stop()


def test_repeats_are_recorded_but_rebuilt_history_is_not(tmp_path):
    """The first laptop run: each LLM call re-recorded every user line."""
    mem, store, _ = make_memory(tmp_path)
    hist = [{"role": "system", "content": "p"}, {"role": "user", "content": "Hello?"}]
    mem.observe(hist)
    hist = [dict(m) for m in hist] + [{"role": "user", "content": "Can you hear me?"}]
    mem.observe(hist)
    hist = [dict(m) for m in hist] + [{"role": "user", "content": "Hello?"}]  # really said again
    mem.observe(hist)
    mem.observe([dict(m) for m in hist])
    assert [r["content"] for r in mem.episode.records] == ["Hello?", "Can you hear me?", "Hello?"]


def test_idle_close_resets_the_context_unless_busy(tmp_path):
    mem, store, _ = make_memory(tmp_path)
    tracker = ActivityTracker()
    ctx = FakeContext(convo(("hello", "hi")))
    mem.attach(ctx, tracker)
    mem.observe(ctx.messages)
    tracker.llm_active = True
    mem.close_episode("idle", reset_context=True)
    assert len(ctx.messages) == 3  # busy: left alone
    mem.observe(ctx.messages)
    tracker.llm_active = False
    mem.close_episode("idle", reset_context=True)
    assert ctx.messages == [{"role": "system", "content": "prompt"}]
    assert [r["reason"] for r in store.index()] == ["idle", "idle"]


@pytest.mark.asyncio
async def test_recall_finds_earlier_days(tmp_path):
    mem, store, _ = make_memory(tmp_path)
    old = store.new_episode()
    t = time.time() - 5 * 86400
    store.append(old, {"t": t, "role": "user", "content": "remind me that my dentist appointment is on Friday at ten"})
    store.append(old, {"t": t + 2, "role": "assistant", "content": "Reminder set for Friday at ten."})
    store.append(old, {"t": t + 60, "role": "user", "content": "what's the weather"})
    found = store.recall("dentist appointment")
    assert len(found["matches"]) == 1
    m = found["matches"][0]
    assert m["who"] == "user" and "dentist" in m["text"] and m["reply"].startswith("Reminder set") and "," in m["when"]
    assert store.recall("the and of") == {"matches": [], "episodes": []}

    results = []

    class Params:
        arguments = {"query": "dentist"}

        async def result_callback(self, result, properties=None):
            results.append(result)

    await mem._recall_tool(Params())
    assert results[0]["matches"][0]["who"] == "user"
    Params.arguments = {"query": "submarine"}
    await mem._recall_tool(Params())
    assert results[1]["matches"] == [] and "nothing recorded" in results[1]["note"]


@pytest.mark.asyncio
async def test_stop_closes_the_open_episode_and_waits_for_the_note(tmp_path):
    fake = FakeModel()
    mem, store, notes = make_memory(tmp_path, fake=fake)
    ctx = FakeContext(convo(("I'm King", "Hello King.")))
    mem.attach(ctx)
    await mem.start()
    await mem.stop()
    assert notes and store.index()[0]["reason"] == "shutdown"


async def _wait(cond, secs: float = 2.0) -> bool:
    for _ in range(int(secs / 0.02)):
        if cond():
            return True
        await asyncio.sleep(0.02)
    return bool(cond())
