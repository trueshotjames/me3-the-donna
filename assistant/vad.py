"""Silero VAD that remembers what it measured.

Pipecat's analyzer only says speaking / not speaking. This subclass also keeps the
last ~60 s of per-frame (time, voice confidence, loudness), so each turn record can
show which of the two bars (confidence, loudness) the speech actually cleared.

In Pipecat 1.13 the transport no longer runs a VAD: ``vad_analyzer`` on the transport
params is silently ignored. The analyzer has to sit in a ``VADProcessor`` in the
pipeline, which main.py now does.
"""

from __future__ import annotations

import time
from collections import deque

import numpy as np

from pipecat.audio.vad.silero import SileroVADAnalyzer


def _scalar(x) -> float:
    """Silero returns a 1-element array; newer numpy refuses float() on it."""
    return float(np.asarray(x, dtype=np.float64).reshape(-1)[0])


class RecordingSileroVAD(SileroVADAnalyzer):
    def __init__(self, *args, history_secs: float = 60.0, **kwargs):
        super().__init__(*args, **kwargs)
        # Silero frames are 32 ms at 16 kHz (512 samples).
        self._history: deque[tuple[float, float, float]] = deque(maxlen=int(history_secs / 0.032) + 10)
        self._last_conf = 0.0

    def voice_confidence(self, buffer) -> float:
        conf = super().voice_confidence(buffer)
        self._last_conf = _scalar(conf)
        return conf

    def _get_smoothed_volume(self, audio: bytes) -> float:
        vol = super()._get_smoothed_volume(audio)
        # voice_confidence() runs just before this on the same frame (see VADAnalyzer._run_analyzer).
        self._history.append((time.time(), self._last_conf, _scalar(vol)))
        return vol

    def frames_between(self, t0: float, t1: float) -> list[tuple[float, float, float]]:
        return [f for f in list(self._history) if t0 <= f[0] <= t1]
