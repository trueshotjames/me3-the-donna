"""TTS MCP server: wraps Deepgram Aura-2 (cloud streaming text-to-speech).

Runs on the Pi. Three tools stream audio back through MCP:

    tts_start(text, context)  -> {"stream": id}   queue a sentence on the warm Aura WebSocket
    tts_read(stream, wait_ms) -> audio content block (raw 16-bit PCM, base64) or {"eof": true}
    tts_stop(stream)          -> Clear the provider buffer (barge-in)

Jobs are pipelined: the next sentence's Speak+Flush is sent as soon as it is
queued, and Aura returns audio in order, so audio bytes are attributed to the
oldest unfinished job until its "Flushed" message arrives.

    tts_set_speed(speed)      -> v8: speaking rate 0.7-1.5 (Aura-2 "speed"); the sockets reopen at the new rate

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

from mcp_servers.common import env, env_float, env_int, log_net_event, setup

setup("tts")

DEEPGRAM_API_KEY = env("DEEPGRAM_API_KEY")
AURA_URL = env("DEEPGRAM_AURA_URL", "wss://api.deepgram.com/v1/speak")
# v3: with a regional AURA_URL (wss://api.in.deepgram.com/v1/speak), connect attempts after a failed one use this
AURA_FALLBACK_URL = env("DEEPGRAM_AURA_FALLBACK_URL", "wss://api.deepgram.com/v1/speak")
VOICE = env("DEEPGRAM_AURA_VOICE", "aura-2-thalia-en")
SAMPLE_RATE = env_int("TTS_SAMPLE_RATE", 16000)
CHUNK_MS = env_int("TTS_CHUNK_MS", 80)  # size of each tts_read block
CHUNK_BYTES = SAMPLE_RATE * 2 * CHUNK_MS // 1000
KEEPALIVE_SECS = 20
# v1: find a dead or stalled Aura socket within seconds (it was up to 40 s) and replace it
PING_SECS = env_float("TTS_PING_SECS", 5.0)
SEND_TIMEOUT_SECS = env_float("TTS_SEND_TIMEOUT_SECS", 1.5)
# v2: 19 of 83 v1 connects failed at the 4 s open timeout on Wi-Fi though most would have finished
OPEN_TIMEOUT_SECS = env_float("TTS_OPEN_TIMEOUT_SECS", 8.0)
CONNECT_ATTEMPTS = env_int("TTS_CONNECT_ATTEMPTS", 3)
STALL_SECS = env_float("TTS_STALL_SECS", 2.0)
# v2: keep a second socket open so a dead or stalled one is replaced at once, with no handshake
SPARE_SOCKET = env_int("TTS_SPARE_SOCKET", 1) == 1
MAX_RESENDS = 2
MIME = f"audio/pcm;rate={SAMPLE_RATE};channels=1"
# v7 (TTS_PHRASE_CACHE): a short sentence she has said before ("Kitchen light on.", "Okay, stopping.") is served from
# memory at once instead of waiting ~250 ms for Aura's first byte. Still the same MCP tools (tts_start / tts_read).
PHRASE_CACHE = env_int("TTS_PHRASE_CACHE", 0) == 1
PHRASE_MAX_CHARS = env_int("TTS_PHRASE_MAX_CHARS", 80)
PHRASE_MAX_ENTRIES = env_int("TTS_PHRASE_MAX_ENTRIES", 128)
CACHE: "dict[str, bytes]" = {}
# v8 (SPEAKING_RATE): Aura-2's speed query parameter (probe 4 Nov: one sentence 4.88 s at 1.0, 6.72 s at 0.8)
SPEED = env_float("TTS_SPEED", 1.0)
CACHE_STATS = {"hits": 0, "stored": 0}


def _phrase_key(text: str) -> str:
    return f"{VOICE}|{SAMPLE_RATE}|{SPEED:g}|" + " ".join(text.lower().split())

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
    resends: int = 0
    pcm: bytearray | None = None  # v7: audio kept for the phrase cache (short sentences only)

    def finish(self) -> None:
        if not self.done:
            self.done = True
            self.queue.put_nowait(None)
            if self.pcm is not None and not self.cancelled and len(self.pcm) > SAMPLE_RATE // 5:
                CACHE[_phrase_key(self.text)] = bytes(self.pcm)
                CACHE_STATS["stored"] += 1
                while len(CACHE) > PHRASE_MAX_ENTRIES:
                    CACHE.pop(next(iter(CACHE)))


class Aura:
    """One warm Aura-2 WebSocket shared by all jobs, used strictly in order.

    v1: a dead or stalled socket is found in seconds, not tens of seconds, and replaced in the background:
    pings every PING_SECS, every send has a timeout, a sentence with no audio STALL_SECS after Speak is
    resent on a fresh socket, and audio that arrives between a Clear and its "Cleared" is dropped (it
    belongs to the reply that was interrupted).
    """

    def __init__(self) -> None:
        self.ws = None
        self.reader: asyncio.Task | None = None
        self.keepalive: asyncio.Task | None = None
        self.reconnecting: asyncio.Task | None = None
        self.in_flight: deque[Job] = deque()  # oldest first
        self.lock = asyncio.Lock()
        self.last_send = 0.0
        self.buffer = bytearray()
        self.clearing = False
        self.spare = None  # v2: a second open socket, idle until the main one fails
        self.spare_task: asyncio.Task | None = None
        self.stats = {"spare_used": 0, "stalls": 0}
        self.speed_of: dict[int, float] = {}  # v8: socket id -> the speed it was opened with

    def url(self, attempt: int = 0) -> str:
        base = AURA_FALLBACK_URL if attempt > 0 and AURA_FALLBACK_URL else AURA_URL
        speed = f"&speed={SPEED:g}" if SPEED != 1.0 else ""
        return f"{base}?model={VOICE}&encoding=linear16&sample_rate={SAMPLE_RATE}{speed}"

    async def _open(self):
        """Open one Aura socket (CONNECT_ATTEMPTS tries). Returns (ws, ms)."""
        if not DEEPGRAM_API_KEY:
            raise RuntimeError("DEEPGRAM_API_KEY is not set (see .env.example)")
        last: Exception | None = None
        for attempt in range(CONNECT_ATTEMPTS):
            t0 = time.perf_counter()
            try:
                ws = await websockets.connect(
                    self.url(attempt),
                    additional_headers={"Authorization": f"Token {DEEPGRAM_API_KEY}"},
                    max_size=None,
                    open_timeout=OPEN_TIMEOUT_SECS,
                    ping_interval=PING_SECS,
                    ping_timeout=PING_SECS,
                    close_timeout=1,
                )
            except Exception as e:  # noqa: BLE001
                last = e
                log_net_event("Aura", (time.perf_counter() - t0) * 1000, ok=False, error=repr(e))
                logger.warning(f"Aura connect attempt {attempt + 1} failed: {e!r}")
                await asyncio.sleep(0.3 * (attempt + 1))
                continue
            ms = (time.perf_counter() - t0) * 1000
            log_net_event("Aura", ms, reconnect=attempt > 0)
            return ws, ms
        raise last or RuntimeError("Aura connect failed")

    async def connect(self) -> None:
        async with self.lock:
            if self.ws is not None:
                return
            spare = self.spare
            if spare is not None and self.speed_of.get(id(spare), 1.0) != SPEED:
                self.spare = None  # v8: opened at the old speaking rate
                asyncio.create_task(_close_quietly(spare))
                spare = None
            if spare is not None:
                # v2: the warm spare takes over at once (its reader is already running)
                self.spare = None
                self.ws = spare
                self.clearing = False
                self.buffer.clear()
                self.stats["spare_used"] += 1
                logger.info("Aura: switched to the warm spare socket (no handshake)")
            else:
                ws, ms = await self._open()
                self.speed_of[id(ws)] = SPEED
                self.ws = ws
                self.clearing = False
                self.buffer.clear()
                self.reader = asyncio.create_task(self._read(ws))
                logger.info(f"Aura connected in {ms:.0f} ms ({VOICE} @ {SAMPLE_RATE} Hz)")
            if self.keepalive is None:
                self.keepalive = asyncio.create_task(self._keepalive())
        self._refill_spare()

    def _refill_spare(self) -> None:
        if not SPARE_SOCKET or self.spare is not None:
            return
        if self.spare_task is None or self.spare_task.done():
            self.spare_task = asyncio.create_task(self._open_spare())

    async def _open_spare(self) -> None:
        speed = SPEED
        try:
            ws, ms = await self._open()
            self.speed_of[id(ws)] = speed
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Aura spare socket failed: {e!r}")
            return
        if self.spare is not None or self.speed_of.get(id(ws), SPEED) != SPEED:
            await _close_quietly(ws)
            return
        self.speed_of[id(ws)] = SPEED
        self.spare = ws
        asyncio.create_task(self._read(ws))  # drains Cleared replies now; becomes the main reader on takeover
        logger.info(f"Aura spare socket ready in {ms:.0f} ms")

    async def reopen(self) -> None:
        """v8: new speaking rate. Let the sentence in flight finish (up to 2 s), then open fresh sockets."""
        for _ in range(40):
            if not self.in_flight:
                break
            await asyncio.sleep(0.05)
        old, spare = self.ws, self.spare
        self.ws = self.spare = None
        for ws in (old, spare):
            if ws is not None:
                self.speed_of.pop(id(ws), None)
                asyncio.create_task(_close_quietly(ws))
        await self.connect()

    def _drop_socket(self, ws) -> None:
        """Forget a socket that is dead or stalled, close it without waiting, reconnect in the background."""
        if ws is None:
            return
        if self.ws is ws:
            self.ws = None
        if self.spare is ws:
            self.spare = None
        asyncio.create_task(_close_quietly(ws))
        self._reconnect_soon()

    def _reconnect_soon(self) -> None:
        if self.reconnecting is None or self.reconnecting.done():
            self.reconnecting = asyncio.create_task(self._reconnect())

    async def _reconnect(self) -> None:
        try:
            await self.connect()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Aura background reconnect failed: {e!r}")

    async def _send(self, ws, payload: dict) -> None:
        await asyncio.wait_for(ws.send(json.dumps(payload)), timeout=SEND_TIMEOUT_SECS)

    async def _read(self, ws) -> None:
        try:
            async for msg in ws:
                if self.ws is not ws:
                    continue  # the spare (or an old socket): nothing it says matters
                if isinstance(msg, bytes):
                    self._on_audio(msg)
                    continue
                try:
                    data = json.loads(msg)
                except json.JSONDecodeError:
                    continue
                mtype = data.get("type")
                if mtype == "Flushed":
                    if self.clearing:
                        continue  # the Flush of a cleared job
                    self._flush_tail()
                    if self.in_flight:
                        job = self.in_flight.popleft()
                        job.finish()
                        logger.debug(f"job {job.id} flushed ({job.bytes_out} bytes)")
                elif mtype == "Cleared":
                    self.clearing = False
                    self.buffer.clear()
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
            if self.spare is ws:
                self.spare = None
                self._refill_spare()
            if self.ws is ws:
                self.ws = None
                # Jobs with no audio yet are resent on the next socket; a half-spoken one ends here.
                pending = [j for j in self.in_flight if j.bytes_out == 0 and not j.cancelled]
                for j in list(self.in_flight):
                    if j not in pending:
                        j.finish()
                self.in_flight.clear()
                if pending:
                    asyncio.create_task(self._resend(pending))
                else:
                    self._reconnect_soon()

    async def _resend(self, jobs: list[Job]) -> None:
        try:
            await self.connect()
            for job in jobs:
                if job.done or job.cancelled:
                    continue
                logger.warning(f"resending job {job.id} on a fresh Aura socket")
                await self._speak_on(job)
        except Exception as e:  # noqa: BLE001
            logger.error(f"Aura resend failed: {e!r}")
            for job in jobs:
                job.finish()

    def _on_audio(self, data: bytes) -> None:
        if not self.in_flight or self.clearing:
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
        if job.pcm is not None:
            job.pcm.extend(chunk)
        job.queue.put_nowait(chunk)

    async def _speak_on(self, job: Job) -> None:
        ws = self.ws
        if ws is None:
            await self.connect()
            ws = self.ws
        self.in_flight.append(job)
        try:
            await self._send(ws, {"type": "Speak", "text": job.text})
            await self._send(ws, {"type": "Flush"})
        except (websockets.ConnectionClosed, asyncio.TimeoutError, OSError) as e:
            logger.warning(f"Aura send failed ({e!r}); new socket")
            if job in self.in_flight:
                self.in_flight.remove(job)
            self._drop_socket(ws)
            await self.connect()
            ws = self.ws
            self.in_flight.append(job)
            await self._send(ws, {"type": "Speak", "text": job.text})
            await self._send(ws, {"type": "Flush"})
        self.last_send = time.time()
        job.speak_sent = self.last_send
        job.resends += 1
        asyncio.create_task(self._watch(job, ws))

    async def speak(self, job: Job) -> None:
        await self._speak_on(job)

    async def _watch(self, job: Job, ws) -> None:
        """No audio STALL_SECS after Speak while the socket is otherwise idle: the socket is stalled."""
        await asyncio.sleep(STALL_SECS)
        if job.done or job.cancelled or job.bytes_out > 0 or self.ws is not ws:
            return
        if self.in_flight and self.in_flight[0] is not job:
            return  # still waiting behind an earlier sentence; that one has its own watch
        if job.resends > MAX_RESENDS:
            return
        logger.warning(f"job {job.id}: no audio {STALL_SECS:.1f} s after Speak; dropping the socket and resending")
        self.stats["stalls"] += 1
        self._drop_socket(ws)  # the reader's finally resends every job with no audio yet

    async def clear(self) -> None:
        """Barge-in: stop everything in flight."""
        self.buffer.clear()
        while self.in_flight:
            job = self.in_flight.popleft()
            job.cancelled = True
            job.finish()
        ws = self.ws
        if ws is not None:
            self.clearing = True
            try:
                await self._send(ws, {"type": "Clear"})
            except Exception:  # noqa: BLE001
                self._drop_socket(ws)

    async def _keepalive(self) -> None:
        # Deepgram closes idle speak sockets; a Clear on an idle socket is harmless
        # and keeps the connection warm so the first sentence has no handshake cost.
        while True:
            await asyncio.sleep(KEEPALIVE_SECS)
            ws = self.ws
            if ws is None:
                self._reconnect_soon()
                continue
            if not self.in_flight and time.time() - self.last_send > KEEPALIVE_SECS:
                try:
                    self.clearing = True
                    await self._send(ws, {"type": "Clear"})
                except Exception:  # noqa: BLE001
                    self._drop_socket(ws)
            spare = self.spare
            if spare is not None:
                try:
                    await self._send(spare, {"type": "Clear"})
                except Exception:  # noqa: BLE001
                    self._drop_socket(spare)
            else:
                self._refill_spare()


async def _close_quietly(ws) -> None:
    try:
        await asyncio.wait_for(ws.close(), timeout=1.0)
    except Exception:  # noqa: BLE001
        pass


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
    if PHRASE_CACHE and len(text) <= PHRASE_MAX_CHARS:
        cached = CACHE.get(_phrase_key(text))
        if cached is not None:
            CACHE_STATS["hits"] += 1
            job.first_audio = job.speak_sent = time.time()
            for i in range(0, len(cached), CHUNK_BYTES):
                job.queue.put_nowait(cached[i:i + CHUNK_BYTES])
            job.bytes_out = len(cached)
            job.finish()
            logger.debug(f"job {job.id}: phrase cache hit ({len(cached)} bytes): {text[:40]!r}")
            return {"stream": job.id, "sample_rate": SAMPLE_RATE, "encoding": "linear16", "cached": True}
        job.pcm = bytearray()
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
async def tts_set_speed(speed: float = 1.0) -> dict[str, Any]:
    """Set the speaking rate for new sentences: 0.7 (slow) to 1.5 (fast), 1.0 = normal (Aura-2 speed)."""
    global SPEED
    new = round(min(1.5, max(0.7, float(speed))), 2)
    if new == SPEED:
        return {"speed": SPEED, "changed": False}
    SPEED = new
    logger.info(f"speaking rate set to {SPEED:g}")
    await AURA.reopen()
    return {"speed": SPEED, "changed": True}


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
        "spare": AURA.spare is not None,
        "in_flight": [j.id for j in AURA.in_flight],
        "phrase_cache": {**CACHE_STATS, "entries": len(CACHE), "on": PHRASE_CACHE},
        "speed": SPEED,
        **AURA.stats,
    }


if __name__ == "__main__":
    mcp.run(transport="stdio")
