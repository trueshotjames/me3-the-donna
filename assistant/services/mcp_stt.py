"""Pipecat STT service that streams audio through our ASR MCP server.

Pipecat's MCP client only wires tools to the LLM, so speech goes through this
custom STTService: every ~60 ms of microphone audio is pushed to the ASR MCP
server with `asr_push`, and a background task long-polls `asr_events` and turns
the server's Flux events into Pipecat frames exactly as Pipecat's own Deepgram
Flux service would:

    StartOfTurn    -> ProposedUserStartedSpeakingFrame (barge-in when the bot talks)
    Update         -> InterimTranscriptionFrame
    EagerEndOfTurn -> EagerTranscriptionFrame (LLM starts early, answer is held)
    TurnResumed    -> EagerEndOfTurnCancelFrame
    EndOfTurn      -> TranscriptionFrame(finalized=True) + ProposedUserStoppedSpeakingFrame
"""

from __future__ import annotations

import asyncio
import base64
from typing import AsyncGenerator

from loguru import logger
from mcp import StdioServerParameters
from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    ErrorFrame,
    Frame,
    InterimTranscriptionFrame,
    ProposedUserStartedSpeakingFrame,
    ProposedUserStoppedSpeakingFrame,
    StartFrame,
    STTMetadataFrame,
    TranscriptionFrame,
)
from pipecat.processors.frame_processor import FrameProcessorSetup
from pipecat.services.settings import STTSettings
from pipecat.services.stt_service import STTService
from pipecat.turns.eager_end_of_turn_mixin import EagerEndOfTurnSTTServiceMixin
from pipecat.turns.user_start import (
    ExternalUserTurnStartStrategy,
    TranscriptionUserTurnStartStrategy,
    VADUserTurnStartStrategy,
)
from pipecat.turns.user_stop import EagerUserTurnStopStrategy, ExternalUserTurnStopStrategy
from pipecat.turns.user_turn_strategies import UserTurnStrategies
from pipecat.utils.time import time_now_iso8601

from assistant.mcp_bridge import MCPBridge


