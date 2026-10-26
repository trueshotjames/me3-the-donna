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

Audio gate (GATE_ENABLED=1): audio only goes to Flux while it can matter, to limit Flux use.
    open   when the local VAD hears a voice (tuned for high recall), or while the bot is
           speaking and for BOT_HANGOVER_SECS after (so Flux decides barge-in), and
           first sends the last PREROLL_MS of audio kept in memory (words said before the
           VAD fired).
    close  when the VAD is quiet, the bot is not speaking, and Flux has ended its turn
           (or never started one within GATE_NOISE_TAIL_SECS: noise), or as a backstop
           GATE_CAP_SECS after the VAD went quiet.

Timing: every pushed chunk's capture time and its position in the Flux stream are kept,
so Flux word times (seconds of audio) map back to the wall-clock moment you spoke.
"""

from __future__ import annotations

import asyncio
import base64
import time
from collections import deque
from typing import AsyncGenerator

from loguru import logger
from mcp import StdioServerParameters
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
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
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessorSetup
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
from assistant.turn_metrics import METRICS


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
        turn_start: str = "asr",
        should_interrupt: bool = True,
        gate_enabled: bool = True,
        preroll_ms: int = 500,
        bot_hangover_secs: float = 1.5,
        gate_noise_tail_secs: float = 0.8,
        gate_cap_secs: float | None = None,
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
        self._send_queue: asyncio.Queue[tuple[bytes, float] | None] = asyncio.Queue(maxsize=64)
        self._sender: asyncio.Task | None = None
        self._poller: asyncio.Task | None = None
        self._user_is_speaking = False
        self._stats = {
            "pushes": 0,
            "push_ms_total": 0.0,
            "events": 0,
            "eager": 0,
            "eager_cancelled": 0,
            "end_of_turn": 0,
            "dropped": 0,
            "audio_total_s": 0.0,
            "audio_sent_s": 0.0,
            "gate_opens": 0,
        }
        self._eager_pending = False
        # audio gate
        self._push_secs = self._push_bytes / (sample_rate * 2)
        self._gate_enabled = gate_enabled
        self._gate_open = not gate_enabled
        self._ring: deque[tuple[bytes, float]] = deque(maxlen=max(1, round(preroll_ms / 1000 / self._push_secs)))
        self._bot_hangover = bot_hangover_secs
        self._noise_tail = gate_noise_tail_secs
        self._gate_cap = gate_cap_secs if gate_cap_secs is not None else eot_timeout_ms / 1000 + 1.0
        self._vad_speaking = False
        self._t_vad_stopped = 0.0
        self._bot_speaking = False
        self._t_bot_stopped = float("-inf")
        self._flux_turn_open = False
        self._t_gate_opened = 0.0
        self._buf_t: float | None = None  # capture time of the first byte in self._buffer
        # (conn, stream offset s, capture wall time, duration s) per pushed chunk
        self._chunk_map: deque[tuple[int, float, float, float]] = deque(maxlen=4000)

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

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        now = time.time()
        if isinstance(frame, VADUserStartedSpeakingFrame):
            self._vad_speaking = True
            METRICS.vad_started(now)
        elif isinstance(frame, VADUserStoppedSpeakingFrame):
            self._vad_speaking = False
            self._t_vad_stopped = now
            METRICS.vad_stopped(now)
        elif isinstance(frame, BotStartedSpeakingFrame):
            self._bot_speaking = True
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._bot_speaking = False
            self._t_bot_stopped = now
        await super().process_frame(frame, direction)

    def _update_gate(self, now: float) -> None:
        if not self._gate_enabled:
            return
        bot_window = self._bot_speaking or (now - self._t_bot_stopped < self._bot_hangover)
        active = self._vad_speaking or bot_window
        if not self._gate_open:
            if active:
                reason = "voice" if self._vad_speaking else "bot speaking"
                preroll = list(self._ring)
                self._ring.clear()
                self._gate_open = True
                self._t_gate_opened = now
                self._stats["gate_opens"] += 1
                preroll_ms = len(preroll) * self._push_secs * 1000
                logger.info(f"{self}: gate opened ({reason}), sending {preroll_ms:.0f} ms of pre-roll")
                METRICS.gate_opened(now, reason, preroll_ms)
                for item in preroll:
                    self._enqueue(item)
            return
        if active:
            return
        quiet = now - max(self._t_vad_stopped, self._t_gate_opened, self._t_bot_stopped + self._bot_hangover)
        if (not self._flux_turn_open and quiet >= self._noise_tail) or quiet >= self._gate_cap:
            self._gate_open = False
            why = "turn ended" if not self._flux_turn_open else "backstop, Flux never ended the turn"
            logger.info(f"{self}: gate closed after {now - self._t_gate_opened:.1f} s ({why})")

    def _enqueue(self, item: tuple[bytes, float]) -> None:
        self._stats["audio_sent_s"] += self._push_secs
        try:
            self._send_queue.put_nowait(item)
        except asyncio.QueueFull:
            # Never block the audio path; drop the oldest block instead.
            self._send_queue.get_nowait()
            self._send_queue.put_nowait(item)
            self._stats["dropped"] += 1
            METRICS.asr_dropped()

    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame | None, None]:
        """Cut microphone audio into ~60 ms blocks; send them through the gate (or keep as pre-roll)."""
        if not self._session_id:
            yield None
            return
        now = time.time()
        if not self._buffer:
            # The frame arrives when its last sample was captured.
            self._buf_t = now - len(audio) / (self.sample_rate * 2)
        self._buffer.extend(audio)
        self._update_gate(now)
        while len(self._buffer) >= self._push_bytes:
            chunk = bytes(self._buffer[: self._push_bytes])
            del self._buffer[: self._push_bytes]
            item = (chunk, self._buf_t or now)
            self._buf_t = (self._buf_t or now) + self._push_secs
            self._stats["audio_total_s"] += self._push_secs
            if self._gate_open:
                self._enqueue(item)
            else:
                self._ring.append(item)
        yield None

    async def _sender_loop(self):
        while True:
            item = await self._send_queue.get()
            if item is None:
                return
            chunk, captured = item
            t0 = time.time()
            try:
                result = await self._bridge.call_json(
                    "asr_push",
                    {"session": self._session_id, "pcm_base64": base64.b64encode(chunk).decode("ascii")},
                    timeout=5,
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(f"{self}: asr_push failed: {e}")
                await asyncio.sleep(0.2)
                continue
            ms = (time.time() - t0) * 1000
            self._stats["pushes"] += 1
            self._stats["push_ms_total"] += ms
            METRICS.asr_push(ms)
            if isinstance(result, dict) and "offset" in result:
                self._chunk_map.append((result.get("conn", 0), float(result["offset"]), captured, self._push_secs))

    def _wall(self, conn: int | None, stream_secs: float | None) -> float | None:
        """Map a time in the Flux stream (seconds of audio on a connection) to when it was captured."""
        if stream_secs is None:
            return None
        for c, off, captured, dur in reversed(self._chunk_map):
            if conn is not None and c != conn:
                continue
            if off <= stream_secs < off + dur + 1e-6:
                return captured + (stream_secs - off)
            if off + dur < stream_secs - 30:
                break
        return None

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
        now = time.time()
        t_server = event.get("t")
        if kind == "StartOfTurn":
            logger.info(f"{self}: Flux start of turn")
            self._user_is_speaking = True
            self._flux_turn_open = True
            METRICS.start_of_turn(now, t_server)
            await self.broadcast_frame(ProposedUserStartedSpeakingFrame)
            await self._call_event_handler("on_start_of_turn", transcript)
        elif kind == "Update":
            heard = self._wall(event.get("conn"), event.get("audio_window_end"))
            if heard is not None:
                METRICS.asr_update_lag((now - heard) * 1000)
            if transcript:
                await self.push_frame(
                    InterimTranscriptionFrame(transcript, self._user_id, time_now_iso8601(), None, result=event)
                )
        elif kind == "EagerEndOfTurn":
            self._stats["eager"] += 1
            self._eager_pending = True
            logger.info(f"{self}: Flux eager end of turn (LLM starts early): [{transcript}]")
            METRICS.eager(now, t_server, transcript)
            await self._push_eager_end_of_turn(transcript, user_id=self._user_id, result=event)
        elif kind == "TurnResumed":
            if self._eager_pending:
                self._stats["eager_cancelled"] += 1
                logger.info(f"{self}: Flux turn resumed; early reply discarded")
                METRICS.turn_resumed(now)
            self._eager_pending = False
            await self._cancel_eager_end_of_turn()
        elif kind == "EndOfTurn":
            self._stats["end_of_turn"] += 1
            early = "early reply already running" if self._eager_pending else "no early reply"
            self._eager_pending = False
            logger.info(f"{self}: Flux end of turn ({early}): [{transcript}]")
            self._user_is_speaking = False
            self._flux_turn_open = False
            self._record_end_of_turn(event, transcript, now, t_server)
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

    def _record_end_of_turn(self, event: dict, transcript: str, now: float, t_server: float | None) -> None:
        words = [w for w in (event.get("words") or []) if isinstance(w, dict)]
        conn = event.get("conn")
        start = end = None
        warning = None
        last_word = None
        if words:
            last_word = words[-1].get("word")
            start = self._wall(conn, words[0].get("start"))
            end = self._wall(conn, words[-1].get("end"))
            if end is None:
                warning = f"last word time {words[-1].get('end')} s is outside the recorded audio"
            elif end > now + 0.05 or now - end > 15:
                warning = f"last word maps to {now - end:+.2f} s before end of turn; word times may be per turn"
                start = end = None
        else:
            warning = "Flux sent no word timings"
        METRICS.end_of_turn(
            now,
            t_server,
            transcript,
            start,
            end,
            "flux words" if end is not None else None,
            last_word,
            event.get("trigger"),
            warning,
        )
        if warning:
            logger.warning(f"{self}: speech-end timing: {warning}")

    # ----------------------------------------------------------------- helpers

    @property
    def stats(self) -> dict:
        s = dict(self._stats)
        s["push_ms_avg"] = round(s["push_ms_total"] / s["pushes"], 2) if s["pushes"] else None
        s["audio_total_s"] = round(s["audio_total_s"], 1)
        s["audio_sent_s"] = round(s["audio_sent_s"], 1)
        s["sent_pct"] = round(100 * s["audio_sent_s"] / s["audio_total_s"], 1) if s["audio_total_s"] else None
        return s
