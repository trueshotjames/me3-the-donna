"""Hermes client and task manager against a fake Hermes API server (httpx MockTransport).

    .venv\\Scripts\\python -m pytest tests -q
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from assistant.services.hermes_client import HermesClient, HermesError, run_task
from assistant import announcer as announcer_mod
from assistant.announcer import ActivityTracker, Announcer
from assistant.tasks import TaskManager


def sse(*events: tuple[str, dict]) -> bytes:
    out = [": keepalive\n\n"]
    for name, data in events:
        out.append(f"event: {name}\ndata: {json.dumps(data)}\n\n")
    return "".join(out).encode()


class FakeHermes:
    """Minimal /health, /v1/models, /v1/runs and /v1/chat/completions."""

    def __init__(self, *, runs: bool = True, key: str = "secret", delay: float = 0.0):
        self.runs = runs
        self.key = key
        self.delay = delay
        self.started: list[dict] = []
        self.stopped: list[str] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.headers.get("Authorization") != f"Bearer {self.key}":
            return httpx.Response(401, json={"error": "bad key"})
        path = request.url.path
        if path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "hermes-agent"}]})
        if path == "/v1/runs" and request.method == "POST":
            if not self.runs:
                return httpx.Response(404, json={"error": "not found"})
            body = json.loads(request.content)
            self.started.append(body)
            return httpx.Response(200, json={"run_id": f"run_{len(self.started)}", "status": "started"})
        if path.startswith("/v1/runs/") and path.endswith("/events"):
            if self.delay:
                await asyncio.sleep(self.delay)
            body = sse(
                ("tool.started", {"tool": "web_search", "preview": "weather Manila"}),
                ("tool.completed", {"tool": "web_search", "duration": 1.2, "error": False, "preview": "..."}),
                ("message.delta", {"delta": "Tomorrow in Manila: "}),
                ("message.delta", {"delta": "thirty-one degrees with showers."}),
                ("run.completed", {"run_id": path.split("/")[3], "status": "completed", "output": "Tomorrow in Manila: thirty-one degrees with showers."}),
            )
            return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})
        if path.startswith("/v1/runs/") and path.endswith("/stop"):
            self.stopped.append(path.split("/")[3])
            return httpx.Response(200, json={"status": "stopping"})
        if path.startswith("/v1/runs/"):
            return httpx.Response(200, json={"run_id": path.split("/")[3], "status": "completed", "output": "Done."})
        if path == "/v1/chat/completions":
            body = json.loads(request.content)
            self.started.append(body)
            chunks = [
                ("hermes.tool.progress", {"tool": "web_search"}),
                ("message", {"choices": [{"delta": {"content": "Chat says "}}]}),
                ("message", {"choices": [{"delta": {"content": "hello."}}]}),
            ]
            return httpx.Response(200, content=sse(*chunks) + b"data: [DONE]\n\n", headers={"content-type": "text/event-stream"})
        return httpx.Response(404)


def make_client(fake: FakeHermes, key: str = "secret") -> HermesClient:
    return HermesClient("http://hermes.test", key, transport=httpx.MockTransport(fake))


class FakeAggregator:
    def __init__(self):
        self.frames = []

    async def queue_frame(self, frame, direction=None):
        self.frames.append(frame)


def make_announcer(agg: FakeAggregator, activity: ActivityTracker | None = None) -> Announcer:
    ann = Announcer(activity)
    ann.attach(FakeAggregator(), agg)  # worker (TTSSpeakFrames) / assistant aggregator (LLM messages)
    return ann


async def wait_for(cond, secs: float = 2.0) -> bool:
    for _ in range(int(secs / 0.02)):
        if cond():
            return True
        await asyncio.sleep(0.02)
    return cond()


@pytest.mark.asyncio
async def test_check_ok_and_bad_key():
    fake = FakeHermes()
    client = make_client(fake)
    assert await client.check() == {"status": "ok", "models": ["hermes-agent"]}
    await client.close()
    bad = make_client(fake, key="wrong")
    with pytest.raises(HermesError, match="rejected the key"):
        await bad.check()
    await bad.close()


@pytest.mark.asyncio
async def test_run_task_with_runs_api():
    fake = FakeHermes()
    client = make_client(fake)
    seen = []
    run_id, out = await run_task(client, "weather in Manila tomorrow", session_id="s1", instructions="short", on_event=seen.append)
    assert run_id == "run_1"
    assert out == "Tomorrow in Manila: thirty-one degrees with showers."
    assert fake.started[0] == {"input": "weather in Manila tomorrow", "session_id": "s1", "instructions": "short"}
    assert [e.kind for e in seen][:2] == ["tool.started", "tool.completed"]
    await client.close()


@pytest.mark.asyncio
async def test_run_task_falls_back_to_chat():
    fake = FakeHermes(runs=False)
    client = make_client(fake)
    run_id, out = await run_task(client, "say hello", session_id="s2", instructions="be brief")
    assert run_id.startswith("chat:")
    assert out == "Chat says hello."
    assert client.has_runs is False
    assert fake.started[0]["messages"] == [{"role": "system", "content": "be brief"}, {"role": "user", "content": "say hello"}]
    await client.close()


@pytest.mark.asyncio
async def test_task_manager_delivers_result_to_assistant_aggregator(monkeypatch):
    # no tracker: the announcement counts as spoken after the timeout; the record waits for it
    monkeypatch.setattr(announcer_mod, "SPOKEN_TIMEOUT_SECS", 0.05)
    fake = FakeHermes()
    client = make_client(fake)
    events, records = [], []
    tm = TaskManager(client, on_event=lambda kind, **f: events.append((kind, f)), on_record=records.append)
    agg = FakeAggregator()
    ann = make_announcer(agg)
    tm.attach(ann)
    task = tm.delegate("weather in Manila tomorrow")
    assert task.id == "t1" and task.state == "running"
    assert await wait_for(lambda: task.state != "running")
    assert task.state == "done"
    assert task.tools_used == ["web_search"]
    assert [k for k, _ in events] == ["task_started", "task_progress", "task_done"]
    assert await wait_for(lambda: agg.frames)
    assert len(agg.frames) == 1
    frame = agg.frames[0]
    assert frame.run_llm is True
    assert frame.messages[0]["role"] == "user"
    assert "Background task finished" in frame.messages[0]["content"]
    assert "thirty-one degrees" in frame.messages[0]["content"]
    assert await wait_for(lambda: records)
    assert records[0]["state"] == "done" and records[0]["task_ms"] is not None and records[0]["deliver_ms"] is not None
    assert task.status()["result"].startswith("Tomorrow")
    await ann.stop()
    await tm.close()
    await client.close()


@pytest.mark.asyncio
async def test_task_manager_nudge_and_cancel():
    fake = FakeHermes(delay=1.0)
    client = make_client(fake)
    tm = TaskManager(client, nudge_secs=0.05)
    agg = FakeAggregator()
    ann = make_announcer(agg)
    tm.attach(ann)
    task = tm.delegate("slow thing")
    await asyncio.sleep(0.15)
    assert task.nudged and len(agg.frames) == 1 and "still running" in agg.frames[0].messages[0]["content"]
    assert await tm.cancel() is True
    assert task.state == "cancelled"
    assert tm.latest() is task
    await ann.stop()
    await tm.close()
    await client.close()


@pytest.mark.asyncio
async def test_tools_are_registered_and_callable():
    fake = FakeHermes()
    client = make_client(fake)
    tm = TaskManager(client)
    tm.attach(make_announcer(FakeAggregator()))
    registered = {}

    class LLM:
        def register_function(self, name, handler, **kw):
            registered[name] = handler

    schemas = tm.register(LLM())
    assert [s.name for s in schemas] == ["delegate", "task_status"]
    assert set(registered) == {"delegate", "task_status"}

    results = []

    class Params:
        def __init__(self, arguments):
            self.arguments = arguments

        async def result_callback(self, result, properties=None):
            results.append(result)

    await registered["delegate"](Params({"task": "look up something"}))
    assert results[-1]["task_id"] == "t1" and results[-1]["state"] == "running"
    await registered["task_status"](Params({"task_id": "latest"}))
    assert results[-1]["task_id"] == "t1"
    await registered["delegate"](Params({"task": ""}))
    assert "error" in results[-1]
    await tm.close()
    await client.close()


@pytest.mark.asyncio
async def test_result_waits_until_the_conversation_is_quiet(monkeypatch):
    """A result that finishes while the bot is busy must not be queued until everything is idle."""
    monkeypatch.setattr(announcer_mod, "QUIET_SECS", 0.1)
    fake = FakeHermes()
    client = make_client(fake)
    tm = TaskManager(client)
    agg = FakeAggregator()
    activity = ActivityTracker()
    activity.llm_active = True
    activity.add_call("call_1")  # e.g. the alarm tool call of the first laptop test
    ann = make_announcer(agg, activity)
    tm.attach(ann)
    task = tm.delegate("weather in Manila tomorrow")
    assert await wait_for(lambda: task.state != "running")
    assert task.state == "done"
    await asyncio.sleep(0.3)
    assert agg.frames == []  # held while the LLM is mid tool call
    activity.llm_active = False
    activity.clear_calls()
    activity.bot_speaking = True
    activity._touch()
    await asyncio.sleep(0.3)
    assert agg.frames == []  # still held while she speaks
    activity.bot_speaking = False
    activity._touch()
    assert await wait_for(lambda: agg.frames)
    assert len(agg.frames) == 1 and "Background task finished" in agg.frames[0].messages[0]["content"]
    await ann.stop()
    await tm.close()
    await client.close()


@pytest.mark.asyncio
async def test_activity_tracker_follows_frames():
    from pipecat.frames.frames import (
        BotStartedSpeakingFrame,
        BotStoppedSpeakingFrame,
        FunctionCallFromLLM,
        FunctionCallResultFrame,
        FunctionCallsStartedFrame,
        LLMFullResponseEndFrame,
        LLMFullResponseStartFrame,
        UserStartedSpeakingFrame,
        UserStoppedSpeakingFrame,
    )
    from pipecat.processors.frame_processor import FrameDirection

    tr = ActivityTracker()
    pushed = []

    async def push(frame, direction=FrameDirection.DOWNSTREAM):
        pushed.append(frame)

    tr.push_frame = push  # no pipeline needed to check the bookkeeping

    import pipecat.processors.frame_processor as fp

    async def base_noop(self, frame, direction):
        return None

    orig_pf = fp.FrameProcessor.process_frame
    fp.FrameProcessor.process_frame = base_noop
    try:
        assert not tr.busy
        await tr.process_frame(UserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        assert tr.busy and tr.user_speaking
        await tr.process_frame(UserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        await tr.process_frame(LLMFullResponseStartFrame(), FrameDirection.DOWNSTREAM)
        assert tr.llm_active
        call = FunctionCallFromLLM(function_name="create_alarm", tool_call_id="c1", arguments={}, context=None)
        await tr.process_frame(FunctionCallsStartedFrame(function_calls=[call]), FrameDirection.DOWNSTREAM)
        await tr.process_frame(LLMFullResponseEndFrame(), FrameDirection.DOWNSTREAM)
        assert tr.busy and tr.pending_calls == {"c1"}
        await tr.process_frame(
            FunctionCallResultFrame(function_name="create_alarm", tool_call_id="c1", arguments={}, result={}),
            FrameDirection.DOWNSTREAM,
        )
        await tr.process_frame(BotStartedSpeakingFrame(), FrameDirection.UPSTREAM)
        assert tr.busy and tr.bot_speaking
        assert tr.turn_open  # his query is not answered until her reply has finished playing
        await tr.process_frame(BotStoppedSpeakingFrame(), FrameDirection.UPSTREAM)
        assert not tr.busy and not tr.turn_open
        await asyncio.sleep(0.05)
        assert tr.idle_for() >= 0.04
        assert len(pushed) == 8  # every frame is passed on
    finally:
        fp.FrameProcessor.process_frame = orig_pf


@pytest.mark.asyncio
async def test_stuck_flags_expire(monkeypatch):
    """An LLM response or tool call whose end frame never arrives must not hold results for long."""
    tr = ActivityTracker()
    monkeypatch.setattr(ActivityTracker, "LLM_STALE", 0.1)
    monkeypatch.setattr(ActivityTracker, "CALL_STALE", 0.1)
    monkeypatch.setattr(ActivityTracker, "BOT_STALE", 0.1)
    tr.llm_active = True
    tr.add_call("c9")
    tr.bot_speaking = True
    assert tr.busy
    await asyncio.sleep(0.15)
    assert not tr.busy and tr.state() == "user=False bot=False llm=False calls=[] turn=False"
