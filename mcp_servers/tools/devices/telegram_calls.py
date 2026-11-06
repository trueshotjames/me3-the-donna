"""Real Telegram calls from this machine (TELEGRAM_CALLS=direct, the default off Windows).

py-tgcalls places the call on the Telethon session Donna already has, and the audio runs through this machine's
sound devices, so on the Pi the user talks on the Jabra with no phone or Telegram app. Works on Windows too.
Everything async here runs on the Telethon loop (telegram._Loop).

Optional .env: TELEGRAM_CALL_MIC, TELEGRAM_CALL_SPEAKER = part of a device name (default: Jabra, else the first).
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, Callable

try:
    from loguru import logger
except ImportError:  # pragma: no cover
    import logging

    logger = logging.getLogger(__name__)  # type: ignore[assignment]

from mcp_servers.tools.devices.base import DeviceError

RING_SECS = 45
ENDED = {"CallDeclined": "declined", "CallBusy": "busy", "TimedOutAnswer": "no answer", "CallDiscarded": "hung up"}


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

    def __init__(self, client: Any, on_end: Callable[[str], None], on_incoming: Callable[[int | None], None]):
        from pytgcalls import PyTgCalls  # here, on the loop: its constructor takes the running event loop

        self._tg = PyTgCalls(client)
        self._on_end = on_end
        self._on_incoming = on_incoming
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
