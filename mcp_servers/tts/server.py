"""TTS MCP server: wraps Deepgram Aura-2 (cloud streaming text-to-speech).

Runs on the Pi. Three tools stream audio back through MCP:

    tts_start(text, context)  -> {"stream": id}   queue a sentence on the warm Aura WebSocket
    tts_read(stream, wait_ms) -> audio content block (raw 16-bit PCM, base64) or {"eof": true}
    tts_stop(stream)          -> Clear the provider buffer (barge-in)

Jobs are pipelined: the next sentence's Speak+Flush is sent as soon as it is
queued, and Aura returns audio in order, so audio bytes are attributed to the
oldest unfinished job until its "Flushed" message arrives.

Transport: stdio (spawned by the assistant). Never print to stdout.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import websockets
from loguru import logger
from mcp.server.mcpserver import MCPServer
from mcp.types import AudioContent, TextContent

from mcp_servers.common import env, env_int, setup

setup("tts")

DEEPGRAM_API_KEY = env("DEEPGRAM_API_KEY")
AURA_URL = env("DEEPGRAM_AURA_URL", "wss://api.deepgram.com/v1/speak")
VOICE = env("DEEPGRAM_AURA_VOICE", "aura-2-thalia-en")
SAMPLE_RATE = env_int("TTS_SAMPLE_RATE", 16000)
CHUNK_MS = env_int("TTS_CHUNK_MS", 80)  # size of each tts_read block
CHUNK_BYTES = SAMPLE_RATE * 2 * CHUNK_MS // 1000
KEEPALIVE_SECS = 20
MIME = f"audio/pcm;rate={SAMPLE_RATE};channels=1"

mcp = MCPServer("tts", instructions="Streaming text-to-speech (Deepgram Aura-2) for the ME3 voice assistant.")


@dataclass
class Job:
    id: str
    text: str
    context: str
    queue: asyncio.Queue = field(default_factory=asyncio.Queue)  # bytes or None (EOF)
    created: float = field(default_factory=time.time)
    first_audio: float | None = None
    speak_sent: float | None = None
    meta_sent: bool = False
    done: bool = False
    cancelled: bool = False
    bytes_out: int = 0

    def finish(self) -> None:
        if not self.done:
            self.done = True
            self.queue.put_nowait(None)


class Aura:
    """One warm Aura-2 WebSocket shared by all jobs, used strictly in order."""

    def __init__(self) -> None:
        self.ws = None
        self.reader: asyncio.Task | None = None
        self.keepalive: asyncio.Task | None = None
        self.in_flight: deque[Job] = deque()  # oldest first
        self.lock = asyncio.Lock()
        self.last_send = 0.0
        self.buffer = bytearray()

    def url(self) -> str:
        return f"{AURA_URL}?model={VOICE}&encoding=linear16&sample_rate={SAMPLE_RATE}"

    async def connect(self) -> None:
        async with self.lock:
            if self.ws is not None:
                return
            if not DEEPGRAM_API_KEY:
                raise RuntimeError("DEEPGRAM_API_KEY is not set (see .env.example)")
            t0 = time.perf_counter()
            self.ws = await websockets.connect(
                self.url(),
                additional_headers={"Authorization": f"Token {DEEPGRAM_API_KEY}"},
                max_size=None,
                ping_interval=20,
                ping_timeout=20,
            )
            self.reader = asyncio.create_task(self._read(self.ws))
            if self.keepalive is None:
                self.keepalive = asyncio.create_task(self._keepalive())
            logger.info(f"Aura connected in {(time.perf_counter() - t0) * 1000:.0f} ms ({VOICE} @ {SAMPLE_RATE} Hz)")

    async def _read(self, ws) -> None:
        try:
            async for msg in ws:
                if isinstance(msg, bytes):
                    self._on_audio(msg)
                    continue
                try:
                    data = json.loads(msg)
                except json.JSONDecodeError:
                    continue
                mtype = data.get("type")
                if mtype == "Flushed":
                    self._flush_tail()
                    if self.in_flight:
                        job = self.in_flight.popleft()
                        job.finish()
                        logger.debug(f"job {job.id} flushed ({job.bytes_out} bytes)")
                elif mtype == "Cleared":
                    logger.debug("Aura cleared")
                elif mtype == "Warning":
                    logger.warning(f"Aura warning: {data}")
                elif mtype == "Metadata":
                    pass
                else:
                    logger.debug(f"Aura message: {data}")
        except websockets.ConnectionClosed as e:
            logger.warning(f"Aura connection closed ({e.code})")
        except Exception as e:  # noqa: BLE001
            logger.error(f"Aura reader failed: {e}")
        finally:
            if self.ws is ws:
                self.ws = None
            # Anything still in flight will never finish on this socket.
            while self.in_flight:
                self.in_flight.popleft().finish()

    def _on_audio(self, data: bytes) -> None:
        if not self.in_flight:
            return  # audio after a Clear; drop it
        self.buffer.extend(data)
        while len(self.buffer) >= CHUNK_BYTES:
            chunk = bytes(self.buffer[:CHUNK_BYTES])
            del self.buffer[:CHUNK_BYTES]
            self._deliver(chunk)

    def _flush_tail(self) -> None:
        if self.buffer:
            chunk = bytes(self.buffer)
            self.buffer.clear()
            self._deliver(chunk)

    def _deliver(self, chunk: bytes) -> None:
        job = self.in_flight[0]
        if job.first_audio is None:
            job.first_audio = time.time()
            logger.debug(f"job {job.id} first audio after {(job.first_audio - job.created) * 1000:.0f} ms")
        job.bytes_out += len(chunk)
        job.queue.put_nowait(chunk)

    async def speak(self, job: Job) -> None:
        if self.ws is None:
            await self.connect()
        self.in_flight.append(job)
        try:
            await self.ws.send(json.dumps({"type": "Speak", "text": job.text}))
            await self.ws.send(json.dumps({"type": "Flush"}))
            self.last_send = time.time()
            job.speak_sent = self.last_send
        except websockets.ConnectionClosed:
            self.ws = None
            self.in_flight.remove(job)
            await self.connect()
            self.in_flight.append(job)
            await self.ws.send(json.dumps({"type": "Speak", "text": job.text}))
            await self.ws.send(json.dumps({"type": "Flush"}))
            self.last_send = time.time()
            job.speak_sent = self.last_send

    async def clear(self) -> None:
        """Barge-in: stop everything in flight."""
        self.buffer.clear()
        while self.in_flight:
            job = self.in_flight.popleft()
            job.cancelled = True
            job.finish()
        if self.ws is not None:
            try:
                await self.ws.send(json.dumps({"type": "Clear"}))
            except Exception:  # noqa: BLE001
                self.ws = None

    async def _keepalive(self) -> None:
        # Deepgram closes idle speak sockets; a Clear on an idle socket is harmless
        # and keeps the connection warm so the first sentence has no handshake cost.
        while True:
            await asyncio.sleep(KEEPALIVE_SECS)
            if self.ws is not None and not self.in_flight and time.time() - self.last_send > KEEPALIVE_SECS:
                try:
                    await self.ws.send(json.dumps({"type": "Clear"}))
                except Exception:  # noqa: BLE001
                    self.ws = None


AURA = Aura()
JOBS: dict[str, Job] = {}


@mcp.tool()
async def tts_start(text: str, context: str = "") -> dict[str, Any]:
    """Queue text for synthesis. Returns {"stream": id}; read audio with tts_read."""
    text = text.strip()
    job = Job(id=uuid.uuid4().hex[:8], text=text, context=context)
    JOBS[job.id] = job
    if not text:
        job.finish()
        return {"stream": job.id}
    await AURA.speak(job)
    return {"stream": job.id, "sample_rate": SAMPLE_RATE, "encoding": "linear16"}


@mcp.tool()
async def tts_read(stream: str, wait_ms: int = 500) -> list[AudioContent | TextContent]:
    """Read the next block of audio for a stream (raw 16-bit mono PCM, base64). Returns {"eof": true} when done."""
    job = JOBS.get(stream)
    if job is None:
        return [TextContent(type="text", text=json.dumps({"eof": True, "error": "unknown stream"}))]
    try:
        item = await asyncio.wait_for(job.queue.get(), timeout=max(wait_ms, 0) / 1000)
    except asyncio.TimeoutError:
        return [TextContent(type="text", text=json.dumps({"eof": False, "wait": True}))]
    if item is None:
        JOBS.pop(stream, None)
        return [
            TextContent(
                type="text",
                text=json.dumps({"eof": True, "cancelled": job.cancelled, "bytes": job.bytes_out}),
            )
        ]
    # Coalesce whatever else is already available so one call carries more audio.
    parts = [item]
    while len(parts) < 4:
        try:
            nxt = job.queue.get_nowait()
        except asyncio.QueueEmpty:
            break
        if nxt is None:
            job.queue.put_nowait(None)
            break
        parts.append(nxt)
    data = b"".join(parts)
    out: list[AudioContent | TextContent] = [
        AudioContent(type="audio", data=base64.b64encode(data).decode("ascii"), mime_type=MIME)
    ]
    if not job.meta_sent:
        # Server-side timings for the client's turn record (sent once, with the first audio).
        job.meta_sent = True
        meta = {"t_created": job.created, "t_speak_sent": job.speak_sent, "t_first_audio": job.first_audio}
        out.append(TextContent(type="text", text=json.dumps({"eof": False, "meta": meta})))
    return out


@mcp.tool()
async def tts_stop(stream: str = "") -> dict[str, Any]:
    """Stop synthesis immediately (barge-in). Clears every job in flight on the shared connection."""
    await AURA.clear()
    if stream:
        job = JOBS.pop(stream, None)
        if job:
            job.cancelled = True
            job.finish()
    return {"ok": True}


@mcp.tool()
async def tts_warmup() -> dict[str, Any]:
    """Open the provider connection ahead of time so the first sentence has no handshake cost."""
    await AURA.connect()
    return {"ok": True, "voice": VOICE, "sample_rate": SAMPLE_RATE}


@mcp.tool()
async def tts_health() -> dict[str, Any]:
    """Report server status."""
    return {
        "provider": "deepgram-aura-2",
        "voice": VOICE,
        "sample_rate": SAMPLE_RATE,
        "api_key_set": bool(DEEPGRAM_API_KEY),
        "connected": AURA.ws is not None,
        "in_flight": [j.id for j in AURA.in_flight],
    }


if __name__ == "__main__":
    mcp.run(transport="stdio")
