"""Pipecat TTS service that streams audio from our TTS MCP server.

Each sentence Pipecat aggregates from the LLM is sent to `tts_start`; audio is
then pulled with `tts_read` in ~80 ms blocks (MCP audio content blocks) and
pushed into the pipeline as it arrives, so playback starts after the first
block rather than after the whole sentence. On barge-in Pipecat calls
`on_audio_context_interrupted`, which calls `tts_stop` to clear the provider.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from typing import AsyncGenerator, AsyncIterator

import numpy as np

from loguru import logger
from mcp import StdioServerParameters
from mcp.types import AudioContent, TextContent
from pipecat.frames.frames import CancelFrame, EndFrame, ErrorFrame, Frame, StartFrame
from pipecat.processors.frame_processor import FrameProcessorSetup
from pipecat.services.tts_service import TTSService
from pipecat.services.settings import TTSSettings

from assistant.mcp_bridge import MCPBridge
from assistant.turn_metrics import METRICS

SILENCE_THRESHOLD = 330  # |sample| above this (about -40 dBFS) counts as sound


def leading_silence_ms(pcm: bytes, sample_rate: int) -> float | None:
    """Milliseconds of near-silence at the start of a 16-bit mono PCM block (None if all silent)."""
    samples = np.frombuffer(pcm[: len(pcm) - len(pcm) % 2], dtype=np.int16)
    loud = np.flatnonzero(np.abs(samples.astype(np.int32)) > SILENCE_THRESHOLD)
    if loud.size == 0:
        return None
    return round(float(loud[0]) / sample_rate * 1000, 1)


class MCPTTSService(TTSService):
    """Streaming TTS over our TTS MCP server (which wraps Deepgram Aura-2)."""

    def __init__(
        self,
        *,
        server_params: StdioServerParameters,
        voice: str = "aura-2-thalia-en",
        server_sample_rate: int = 16000,
        read_wait_ms: int = 400,
        sample_rate: int | None = None,
        **kwargs,
    ):
        super().__init__(
            sample_rate=sample_rate,
            push_start_frame=True,
            push_stop_frames=True,
            stop_frame_timeout_s=1.0,
            settings=TTSSettings(model="deepgram-aura-2-via-mcp", voice=voice, language=None),
            **kwargs,
        )
        self._bridge = MCPBridge("tts", stdio=server_params)
        self._server_sample_rate = server_sample_rate
        self._read_wait_ms = read_wait_ms
        self._active_streams: dict[str, set[str]] = {}  # context_id -> stream ids in flight
        self._stats = {"sentences": 0, "first_audio_ms_total": 0.0, "reads": 0}

    def can_generate_metrics(self) -> bool:
        return True

    # ---------------------------------------------------------------- lifecycle

    async def setup(self, setup: FrameProcessorSetup):
        await super().setup(setup)
        try:
            await self._bridge.start()
            await self._bridge.call("tts_warmup", timeout=15)
            await self._call_event_handler("on_connected")
        except Exception as e:  # noqa: BLE001
            logger.error(f"{self}: could not start TTS MCP server: {e!r}")
            await self._call_event_handler("on_connection_error", f"{e}")

    async def stop(self, frame: EndFrame):
        await super().stop(frame)
        await self._bridge.close()

    async def cancel(self, frame: CancelFrame):
        await super().cancel(frame)
        await self._bridge.close()

    async def cleanup(self):
        await super().cleanup()
        await self._bridge.close()

    # ------------------------------------------------------------------- speech

    async def run_tts(self, text: str, context_id: str) -> AsyncGenerator[Frame | None, None]:
        if not self._bridge.connected:
            yield ErrorFrame(error="TTS MCP server is not connected")
            return
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        rec: dict = {"t_call": time.time(), "chars": len(text), "chunks": [], "read_ms": [], "audio_bytes": 0}
        try:
            await self.start_tts_usage_metrics(text)
            info = await self._bridge.call_json("tts_start", {"text": text, "context": context_id}, timeout=10)
            rec["t_started"] = time.time()
            stream = info["stream"]
            self._active_streams.setdefault(context_id, set()).add(stream)
            self._stats["sentences"] += 1
            first = True
            async for frame in self._stream_audio_frames_from_iterator(
                self._read_stream(stream, rec), in_sample_rate=self._server_sample_rate, context_id=context_id
            ):
                if first:
                    first = False
                    await self.stop_ttfb_metrics()
                    self._stats["first_audio_ms_total"] += (loop.time() - t0) * 1000
                yield frame
        except asyncio.CancelledError:
            rec["interrupted"] = True
            raise
        except Exception as e:  # noqa: BLE001
            logger.error(f"{self}: TTS failed: {e!r}")
            rec["error"] = repr(e)
            yield ErrorFrame(error=f"TTS MCP error: {e}")
        finally:
            await self.stop_ttfb_metrics()
            streams = self._active_streams.get(context_id)
            if streams:
                streams.discard(locals().get("stream", ""))
            rec["audio_s"] = rec["audio_bytes"] / (self._server_sample_rate * 2)
            METRICS.tts_sentence(rec)

    async def _read_stream(self, stream: str, rec: dict | None = None) -> AsyncIterator[bytes]:
        """Pull audio blocks until the server says eof."""
        idle_polls = 0
        lead_buf = b""
        while True:
            t_call = time.time()
            result = await self._bridge.call("tts_read", {"stream": stream, "wait_ms": self._read_wait_ms}, timeout=10)
            t_back = time.time()
            self._stats["reads"] += 1
            got_audio = False
            for block in result.content:
                if isinstance(block, AudioContent):
                    got_audio = True
                    data = base64.b64decode(block.data)
                    if rec is not None:
                        rec["read_ms"].append((t_back - t_call) * 1000)
                        rec["chunks"].append((t_back, len(data)))
                        rec["audio_bytes"] += len(data)
                        if "leading_silence_ms" not in rec:
                            lead_buf += data
                            lead = leading_silence_ms(lead_buf, self._server_sample_rate)
                            if lead is not None or len(lead_buf) > self._server_sample_rate:  # give up after 0.5 s
                                rec["leading_silence_ms"] = lead if lead is not None else len(lead_buf) / (self._server_sample_rate * 2) * 1000
                    yield data
                elif isinstance(block, TextContent):
                    try:
                        status = json.loads(block.text)
                    except json.JSONDecodeError:
                        continue
                    if rec is not None and isinstance(status.get("meta"), dict):
                        m = status["meta"]
                        rec["t_speak_sent"] = m.get("t_speak_sent")
                        rec["t_first_audio_server"] = m.get("t_first_audio")
                    if status.get("eof"):
                        if status.get("error"):
                            logger.warning(f"{self}: tts_read: {status['error']}")
                        if rec is not None and status.get("cancelled"):
                            rec["interrupted"] = True
                        return
            if got_audio:
                idle_polls = 0
            else:
                idle_polls += 1
                if idle_polls * self._read_wait_ms > 15000:
                    logger.warning(f"{self}: no audio for 15 s on stream {stream}; giving up")
                    return

    async def on_audio_context_interrupted(self, context_id: str):
        """Barge-in: tell the server to clear the provider buffer."""
        streams = self._active_streams.pop(context_id, set())
        try:
            await self._bridge.call("tts_stop", {"stream": next(iter(streams), "")}, timeout=5)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"{self}: tts_stop failed: {e}")
        await super().on_audio_context_interrupted(context_id)

    async def on_audio_context_completed(self, context_id: str):
        self._active_streams.pop(context_id, None)
        await super().on_audio_context_completed(context_id)

    @property
    def stats(self) -> dict:
        s = dict(self._stats)
        s["first_audio_ms_avg"] = (
            round(s["first_audio_ms_total"] / s["sentences"], 1) if s["sentences"] else None
        )
        return s
