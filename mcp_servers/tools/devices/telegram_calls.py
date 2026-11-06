"""Real Telegram calls from this machine (TELEGRAM_CALLS=direct, the default off Windows).

py-tgcalls places the call on the Telethon session Donna already has, and the audio runs through this machine's
sound devices, so on the Pi the user talks on the Jabra with no phone or Telegram app. Works on Windows too.
Everything async here runs on the Telethon loop (telegram._Loop).

Optional .env: TELEGRAM_CALL_MIC, TELEGRAM_CALL_SPEAKER = part of a device name (default: Jabra, else the first).

py-tgcalls plays no call sounds of its own, so CallSounds plays Donna's own tones (assistant/cues.py) on the default
output: ringing, connected, ended, busy. On the Pi that is the Jabra, at the master volume.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import tempfile
import threading
import wave
from pathlib import Path
from typing import Any, Callable

try:
    from loguru import logger
except ImportError:  # pragma: no cover
    import logging

    logger = logging.getLogger(__name__)  # type: ignore[assignment]

from mcp_servers.tools.devices.base import DeviceError

RING_SECS = 45
ENDED = {"CallDeclined": "declined", "CallBusy": "busy", "TimedOutAnswer": "no answer", "CallDiscarded": "hung up"}


RING_GAP_SECS = {"call_ring": 3.0, "call_incoming": 1.5}
BUSY_REASONS = ("declined", "busy")
_SOUND_DIR = Path(tempfile.gettempdir()) / "donna-call-sounds"


def _sound_file(kind: str) -> Path:
    from assistant.cues import tone

    path = _SOUND_DIR / f"{kind}.wav"
    if not path.exists():
        _SOUND_DIR.mkdir(parents=True, exist_ok=True)
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(48000)
            w.writeframes(tone(kind, 48000, 0.3))
    return path


def play_sound(kind: str) -> subprocess.Popen | None:
    """Start a tone; the returned process can be stopped early. Never raises: a sound must not break a call."""
    try:
        path = str(_sound_file(kind))
        if os.name == "nt":
            import winsound

            winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC)
            return None
        player = shutil.which("pw-play") or shutil.which("aplay")
        return subprocess.Popen([player, path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) if player else None
    except Exception as e:  # noqa: BLE001
        logger.debug(f"phone: call sound failed: {e!r}")
        return None


class CallSounds:
    """One call's sounds. Each step is safe to reach twice, since Telegram and py-tgcalls both report the same events."""

    def __init__(self, player: Callable[[str], Any] = play_sound):
        self._player = player
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._proc: Any = None
        self._ringing = False
        self.active = False

    def _halt(self) -> None:
        self._stop.set()
        self._stop = threading.Event()
        if self._proc is not None:
            try:
                self._proc.terminate()
            except Exception:  # noqa: BLE001
                pass
            self._proc = None

    def _loop(self, kind: str, stop: threading.Event) -> None:
        while not stop.is_set():
            with self._lock:
                if stop.is_set():
                    return
                self._proc = self._player(kind)
            stop.wait(sum(s for _f, s in _cue(kind)) + RING_GAP_SECS[kind])

    def ringing(self, kind: str = "call_ring") -> None:
        with self._lock:
            self._halt()
            self.active, self._ringing = True, True
            stop = self._stop
        threading.Thread(target=self._loop, args=(kind, stop), name="call-ring", daemon=True).start()

    def connected(self) -> None:
        with self._lock:
            if not (self.active and self._ringing):
                return
            self._ringing = False
            self._halt()
            self._proc = self._player("call_connected")

    def finish(self, kind: str | None = "call_end") -> None:
        """Stop ringing and play the closing sound once; kind None stops silently."""
        with self._lock:
            if not self.active:
                return
            self.active, self._ringing = False, False
            self._halt()
            if kind:
                self._proc = self._player(kind)


def _cue(kind: str) -> list[tuple[float, float]]:
    from assistant.cues import CUES

    return CUES[kind]


