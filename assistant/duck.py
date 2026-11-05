"""v7 (DUCK_BARGE_IN): duck-and-decide barge-in.

v6's floor control let her talk on at full volume while it waited for Flux's words (barge-in stop ~250 ms, fdbench
t_stop 0.75 s), and "Okay" still cut her off. Here her audio pauses the moment Silero hears the user over her, and
the decision comes after, with her silent:

    user speech onset (DUCK_FRAMES voiced Silero frames, near voice)  -> pause her audio (this module)
    Flux words: real content, or still talking after DUCK_DECIDE_SECS -> interrupt (Pipecat interruption)
    Flux: backchannel only ("okay", "mm-hmm"), a far voice, or no turn -> resume where she paused

The pause sits in the output transport's write_audio_frame: the frame being written waits, so nothing she has not
said is lost and resuming continues mid-word. A frame that waited through an interruption is dropped (this Pipecat
keeps the frame being written across an interruption). With DUCK_PACE the transport is also held to sound-card pace
(~40 ms ahead), as a Pi with ALSA is; fd-benchmark's websocket transport otherwise sends twice as fast as real time,
so a pause would not stop audio it had already sent.
"""

from __future__ import annotations

import asyncio
import time

from loguru import logger


class Duck:
    def __init__(self, *, pace: bool = True, max_secs: float = 2.5, lead_secs: float = 0.04):
        self._go = asyncio.Event()
        self._go.set()
        self._pace = pace
        self._max = max_secs
        self._lead = lead_secs
        self._t_next = 0.0
        self._gen = 0  # interruptions seen; a frame that waited through one is dropped
        self._watchdog: asyncio.Task | None = None
        self.paused_at: float | None = None
        self.stats = {"paused": 0, "resumed": 0, "interrupted": 0, "watchdog": 0, "dropped_frames": 0, "paused_ms": []}

    @property
    def paused(self) -> bool:
        return not self._go.is_set()

    def install(self, output) -> None:
        """Wrap a Pipecat BaseOutputTransport instance (local audio, fdbench websocket, the silent null speaker)."""
        from pipecat.frames.frames import InterruptionFrame

        write = output.write_audio_frame
        process = output.process_frame

        async def write_audio_frame(frame):
            if not self._go.is_set():
                gen = self._gen
                await self._go.wait()
                if gen != self._gen:
                    self.stats["dropped_frames"] += 1
                    return True  # she was interrupted while this frame waited: never play it
            ok = await write(frame)
            if self._pace:
                await self._pace_sleep(frame)
            return ok

        async def process_frame(frame, direction):
            await process(frame, direction)
            if isinstance(frame, InterruptionFrame):
                self._gen += 1
                self.resume("interrupted")

        output.write_audio_frame = write_audio_frame
        output.process_frame = process_frame

    async def _pace_sleep(self, frame) -> None:
        dur = len(frame.audio) / 2 / max(1, frame.num_channels) / max(1, frame.sample_rate)
        now = time.perf_counter()
        self._t_next = max(self._t_next, now) + dur
        wait = self._t_next - now - self._lead
        if wait > 0:
            await asyncio.sleep(wait)

    def pause(self, why: str) -> bool:
        if self.paused:
            return False
        self._go.clear()
        self.paused_at = time.time()
        self.stats["paused"] += 1
        logger.info(f"duck: her audio paused ({why})")
        if self._watchdog:
            self._watchdog.cancel()
        self._watchdog = asyncio.get_running_loop().create_task(self._guard())
        return True

    def resume(self, why: str) -> None:
        if not self.paused:
            return
        ms = (time.time() - (self.paused_at or time.time())) * 1000
        self.stats["paused_ms"].append(round(ms))
        self.stats["interrupted" if why == "interrupted" else "resumed"] += 1
        self._go.set()
        self.paused_at = None
        if self._watchdog and self._watchdog is not asyncio.current_task():
            self._watchdog.cancel()
        self._watchdog = None
        logger.info(f"duck: her audio {'stopped for good' if why == 'interrupted' else 'resumed'} after {ms:.0f} ms ({why})")

    async def _guard(self) -> None:
        """Never stay paused longer than DUCK_MAX_SECS + 1 s, whatever the decision logic does."""
        try:
            await asyncio.sleep(self._max + 1.0)
            if self.paused:
                self.stats["watchdog"] += 1
                self.resume("watchdog")
        except asyncio.CancelledError:
            pass
