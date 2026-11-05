"""Sound cues: Donna says whether she works without anything to look at (headless on the Pi).

Four cues, each a short tone pattern (no cloud call, so they play with the internet down):
  starting  one soft mid beep              the supervisor started (Pi booted, or `donna start`)
  live      two rising notes               she is connected and listening
  down      two falling notes, low         she stopped working (no internet, Deepgram or the LLM down,
                                           or she crashed); repeats every CUE_DOWN_REPEAT_SECS
  back      three rising notes             she works again after a "down"

While assistant.main runs it owns the speaker, so it plays cues through the pipeline (CuePlayer,
just before the output transport). When it is not running, the supervisor plays them directly
with PyAudio (play_blocking).

The current state also goes to data/donna_status.json, which `donna status` (tools/donna.ps1) reads.
"""

from __future__ import annotations

import json
import math
import os
import struct
import time
from pathlib import Path
from typing import Any

from loguru import logger

CUES: dict[str, list[tuple[float, float]]] = {
    # (frequency Hz, seconds); frequency 0 = silence
    "starting": [(660, 0.12)],
    "live": [(523, 0.12), (0, 0.04), (784, 0.18)],
    "down": [(587, 0.18), (0, 0.05), (392, 0.30)],
    "back": [(523, 0.10), (0, 0.03), (659, 0.10), (0, 0.03), (784, 0.20)],
}

STATUS_FILE_NAME = "donna_status.json"


def tone(kind: str, sample_rate: int = 16000, volume: float = 0.3) -> bytes:
    """16-bit mono PCM of cue ``kind``, with 10 ms fades so the notes don't click."""
    out = bytearray()
    amp = max(0.0, min(1.0, volume)) * 32767
    fade = int(0.01 * sample_rate)
    for freq, secs in CUES[kind]:
        n = int(secs * sample_rate)
        for i in range(n):
            if freq <= 0:
                v = 0.0
            else:
                env = min(1.0, i / fade, (n - 1 - i) / fade) if fade else 1.0
                v = amp * env * math.sin(2 * math.pi * freq * i / sample_rate)
            out += struct.pack("<h", int(v))
    out += b"\x00\x00" * int(0.05 * sample_rate)  # short tail so the last note is not cut
    return bytes(out)


def write_status(data_dir: Path, state: str, detail: str = "", **extra: Any) -> None:
    """data/donna_status.json: {state, detail, since, pid}. Best effort (never raises)."""
    p = data_dir / STATUS_FILE_NAME
    try:
        prev = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except (OSError, ValueError):
        prev = {}
    since = prev.get("since") if prev.get("state") == state else time.time()
    rec = {"state": state, "detail": detail, "since": since, "updated": time.time(), "pid": os.getpid(), **extra}
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(rec), encoding="utf-8")
        os.replace(tmp, p)
    except OSError as e:
        logger.debug(f"status file not written: {e}")


def play_blocking(kind: str, *, device: int | None, sample_rate: int, volume: float) -> bool:
    """Play a cue straight to the sound card (only when assistant.main is not holding it)."""
    try:
        import pyaudio
    except ImportError:
        return False
    pa = None
    try:
        pa = pyaudio.PyAudio()
        stream = pa.open(format=pyaudio.paInt16, channels=1, rate=sample_rate, output=True,
                         output_device_index=device, frames_per_buffer=int(sample_rate * 0.05))
        stream.write(tone(kind, sample_rate, volume))
        stream.stop_stream()
        stream.close()
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning(f"cue '{kind}' not played: {e!r}")
        return False
    finally:
        if pa is not None:
            pa.terminate()


def make_cue_player(sample_rate: int = 16000, volume: float = 0.3):
    """A pass-through FrameProcessor that can push a cue to the output transport (place it just before it)."""
    from pipecat.frames.frames import Frame, OutputAudioRawFrame
    from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

    class CuePlayer(FrameProcessor):
        def __init__(self):
            super().__init__(name="CuePlayer")
            self._started = False
            self._pending: list[str] = []

        async def process_frame(self, frame: Frame, direction: FrameDirection):
            await super().process_frame(frame, direction)
            from pipecat.frames.frames import StartFrame

            await self.push_frame(frame, direction)
            if isinstance(frame, StartFrame):
                # the ASR connects while StartFrame is still upstream: its "live" cue waits here
                self._started = True
                pending, self._pending = self._pending, []
                for kind in pending:
                    await self.play(kind)

        async def play(self, kind: str) -> None:
            if not self._started:
                self._pending.append(kind)
                return
            audio = tone(kind, sample_rate, volume)
            await self.push_frame(OutputAudioRawFrame(audio=audio, sample_rate=sample_rate, num_channels=1))

    return CuePlayer()


class CueState:
    """Live / down / back bookkeeping inside assistant.main, fed by the ASR connection and the net monitor.

    ``down`` needs ``down_after`` failed checks in a row (one lost probe is not an outage). While down,
    the down cue repeats every ``repeat_secs`` (0 = once).
    """

    def __init__(self, play, data_dir: Path, *, recovered: bool = False, down_after: int = 2, repeat_secs: float = 60.0):
        self._play = play
        self._data_dir = data_dir
        self._recovered = recovered  # the supervisor restarted her after a "down": first live is a "back"
        self._down_after = down_after
        self._repeat = repeat_secs
        self.live = False
        self.down = False
        self._fails = 0
        self._last_down_cue = 0.0
        self.reason = ""

    async def on_live(self) -> None:
        if self.live:
            return
        self.live = True
        kind = "back" if self._recovered else "live"
        logger.info(f"cue: {kind} (connected and listening)")
        write_status(self._data_dir, "live", "connected and listening")
        await self._play(kind)

    async def on_net(self, status: dict[str, Any]) -> None:
        rows = {r["name"]: r for r in status.get("targets", [])}
        bad = [r for r in rows.values() if r.get("state") == "down"]
        now = time.monotonic()
        if bad:
            self._fails += 1
            if self._fails < self._down_after:
                return
            reason = ", ".join(f"{r['host']} unreachable ({r.get('error')})" for r in bad)
            if not self.down:
                self.down = True
                self.reason = reason
                logger.warning(f"cue: down ({reason})")
                write_status(self._data_dir, "down", reason)
                self._last_down_cue = now
                await self._play("down")
            elif self._repeat and now - self._last_down_cue >= self._repeat:
                self._last_down_cue = now
                await self._play("down")
            return
        self._fails = 0
        if self.down:
            self.down = False
            logger.info("cue: back (network and providers reachable again)")
            write_status(self._data_dir, "live", "back after: " + self.reason)
            await self._play("back")