def pick_device(devices: list[Any], wanted: str | None) -> Any | None:
    """The device whose name contains `wanted` (default: Jabra, else the first). Never a "Monitor of ..." source."""
    real = [d for d in devices if not d.title.lower().startswith("monitor of")]
    if wanted and wanted.strip():
        return next((d for d in real if wanted.strip().lower() in d.title.lower()), None)
    return next((d for d in real if "jabra" in d.title.lower()), real[0] if real else None)


def end_reason(e: Exception) -> str:
    return ENDED.get(type(e).__name__, f"failed: {e}")


class DirectCaller:
    """One call at a time. on_end(reason) runs on the loop when a call fails or nobody answers."""

    def __init__(self, client: Any, on_end: Callable[[str], None], on_incoming: Callable[[int | None], None],
                 on_connected: Callable[[], None]):
        from pytgcalls import PyTgCalls  # here, on the loop: its constructor takes the running event loop

        self._tg = PyTgCalls(client)
        self._on_end = on_end
        self._on_incoming = on_incoming
        self._on_connected = on_connected
        self._task: asyncio.Task | None = None
        self._peer: int | None = None
        self.incoming: int | None = None

    async def start(self) -> None:
        from pytgcalls.types import ChatUpdate

        status = ChatUpdate.Status

        async def on_update(_client: Any, update: Any) -> None:
            if not isinstance(update, ChatUpdate):
                return
            if update.status & status.INCOMING_CALL:
                self.incoming = update.chat_id
                self._on_incoming(update.chat_id)
            elif update.status & status.DISCARDED_CALL and update.chat_id == self.incoming:
                self.incoming = None
                self._on_incoming(None)

        self._tg.add_handler(on_update)
        await self._tg.start()

    @staticmethod
    def _streams() -> tuple[Any, Any]:
        """(what we send, what we play), from the devices plugged in right now."""
        from pytgcalls import MediaDevices
        from pytgcalls.types import MediaStream, RecordStream
        from pytgcalls.types.raw import AudioParameters

        mic = pick_device(list(MediaDevices.microphone_devices()), os.getenv("TELEGRAM_CALL_MIC"))
        speaker = pick_device(list(MediaDevices.speaker_devices()), os.getenv("TELEGRAM_CALL_SPEAKER"))
        if mic is None or speaker is None:
            raise DeviceError(f"I can't find a {'microphone' if mic is None else 'speaker'} for the call")
        logger.info(f"phone: call audio: mic {mic.title!r}, speaker {speaker.title!r}")
        return (
            MediaStream(mic, audio_parameters=AudioParameters(48000, 1), video_flags=MediaStream.Flags.IGNORE),
            RecordStream(speaker, audio_parameters=AudioParameters(48000, 2)),
        )

    async def _run(self, peer: int, send: Any, play: Any) -> None:
        from pytgcalls.types import CallConfig

        try:
            await self._tg.play(peer, send, CallConfig(timeout=RING_SECS))  # returns once the call is connected
            self._on_connected()
            await self._tg.record(peer, play)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning(f"phone: call ended: {e!r}")
            self._peer, self._task = None, None
            self._on_end(end_reason(e))

    async def dial(self, peer: int) -> None:
        """Start ringing `peer` and return at once; ringing can take RING_SECS, tools cannot wait that long."""
        send, play = self._streams()
        self._peer = peer
        self._task = asyncio.get_running_loop().create_task(self._run(peer, send, play))

    async def answer(self) -> int | None:
        """Pick up the incoming call, if there is one; returns who is calling."""
        peer, self.incoming = self.incoming, None
        if peer is not None:
            await self.dial(peer)
        return peer

    async def hang_up(self) -> bool:
        """True when this ended the call. play() holds a per-chat lock while ringing, so stop it before leaving."""
        if self._peer is None:
            return False
        if self._task is not None:
            self._task.cancel()
        peer, self._peer, self._task = self._peer, None, None
        try:
            await self._tg.leave_call(peer)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"phone: leaving the call failed: {e!r}")
            return False
        return True
