"""Voice tone hints from audEERING's Wav2Small (arousal, dominance, valence, each 0..1).

Two small pieces, both on the Pi:

    EmotionProcessor   sits after the VAD. Keeps the last EMOTION_WINDOW_SECS of microphone
                       audio, scores it in a worker thread every EMOTION_EVERY_SECS while the
                       user speaks and once more when they stop. 17K parameters, ONNX, a few
                       ms per window on a laptop; measured on the Pi (`emotion.infer_ms` in
                       turns.jsonl).
    VoiceHintInjector  sits right before the LLM. On every context it rewrites the system
                       message so it ends with one line like
                           Voice: the user sounds hesitant and upset. Match your tone and length to that.
                       or no line at all when every value is inside its deadzone.

Thresholds (from King): arousal and dominance hint below AD_LOW (0.35) or above AD_HIGH (0.65);
valence below V_LOW (0.20) or above V_HIGH (0.80). Values are smoothed with the previous
turn (EMOTION_SMOOTHING, 0.5 = equal weight) before the thresholds apply.

The model comes from tools/export_wav2small.py (PyTorch weights -> models/wav2small.onnx).
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np
from loguru import logger
from pipecat.frames.frames import (
    Frame,
    InputAudioRawFrame,
    LLMContextFrame,
    UserStoppedSpeakingFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

WORDS = {
    "arousal": ("calm and low in energy", "energetic and agitated"),
    "dominance": ("hesitant and unsure", "assertive and firm"),
    "valence": ("upset and negative", "pleased and positive"),
}


@dataclass(frozen=True)
class Thresholds:
    ad_low: float = 0.35
    ad_high: float = 0.65
    v_low: float = 0.20
    v_high: float = 0.80

    def label(self, dim: str, value: float) -> str | None:
        lo, hi = (self.v_low, self.v_high) if dim == "valence" else (self.ad_low, self.ad_high)
        if value < lo:
            return WORDS[dim][0]
        if value > hi:
            return WORDS[dim][1]
        return None


@dataclass
class Scores:
    arousal: float
    dominance: float
    valence: float
    infer_ms: float = 0.0
    window_s: float = 0.0
    t: float = 0.0

    def as_dict(self) -> dict:
        return {
            "arousal": round(self.arousal, 3),
            "dominance": round(self.dominance, 3),
            "valence": round(self.valence, 3),
            "infer_ms": round(self.infer_ms, 1),
            "window_s": round(self.window_s, 2),
        }


def hint_line(scores: Scores | None, thresholds: Thresholds) -> str | None:
    """The prompt line for these scores, or None when nothing is outside its deadzone."""
    if scores is None:
        return None
    labels = [
        lab
        for dim, value in (("arousal", scores.arousal), ("dominance", scores.dominance), ("valence", scores.valence))
        if (lab := thresholds.label(dim, value))
    ]
    if not labels:
        return None
    # Each label already contains "and", so the list uses commas: "A and B, and C and D".
    joined = labels[0] if len(labels) == 1 else ", ".join(labels[:-1]) + ", and " + labels[-1]
    return f"Voice: the user sounds {joined}. Match your tone and length to that."


# ------------------------------------------------------------------- scorer


class Wav2SmallScorer:
    """Runs the ONNX export. Input: float32 [1, samples] at 16 kHz. Output: [1, 3] A, D, V."""

    def __init__(self, model_path: Path, threads: int = 1):
        import onnxruntime as ort

        opts = ort.SessionOptions()
        opts.intra_op_num_threads = threads
        opts.inter_op_num_threads = 1
        self._sess = ort.InferenceSession(str(model_path), opts, providers=["CPUExecutionProvider"])
        self._input = self._sess.get_inputs()[0].name
        self.model_path = model_path

    def __call__(self, audio: np.ndarray) -> tuple[float, float, float]:
        x = np.asarray(audio, dtype=np.float32).reshape(1, -1)
        out = self._sess.run(None, {self._input: x})[0].reshape(-1)
        a, d, v = (float(np.clip(out[i], 0.0, 1.0)) for i in range(3))
        return a, d, v


# ---------------------------------------------------------------- processor


class EmotionProcessor(FrameProcessor):
    """Scores the user's voice while they speak; publishes the latest Scores via `latest`."""

    def __init__(
        self,
        scorer: Callable[[np.ndarray], tuple[float, float, float]],
        *,
        sample_rate: int = 16000,
        window_secs: float = 3.0,
        every_secs: float = 1.0,
        min_secs: float = 0.6,
        smoothing: float = 0.5,
        on_scores: Callable[[Scores], None] | None = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._scorer = scorer
        self._rate = sample_rate
        self._window = int(window_secs * sample_rate)
        self._every = every_secs
        self._min = int(min_secs * sample_rate)
        self._alpha = smoothing
        self._on_scores = on_scores
        self._ring: deque[np.ndarray] = deque()
        self._ring_len = 0
        self._speaking = False
        self._spoken = 0  # samples captured since the user started
        self._last_run = 0.0
        self._task: asyncio.Task | None = None
        self.latest: Scores | None = None  # raw scores of the current/last turn
        self.smoothed: Scores | None = None  # what the prompt uses
        self._prev_turn: Scores | None = None
        self.stats = {"windows": 0, "infer_ms_total": 0.0, "infer_ms_max": 0.0}

    # audio bookkeeping

    def _append(self, audio: bytes) -> None:
        chunk = np.frombuffer(audio, dtype=np.int16).astype(np.float32) / 32768.0
        self._ring.append(chunk)
        self._ring_len += len(chunk)
        while self._ring_len - len(self._ring[0]) >= self._window and len(self._ring) > 1:
            self._ring_len -= len(self._ring.popleft())
        if self._speaking:
            self._spoken += len(chunk)

    def _window_audio(self) -> np.ndarray:
        if not self._ring:
            return np.zeros(0, dtype=np.float32)
        buf = np.concatenate(list(self._ring))
        return buf[-self._window :]

    # scoring

    def _schedule(self, final: bool) -> None:
        if self._task and not self._task.done():
            return
        if self._spoken < self._min:
            return
        self._last_run = time.monotonic()
        self._task = asyncio.create_task(self._score(self._window_audio(), final))

    async def _score(self, audio: np.ndarray, final: bool) -> None:
        if len(audio) < self._min:
            return
        t0 = time.perf_counter()
        try:
            a, d, v = await asyncio.to_thread(self._scorer, audio)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"emotion: scoring failed: {e!r}")
            return
        ms = (time.perf_counter() - t0) * 1000
        self.stats["windows"] += 1
        self.stats["infer_ms_total"] += ms
        self.stats["infer_ms_max"] = max(self.stats["infer_ms_max"], ms)
        raw = Scores(a, d, v, infer_ms=ms, window_s=len(audio) / self._rate, t=time.time())
        self.latest = raw
        self.smoothed = self._smooth(raw)
        if self._on_scores:
            try:
                self._on_scores(self.smoothed)
            except Exception as e:  # noqa: BLE001
                logger.debug(f"emotion hook failed: {e!r}")
        logger.debug(
            f"voice A={a:.2f} D={d:.2f} V={v:.2f} (smoothed {self.smoothed.arousal:.2f}/"
            f"{self.smoothed.dominance:.2f}/{self.smoothed.valence:.2f}) {ms:.1f} ms{' final' if final else ''}"
        )

    def _smooth(self, raw: Scores) -> Scores:
        prev = self._prev_turn
        if prev is None or self._alpha <= 0:
            return Scores(raw.arousal, raw.dominance, raw.valence, raw.infer_ms, raw.window_s, raw.t)
        k = self._alpha
        return Scores(
            k * prev.arousal + (1 - k) * raw.arousal,
            k * prev.dominance + (1 - k) * raw.dominance,
            k * prev.valence + (1 - k) * raw.valence,
            raw.infer_ms,
            raw.window_s,
            raw.t,
        )

    def end_turn(self) -> Scores | None:
        """Called when the user's turn is over: the smoothed value becomes the memory for the next turn."""
        if self.latest is not None:
            self._prev_turn = self.smoothed
        done = self.smoothed
        self._spoken = 0
        return done

    # frames

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, InputAudioRawFrame):
            self._append(frame.audio)
            if self._speaking and time.monotonic() - self._last_run >= self._every:
                self._schedule(final=False)
        elif isinstance(frame, VADUserStartedSpeakingFrame):
            if not self._speaking:
                self._speaking = True
                self._spoken = min(self._ring_len, self._rate // 2)  # count the preroll as speech
                self._last_run = time.monotonic()
        elif isinstance(frame, VADUserStoppedSpeakingFrame):
            if self._speaking:
                self._speaking = False
                self._schedule(final=True)
        elif isinstance(frame, UserStoppedSpeakingFrame):
            # The turn is closed (Flux end of turn); fold this turn into the smoothing memory.
            self.end_turn()
        await self.push_frame(frame, direction)


class VoiceHintInjector(FrameProcessor):
    """Rewrites the system message with the current voice hint before each LLM run."""

    def __init__(
        self,
        base_system_prompt: str,
        scores: Callable[[], Scores | None],
        thresholds: Thresholds,
        *,
        on_hint: Callable[[str | None, Scores | None], None] | None = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._base = base_system_prompt
        self._scores = scores
        self._thr = thresholds
        self._on_hint = on_hint
        self.last_hint: str | None = None

    def set_base_prompt(self, text: str) -> None:
        self._base = text

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, LLMContextFrame):
            scores = self._scores()
            hint = hint_line(scores, self._thr)
            self.last_hint = hint
            messages = frame.context.messages
            content = self._base + (f"\n\n{hint}" if hint else "")
            if messages and messages[0].get("role") == "system":
                messages[0]["content"] = content
            else:
                messages.insert(0, {"role": "system", "content": content})
            if self._on_hint:
                try:
                    self._on_hint(hint, scores)
                except Exception as e:  # noqa: BLE001
                    logger.debug(f"hint hook failed: {e!r}")
        await self.push_frame(frame, direction)