class MCPSTTService(EagerEndOfTurnSTTServiceMixin, STTService):
    """Streaming STT over our ASR MCP server (which wraps Deepgram Flux)."""

    def __init__(
        self,
        *,
        server_params: StdioServerParameters,
        sample_rate: int = 16000,
        push_interval_ms: int = 60,
        eot_threshold: float = 0.7,
        eager_eot_threshold: float = 0.5,
        eot_timeout_ms: int = 5000,
        enable_eager_end_of_turn: bool = True,
        turn_start: str = "vad",
        should_interrupt: bool = True,
        **kwargs,
    ):
        super().__init__(
            sample_rate=sample_rate,
            enable_eager_end_of_turn=enable_eager_end_of_turn,
            settings=STTSettings(model="deepgram-flux-via-mcp", language=None),
            **kwargs,
        )
        self._server_params = server_params
        self._push_bytes = max(1, sample_rate * 2 * push_interval_ms // 1000)
        self._eot_threshold = eot_threshold
        self._eager_eot_threshold = eager_eot_threshold if enable_eager_end_of_turn else None
        self._eot_timeout_ms = eot_timeout_ms
        self._turn_start = turn_start
        self._should_interrupt = should_interrupt

        self._bridge = MCPBridge("asr", stdio=server_params)
        self._session_id: str | None = None
        self._buffer = bytearray()
        self._send_queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=64)
        self._sender: asyncio.Task | None = None
        self._poller: asyncio.Task | None = None
        self._user_is_speaking = False
        self._stats = {"pushes": 0, "push_ms_total": 0.0, "events": 0}

    # ----------------------------------------------------------------- metadata

    def can_generate_metrics(self) -> bool:
        return True

    @property
    def supports_ttfs(self) -> bool:
        return False  # the server (Flux) decides the turn boundary

    def service_metadata_frame(self) -> STTMetadataFrame:
        frame = super().service_metadata_frame()
        frame.user_turn_strategies = self._strategies()
        return frame

    def _strategies(self) -> UserTurnStrategies:
        """Turn strategies.

        'asr': Flux's StartOfTurn starts the turn (one cloud round trip later).
        'vad': local Silero VAD starts the turn, so barge-in is a local reflex
               (~150-250 ms). Flux's StartOfTurn and the transcript itself are
               fallbacks, so a quiet mic that never trips the VAD still gets a
               turn. Flux decides when the turn ends either way.
        """
        if self._turn_start == "asr":
            return self.recommended_user_turn_strategies(enable_interruptions=self._should_interrupt)
        stop = (
            EagerUserTurnStopStrategy(match_policy=self._eager_match_policy)
            if self._enable_eager_end_of_turn
            else ExternalUserTurnStopStrategy()
        )
        return UserTurnStrategies(
            start=[
                VADUserTurnStartStrategy(enable_interruptions=self._should_interrupt),
                ExternalUserTurnStartStrategy(enable_interruptions=self._should_interrupt),
                TranscriptionUserTurnStartStrategy(),
            ],
            stop=[stop],
        )

    # ---------------------------------------------------------------- lifecycle

    async def setup(self, setup: FrameProcessorSetup):
        await super().setup(setup)
        await self._connect()

    async def stop(self, frame: EndFrame):
        await super().stop(frame)
        await self._disconnect()

    async def cancel(self, frame: CancelFrame):
        await super().cancel(frame)
        await self._disconnect()

    async def cleanup(self):
        await super().cleanup()
        await self._disconnect()

    async def _connect(self):
        if self._session_id:
            return
        try:
            await self._bridge.start()
            info = await self._bridge.call_json(
                "asr_start",
                {
                    "sample_rate": self.sample_rate or 16000,
                    "eot_threshold": self._eot_threshold,
                    "eager_eot_threshold": self._eager_eot_threshold,
                    "eot_timeout_ms": self._eot_timeout_ms,
                },
            )
            self._session_id = info["session"]
            self._sender = asyncio.create_task(self._sender_loop(), name="asr-sender")
            self._poller = asyncio.create_task(self._poll_loop(), name="asr-poller")
            logger.info(f"{self}: ASR session {self._session_id} ({info.get('model')})")
            await self._call_event_handler("on_connected")
        except Exception as e:  # noqa: BLE001
            logger.error(f"{self}: could not start ASR MCP server: {e!r}")
            await self._call_event_handler("on_connection_error", f"{e}")
            await self.push_error(error_msg=f"ASR MCP server failed to start: {e}", exception=e)

    async def _disconnect(self):
        for task in (self._sender, self._poller):
            if task:
                task.cancel()
        self._sender = self._poller = None
        if self._session_id and self._bridge.connected:
            try:
                await self._bridge.call("asr_stop", {"session": self._session_id}, timeout=5)
            except Exception:  # noqa: BLE001
                pass
        self._session_id = None
        await self._bridge.close()
        await self._call_event_handler("on_disconnected")

    # -------------------------------------------------------------------- audio

    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame | None, None]:
        """Buffer microphone audio and hand it to the sender task in ~60 ms blocks."""
        if not self._session_id:
            yield None
            return
        self._buffer.extend(audio)
        while len(self._buffer) >= self._push_bytes:
            chunk = bytes(self._buffer[: self._push_bytes])
            del self._buffer[: self._push_bytes]
            try:
                self._send_queue.put_nowait(chunk)
            except asyncio.QueueFull:
                # Never block the audio path; drop the oldest block instead.
                self._send_queue.get_nowait()
                self._send_queue.put_nowait(chunk)
        yield None

    async def _sender_loop(self):
        loop = asyncio.get_running_loop()
        while True:
            chunk = await self._send_queue.get()
            if chunk is None:
                return
            t0 = loop.time()
            try:
                await self._bridge.call(
                    "asr_push",
                    {"session": self._session_id, "pcm_base64": base64.b64encode(chunk).decode("ascii")},
                    timeout=5,
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(f"{self}: asr_push failed: {e}")
                await asyncio.sleep(0.2)
                continue
            self._stats["pushes"] += 1
            self._stats["push_ms_total"] += (loop.time() - t0) * 1000

    # ------------------------------------------------------------------- events

    async def _poll_loop(self):
        while True:
            try:
                data = await self._bridge.call_json(
                    "asr_events", {"session": self._session_id, "wait_ms": 2000}, timeout=10
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.warning(f"{self}: asr_events failed: {e}")
                await asyncio.sleep(0.5)
                continue
            for event in (data or {}).get("events", []):
                self._stats["events"] += 1
                try:
                    await self._handle_event(event)
                except Exception as e:  # noqa: BLE001
                    logger.error(f"{self}: error handling ASR event {event}: {e!r}")

    async def _handle_event(self, event: dict):
        kind = event.get("event")
        transcript = (event.get("transcript") or "").strip()
        if kind == "StartOfTurn":
            logger.info(f"{self}: Flux start of turn")
            self._user_is_speaking = True
            await self.broadcast_frame(ProposedUserStartedSpeakingFrame)
            await self._call_event_handler("on_start_of_turn", transcript)
        elif kind == "Update":
            if transcript:
                await self.push_frame(
                    InterimTranscriptionFrame(transcript, self._user_id, time_now_iso8601(), None, result=event)
                )
        elif kind == "EagerEndOfTurn":
            await self._push_eager_end_of_turn(transcript, user_id=self._user_id, result=event)
        elif kind == "TurnResumed":
            await self._cancel_eager_end_of_turn()
        elif kind == "EndOfTurn":
            logger.info(f"{self}: Flux end of turn: [{transcript}]")
            self._user_is_speaking = False
            self._clear_eager_end_of_turn()
            if transcript:
                await self.push_frame(
                    TranscriptionFrame(
                        transcript, self._user_id, time_now_iso8601(), None, result=event, finalized=True
                    )
                )
            await self.broadcast_frame(ProposedUserStoppedSpeakingFrame)
            await self._call_event_handler("on_end_of_turn", transcript)
        elif kind == "Error":
            await self.push_error(error_msg=f"ASR server error: {event.get('message')}")
        elif kind in ("Connected", "Reconnected"):
            logger.info(f"{self}: ASR server {kind.lower()}")
        else:
            logger.debug(f"{self}: unhandled ASR event {kind}")

    # ----------------------------------------------------------------- helpers

    @property
    def stats(self) -> dict:
        s = dict(self._stats)
        s["push_ms_avg"] = round(s["push_ms_total"] / s["pushes"], 2) if s["pushes"] else None
        return s
