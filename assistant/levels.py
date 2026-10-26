"""Mic and speaker levels for the UI meter, in dBFS, from frames the pipeline already carries.

An observer, not a processor, so the pipeline order is unchanged. The mic level comes from the
InputAudioRawFrames the input transport pushes. The speaker level comes from the chunks the
output transport pushes after writing them to the sound card, so the meter follows what is
audible, not the faster-than-real-time TTS bursts. A small loop sends one ``levels`` event
every 100 ms, and only while a UI is connected (one RMS over ~20 ms of int16 per frame).
"""

from __future__ import annotations

import asyncio
import math

import numpy as np
from pipecat.frames.frames import InputAudioRawFrame, OutputAudioRawFrame
from pipecat.observers.base_observer import BaseObserver, FramePushed
from pipecat.transports.base_output import BaseOutputTransport

from assistant.ui_bus import UIBus

FLOOR_DB = -90.0
_FULL_SCALE_DB = 20 * math.log10(32768.0)


def dbfs(audio: bytes) -> tuple[float, float]:
    """(RMS, peak) of 16-bit PCM in dBFS; 0 dBFS is full scale, silence is FLOOR_DB."""
    n = len(audio) // 2
    if n == 0:
        return FLOOR_DB, FLOOR_DB
    x = np.frombuffer(audio, dtype=np.int16, count=n).astype(np.float32)
    ms = float(np.dot(x, x)) / n
    peak = float(np.max(np.abs(x)))
    rms_db = 10 * math.log10(ms) - _FULL_SCALE_DB if ms > 0 else FLOOR_DB
    peak_db = 20 * math.log10(peak) - _FULL_SCALE_DB if peak > 0 else FLOOR_DB
    return max(FLOOR_DB, rms_db), max(FLOOR_DB, peak_db)


class LevelMeter(BaseObserver):
    """Keeps the loudest frame since the last tick; ``run()`` sends it as a ``levels`` event."""

    def __init__(self, bus: UIBus, *, every_secs: float = 0.1, **kwargs):
        super().__init__(observe_every_push=False, **kwargs)
        self._bus = bus
        self._every = every_secs
        self._mic: float | None = None
        self._mic_peak: float | None = None
        self._out: float | None = None
        self._task: asyncio.Task | None = None

    async def on_push_frame(self, data: FramePushed):
        frame = data.frame
        if isinstance(frame, InputAudioRawFrame):
            if not self._bus.clients:
                return
            rms, peak = dbfs(frame.audio)
            self._mic = rms if self._mic is None else max(self._mic, rms)
            self._mic_peak = peak if self._mic_peak is None else max(self._mic_peak, peak)
        elif isinstance(frame, OutputAudioRawFrame) and isinstance(data.source, BaseOutputTransport):
            if not self._bus.clients:
                return
            rms, _ = dbfs(frame.audio)
            self._out = rms if self._out is None else max(self._out, rms)

    def take(self) -> dict:
        """The levels since the last call (FLOOR_DB when no frame arrived), then reset."""
        msg = {
            "mic_db": round(self._mic if self._mic is not None else FLOOR_DB, 1),
            "mic_peak_db": round(self._mic_peak if self._mic_peak is not None else FLOOR_DB, 1),
            "out_db": round(self._out if self._out is not None else FLOOR_DB, 1),
        }
        self._mic = self._mic_peak = self._out = None
        return msg

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._every)
            if self._bus.clients:
                self._bus.emit("levels", **self.take())
