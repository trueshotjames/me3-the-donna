"""Music-aware listening (MUSIC_DUCK): what Donna does while Spotify (or the sim player) is playing.

King's first Spotify run (2026-11-05): music through the Jabra leaked into the mic at -24 to -28 dBFS (his voice
-16). The echo canceller removes most of it, not all; the leftover lyrics opened turns, garbled his words and one
("Saint Clair", -24 dB) cut her off. Two remedies, both driven from here:

1. Duck: the moment a NEAR voice starts (louder than the music-aware gate threshold) or she starts speaking, the
   music drops to MUSIC_DUCK_LEVEL percent through the tools server's internal `duck_music` tool; it comes back
   MUSIC_RESTORE_SECS after the conversation goes idle. Smart speakers do the same.
2. A stricter gate while music plays: assistant/voice_gate.py asks `MUSIC.playing` and uses the calibrated
   music threshold (assistant/calibrate.py) instead of the usual 10 dB gap.

State comes from the house snapshot (scheduler.home_message) and from control_music results. Everything here
runs on the pipeline's event loop; the only I/O is one local MCP call per duck or restore.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable

from loguru import logger


class MusicState:
    def __init__(self) -> None:
        self.enabled = True
        self.playing = False
        self.ducked = False
        self.level = 15
        self.restore_secs = 2.5
        self._call: Callable[[bool, int], Awaitable[Any]] | None = None
        self._state = "idle"  # the UI state: idle | listening | thinking | speaking
        self._idle_since = time.time()
        self._task: asyncio.Task | None = None
        self._busy = False
        self.stats = {"ducks": 0, "restores": 0, "errors": 0}

    def attach(self, call: Callable[[bool, int], Awaitable[Any]], *, level: int = 15, restore_secs: float = 2.5,
               enabled: bool = True) -> None:
        self._call, self.level, self.restore_secs, self.enabled = call, int(level), float(restore_secs), enabled

    # ------------------------------------------------------------- inputs
    def update_media(self, media: dict[str, Any] | None) -> None:
        """A media status (home snapshot or a control_music result)."""
        if not isinstance(media, dict) or "state" not in media:
            return
        playing = media.get("state") == "playing"
        if playing != self.playing:
            logger.info(f"music: {'playing' if playing else 'not playing'}")
        self.playing = playing
        if "ducked" in media:
            self.ducked = bool(media["ducked"])
        # Stopped while ducked ("stop the music" always is): stay ducked, so run() still restores the volume.

    def on_state(self, state: str) -> None:
        """The assistant's UI state; she speaking also ducks, and idle starts the restore countdown."""
        if state == self._state:
            return
        self._state = state
        if state == "idle":
            self._idle_since = time.time()
        elif state == "speaking":
            self.near_voice("she speaks")

    def near_voice(self, why: str = "near voice") -> None:
        """Someone close is talking to her (or she talks): duck the music now."""
        if not (self.enabled and self.playing and not self.ducked and self._call is not None):
            return
        self._idle_since = time.time()
        self._spawn(True, why)

    # ------------------------------------------------------------ actions
    def _spawn(self, on: bool, why: str) -> None:
        if self._busy:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._busy = True
        loop.create_task(self._set(on, why), name="music-duck")

    async def _set(self, on: bool, why: str) -> None:
        try:
            t0 = time.time()
            await self._call(on, self.level)
            self.ducked = on
            self.stats["ducks" if on else "restores"] += 1
            logger.info(f"music: {'ducked to ' + str(self.level) + '%' if on else 'restored'} ({why}, {round((time.time() - t0) * 1000)} ms)")
        except Exception as e:  # noqa: BLE001
            self.stats["errors"] += 1
            logger.warning(f"music: duck {'on' if on else 'off'} failed: {e!r}")
        finally:
            self._busy = False

    async def run(self) -> None:
        """Restore the music once the conversation has been idle for restore_secs."""
        while True:
            await asyncio.sleep(0.25)
            if self.ducked and self._state == "idle" and time.time() - self._idle_since >= self.restore_secs:
                self._spawn(False, f"idle {self.restore_secs:.1f} s")

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self.run(), name="music-restore")


MUSIC = MusicState()
