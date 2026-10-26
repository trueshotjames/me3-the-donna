"""Voice tone hints: thresholds, smoothing, and the processors with a fake scorer."""

from __future__ import annotations

import asyncio

import numpy as np
import pytest
from pipecat.frames.frames import (
    InputAudioRawFrame,
    LLMContextFrame,
    UserStoppedSpeakingFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.frame_processor import FrameDirection

from assistant.emotion import EmotionProcessor, Scores, Thresholds, VoiceHintInjector, hint_line

THR = Thresholds()


def test_hint_line_thresholds():
    assert hint_line(Scores(0.5, 0.5, 0.5), THR) is None
    assert hint_line(Scores(0.35, 0.65, 0.2), THR) is None  # edges are inside the deadzone
    assert hint_line(Scores(0.2, 0.5, 0.5), THR) == "Voice: the user sounds calm and low in energy. Match your tone and length to that."
    assert hint_line(Scores(0.9, 0.3, 0.1), THR) == (
        "Voice: the user sounds energetic and agitated, hesitant and unsure, and upset and negative. "
        "Match your tone and length to that."
    )
    assert hint_line(Scores(0.5, 0.5, 0.81), THR) == "Voice: the user sounds pleased and positive. Match your tone and length to that."
    assert hint_line(Scores(0.5, 0.5, 0.79), THR) is None  # valence deadzone is 0.2..0.8
    assert hint_line(None, THR) is None
    assert "uncertain" not in (hint_line(Scores(0.9, 0.9, 0.9), THR) or "")


class FakeScorer:
    def __init__(self, value=(0.9, 0.2, 0.5)):
        self.value = value
        self.calls: list[int] = []

    def __call__(self, audio):
        self.calls.append(len(audio))
        return self.value


def pcm(seconds: float, rate: int = 16000) -> bytes:
    return (np.random.randn(int(seconds * rate)) * 3000).astype(np.int16).tobytes()


async def feed(proc: EmotionProcessor, frames):
    for f in frames:
        await proc.process_frame(f, FrameDirection.DOWNSTREAM)


@pytest.mark.asyncio
async def test_processor_scores_while_speaking_and_smooths_across_turns():
    scorer = FakeScorer((0.9, 0.2, 0.5))
    got = []
    proc = EmotionProcessor(scorer, window_secs=3.0, every_secs=0.0, min_secs=0.5, smoothing=0.5, on_scores=got.append)
    proc._enable_direct_mode = True  # noqa: SLF001 - no pipeline in the test

    # 20 ms chunks for 1.0 s of speech
    chunks = [InputAudioRawFrame(audio=pcm(0.02), sample_rate=16000, num_channels=1) for _ in range(50)]
    await feed(proc, [VADUserStartedSpeakingFrame()] + chunks + [VADUserStoppedSpeakingFrame()])
    await asyncio.sleep(0.05)
    assert scorer.calls, "the scorer ran during speech"
    assert all(n <= 3 * 16000 for n in scorer.calls)
    assert proc.smoothed.arousal == pytest.approx(0.9)  # first turn: no history, no smoothing
    assert got[-1].infer_ms >= 0

    await feed(proc, [UserStoppedSpeakingFrame()])  # turn closed -> becomes the memory
    scorer.value = (0.1, 0.2, 0.5)
    chunks = [InputAudioRawFrame(audio=pcm(0.02), sample_rate=16000, num_channels=1) for _ in range(50)]
    await feed(proc, [VADUserStartedSpeakingFrame()] + chunks + [VADUserStoppedSpeakingFrame()])
    await asyncio.sleep(0.05)
    assert proc.latest.arousal == pytest.approx(0.1)
    assert proc.smoothed.arousal == pytest.approx(0.5)  # 0.5 * 0.9 + 0.5 * 0.1


@pytest.mark.asyncio
async def test_injector_rewrites_system_message_each_turn():
    current = {"s": Scores(0.9, 0.5, 0.5)}
    hints = []
    inj = VoiceHintInjector("BASE PROMPT", lambda: current["s"], THR, on_hint=lambda h, s: hints.append(h))
    inj._enable_direct_mode = True  # noqa: SLF001
    pushed = []

    async def push(frame, direction=FrameDirection.DOWNSTREAM):
        pushed.append(frame)

    inj.push_frame = push  # type: ignore[method-assign]

    ctx = LLMContext(messages=[{"role": "system", "content": "BASE PROMPT"}, {"role": "user", "content": "hi"}])
    await inj.process_frame(LLMContextFrame(context=ctx), FrameDirection.DOWNSTREAM)
    assert ctx.messages[0]["content"] == "BASE PROMPT\n\nVoice: the user sounds energetic and agitated. Match your tone and length to that."
    assert ctx.messages[1] == {"role": "user", "content": "hi"}

    current["s"] = Scores(0.5, 0.5, 0.5)
    await inj.process_frame(LLMContextFrame(context=ctx), FrameDirection.DOWNSTREAM)
    assert ctx.messages[0]["content"] == "BASE PROMPT"  # hint removed again, nothing accumulates
    assert hints == ["Voice: the user sounds energetic and agitated. Match your tone and length to that.", None]
    assert len(pushed) == 2


def test_voice_hint_belongs_to_the_open_user_turn_during_eager_end_of_turn():
    """The hint is injected when the early (eager) LLM call starts, before Flux closes the turn."""
    import time as _t

    from assistant.turn_metrics import Recorder

    rec = Recorder()
    rec.start_of_turn(_t.time(), None)
    turn = rec.current
    turn.flux_start = turn.flux_start or _t.time()
    assert turn.eot is None
    rec.voice_hint("Voice: the user sounds energetic and agitated.", {"arousal": 0.8})
    assert turn.voice == {"hint": "Voice: the user sounds energetic and agitated.", "arousal": 0.8}
    assert rec.reply is None  # no phantom bot turn


def test_speakable_cleans_gpt_oss_text():
    from assistant.processors import speakable

    assert speakable("October twenty\\u2011six at nine o’clock") == "October twenty-six at nine o'clock"
    assert speakable("hot — and humid…") == "hot, and humid..."
    assert speakable("plain text.") == "plain text."
