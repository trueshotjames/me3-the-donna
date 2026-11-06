"""v11 (HOLD_TOOL_CALLS): a tool call from an early reply is held, never run before the turn is confirmed."""

import asyncio

import pytest
from pipecat.frames.frames import (
    EagerEndOfTurnCancelFrame,
    FunctionCallFromLLM,
    InterruptionFrame,
    LLMRunFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.llm_service import LLMService

from assistant.services.timed_llm import TimedGroqLLMService


def make(hold: bool, committed: str | None = "set a timer for five minutes"):
    llm = TimedGroqLLMService(api_key="test", hold_tool_calls=hold)
    ran: list = []  # (time, calls) every time Pipecat's runner is reached
    pushed: list = []  # frames that left the service
    broadcast: list = []

    async def fake_run(self, calls):  # Pipecat's run_function_calls, as the base class sees it
        ran.append((asyncio.get_running_loop().time(), list(calls), self._speculation_gate.is_speculating))
        if self._speculation_gate.is_speculating:
            await self.broadcast_frame(EagerEndOfTurnCancelFrame)  # what 1.13 does: withdraw, run nothing

    async def fake_push(self, frame, direction=FrameDirection.DOWNSTREAM):
        pushed.append((frame, direction))

    async def fake_broadcast(cls, **kw):
        broadcast.append(cls)

    llm._ran, llm._pushed, llm._broadcast = ran, pushed, broadcast
    llm._fake_run, llm._fake_push = fake_run, fake_push
    llm.broadcast_frame = fake_broadcast
    real = LLMContext(messages=[{"role": "system", "content": "s"}])
    llm.real_context = real
    llm.committed_text = (lambda: committed) if committed is not None else None
    llm._last_speculative_text = "Set a timer for five minutes."
    return llm, real


def call(name="create_timer", args=None):
    return FunctionCallFromLLM(function_name=name, tool_call_id=f"id_{name}", arguments=args or {"minutes": 5},
                               context=LLMContext(messages=[{"role": "user", "content": "provisional"}]))


@pytest.fixture
def patched(monkeypatch):
    def apply(llm):
        from pipecat.processors.frame_processor import FrameProcessor

        monkeypatch.setattr(LLMService, "run_function_calls", llm._fake_run)
        monkeypatch.setattr(FrameProcessor, "push_frame", llm._fake_push)
        llm.create_task = lambda coro, name=None: asyncio.get_running_loop().create_task(coro)

    return apply


async def confirm(llm, real, write_user=True):
    if write_user:
        real.add_message({"role": "user", "content": "Set a timer for five minutes."})
    await llm.push_frame(UserStoppedSpeakingFrame())
    if llm._hold_task:
        await llm._hold_task


async def test_flag_off_keeps_pipecat_behaviour(patched):
    llm, real = make(hold=False)
    patched(llm)
    llm._speculation_gate.begin_speculation(True)
    await llm.run_function_calls([call()])
    assert len(llm._ran) == 1 and llm._ran[0][2] is True  # reached Pipecat while speculating
    assert llm._broadcast == [EagerEndOfTurnCancelFrame]  # ... which withdraws the early reply
    assert llm.tool_hold.held is None


async def test_held_call_runs_once_after_confirmation(patched):
    llm, real = make(hold=True)
    patched(llm)
    llm._speculation_gate.begin_speculation(True)
    await llm.run_function_calls([call()])
    assert llm._ran == [] and llm._broadcast == []  # not run, early reply not withdrawn
    assert llm.tool_hold.held is not None
    await confirm(llm, real)
    assert len(llm._ran) == 1
    _, calls, speculating = llm._ran[0]
    assert speculating is False  # safeguard 1: run only after the gate stopped speculating
    assert calls[0].context is real  # against the conversation, not the provisional copy
    assert llm.tool_hold.stats["released"] == 1


async def test_parallel_and_delegate_calls_are_held_together(patched):
    llm, real = make(hold=True)
    patched(llm)
    llm._speculation_gate.begin_speculation(True)
    await llm.run_function_calls([call("delegate", {"task": "make a website"}), call("get_weather")])
    assert llm._ran == []
    await confirm(llm, real)
    assert [c.function_name for c in llm._ran[0][1]] == ["delegate", "get_weather"]


@pytest.mark.parametrize("frame", [EagerEndOfTurnCancelFrame(), InterruptionFrame(), UserStartedSpeakingFrame()])
async def test_withdrawal_drops_the_call_and_it_never_runs(patched, frame):
    llm, real = make(hold=True)
    patched(llm)
    llm._speculation_gate.begin_speculation(True)
    await llm.run_function_calls([call()])
    await llm.push_frame(frame)
    assert llm.tool_hold.held is None
    await confirm(llm, real)  # the later turn end must not resurrect it
    assert llm._ran == []
    assert llm.tool_hold.stats["dropped"] == 1


async def test_committed_words_differ_drops_and_asks_again(patched):
    llm, real = make(hold=True, committed="set a timer for fifteen minutes")
    patched(llm)
    llm._speculation_gate.begin_speculation(True)
    await llm.run_function_calls([call()])
    await confirm(llm, real)
    assert llm._ran == []  # safeguard 2
    assert any(isinstance(f, LLMRunFrame) and d == FrameDirection.UPSTREAM for f, d in llm._pushed)
    assert llm.tool_hold.stats["mismatch"] == 1


async def test_interruption_after_confirmation_but_before_start(patched):
    llm, real = make(hold=True)
    patched(llm)
    llm._speculation_gate.begin_speculation(True)
    await llm.run_function_calls([call()])
    await llm.push_frame(UserStoppedSpeakingFrame())  # confirmed, but the user message is not in the context yet
    await asyncio.sleep(0.02)
    await llm.push_frame(InterruptionFrame())  # barge-in while the call waits for the history
    await asyncio.sleep(0.6)
    assert llm._ran == []


async def test_not_speculating_runs_immediately(patched):
    llm, real = make(hold=True)
    patched(llm)
    llm._speculation_gate.begin_speculation(False)
    await llm.run_function_calls([call()])
    assert len(llm._ran) == 1 and llm.tool_hold.held is None


async def test_new_inference_drops_held(patched):
    from pipecat.frames.frames import LLMContextFrame

    llm, real = make(hold=True)
    patched(llm)
    llm._speculation_gate.begin_speculation(True)
    await llm.run_function_calls([call()])
    from pipecat.services.openai.base_llm import BaseOpenAILLMService

    async def noop(self, frame, direction):
        pass

    import unittest.mock as um

    with um.patch.object(BaseOpenAILLMService, "process_frame", noop):
        await llm.process_frame(LLMContextFrame(context=real), FrameDirection.DOWNSTREAM)
    assert llm.tool_hold.held is None
    await confirm(llm, real)
    assert llm._ran == []
