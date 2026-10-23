"""ASR MCP server: wraps Deepgram Flux (cloud streaming speech-to-text).

Runs on the Pi. The harness streams audio *through* MCP with three tools,
so MCP's request/response shape never buffers a whole utterance:

    asr_start(...)                -> {"session": id}   opens a warm Flux WebSocket
    asr_push(session, pcm_base64) -> {"ok": true}      ~60-80 ms of 16-bit PCM, fire and forget
    asr_events(session, wait_ms)  -> {"events": [...]} long-poll; returns the moment an event exists
    asr_finalize(session)         -> sends 500 ms of silence so Flux commits the turn
    asr_stop(session)             -> CloseStream and tear down

Events are Flux TurnInfo events passed through almost verbatim:
    StartOfTurn, Update, EagerEndOfTurn, TurnResumed, EndOfTurn
plus server-side ones: Connected, Reconnected, Error.

Transport: stdio (spawned by the assistant). Never print to stdout.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import websockets
from loguru import logger
from mcp.server.mcpserver import MCPServer

from mcp_servers.common import env, env_float, env_int, setup

setup("asr")

DEEPGRAM_API_KEY = env("DEEPGRAM_API_KEY")
FLUX_URL = env("DEEPGRAM_FLUX_URL", "wss://api.deepgram.com/v2/listen")
FLUX_MODEL = env("DEEPGRAM_FLUX_MODEL", "flux-general-en")
DEFAULT_EOT = env_float("FLUX_EOT_THRESHOLD", 0.7)
DEFAULT_EAGER_EOT = env_float("FLUX_EAGER_EOT_THRESHOLD", 0.5)
DEFAULT_EOT_TIMEOUT_MS = env_int("FLUX_EOT_TIMEOUT_MS", 5000)
MAX_QUEUE = 500
# While the client's gate is closed no audio arrives. Deepgram closes a streaming socket
# after ~10 s without audio (documented for v1; Flux undocumented), and reconnecting costs
# ~1 s, so a short burst of silence is sent after KEEPALIVE_SECS of no audio.
# FLUX_KEEPALIVE_MODE: silence (default, always accepted) | message (JSON KeepAlive) | off
KEEPALIVE_SECS = env_float("FLUX_KEEPALIVE_SECS", 5.0)
KEEPALIVE_MODE = (env("FLUX_KEEPALIVE_MODE", "silence") or "silence").lower()
KEEPALIVE_SILENCE_MS = 100

mcp = MCPServer("asr", instructions="Streaming speech-to-text (Deepgram Flux) for the ME3 voice assistant.")


@dataclass
class Session:
    id: str
    sample_rate: int
    eot_threshold: float
    eager_eot_threshold: float | None
    eot_timeout_ms: int
    ws: Any = None
    reader: asyncio.Task | None = None
    events: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=MAX_QUEUE))
    connect_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    closed: bool = False
    bytes_pushed: int = 0
    last_push: float = 0.0
    last_send: float = 0.0
    reconnects: int = 0
    conn_id: int = 0  # increments on every (re)connect; Flux word times restart at 0
    conn_bytes: int = 0  # audio bytes sent on the current connection (incl. keepalive silence)
    keepalive_bytes: int = 0
    keepalive_task: asyncio.Task | None = None
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def url(self) -> str:
        params = [
            f"model={FLUX_MODEL}",
            f"encoding=linear16",
            f"sample_rate={self.sample_rate}",
            f"eot_threshold={self.eot_threshold}",
            f"eot_timeout_ms={self.eot_timeout_ms}",
        ]
        if self.eager_eot_threshold is not None:
            params.append(f"eager_eot_threshold={self.eager_eot_threshold}")
        return f"{FLUX_URL}?{'&'.join(params)}"


SESSIONS: dict[str, Session] = {}


def _put(session: Session, event: dict[str, Any]) -> None:
    event.setdefault("t", time.time())
    try:
        session.events.put_nowait(event)
    except asyncio.QueueFull:
        # Drop the oldest interim update rather than block the reader.
        try:
            session.events.get_nowait()
        except asyncio.QueueEmpty:
            pass
        session.events.put_nowait(event)


async def _reader(session: Session, ws, conn_id: int) -> None:
    """Forward Flux messages into the session event queue."""
    try:
        async for raw in ws:
            if isinstance(raw, bytes):
                continue
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            mtype = msg.get("type")
            if mtype == "TurnInfo":
                _put(
                    session,
                    {
                        "event": msg.get("event"),
                        "transcript": msg.get("transcript", "") or "",
                        "turn_index": msg.get("turn_index"),
                        "end_of_turn_confidence": msg.get("end_of_turn_confidence"),
                        "audio_window_start": msg.get("audio_window_start"),
                        "audio_window_end": msg.get("audio_window_end"),
                        "words": msg.get("words"),
                        "trigger": msg.get("trigger"),
                        "conn": conn_id,
                    },
                )
            elif mtype == "Connected":
                _put(session, {"event": "Connected", "request_id": msg.get("request_id")})
            elif mtype in ("Error", "FatalError"):
                logger.error(f"Flux error: {msg}")
                _put(session, {"event": "Error", "message": str(msg)})
            else:
                logger.debug(f"Flux message: {mtype}")
    except websockets.ConnectionClosed as e:
        logger.warning(f"Flux connection closed ({e.code}); will reconnect on next push")
    except Exception as e:  # noqa: BLE001
        logger.error(f"Flux reader failed: {e}")
    finally:
        if session.ws is ws:
            session.ws = None


async def _connect(session: Session) -> None:
    async with session.connect_lock:
        if session.ws is not None or session.closed:
            return
        if not DEEPGRAM_API_KEY:
            raise RuntimeError("DEEPGRAM_API_KEY is not set (see .env.example)")
        t0 = time.perf_counter()
        ws = await websockets.connect(
            session.url(),
            additional_headers={"Authorization": f"Token {DEEPGRAM_API_KEY}"},
            max_size=None,
            ping_interval=20,
            ping_timeout=20,
        )
        session.ws = ws
        session.conn_id += 1
        session.conn_bytes = 0
        session.last_send = time.time()
        session.reader = asyncio.create_task(_reader(session, ws, session.conn_id))
        if session.keepalive_task is None and KEEPALIVE_MODE != "off":
            session.keepalive_task = asyncio.create_task(_keepalive(session))
        was_reconnect = session.reconnects > 0
        session.reconnects += 1
        logger.info(f"Flux connected in {(time.perf_counter() - t0) * 1000:.0f} ms ({session.url()})")
        if was_reconnect:
            _put(session, {"event": "Reconnected"})


async def _send_audio(s: Session, audio: bytes) -> tuple[int, float]:
    """Send audio on the current connection; returns (conn_id, stream offset in seconds before it)."""
    async with s.send_lock:
        if s.ws is None:
            await _connect(s)
        try:
            await s.ws.send(audio)
        except websockets.ConnectionClosed:
            s.ws = None
            await _connect(s)
            await s.ws.send(audio)
        offset = s.conn_bytes / (s.sample_rate * 2)
        s.conn_bytes += len(audio)
        s.last_send = time.time()
        return s.conn_id, offset


async def _keepalive(s: Session) -> None:
    silence = b"\x00" * (s.sample_rate * 2 * KEEPALIVE_SILENCE_MS // 1000)
    while not s.closed:
        await asyncio.sleep(1.0)
        if s.ws is None or time.time() - s.last_send < KEEPALIVE_SECS:
            continue
        try:
            if KEEPALIVE_MODE == "message":
                async with s.send_lock:
                    await s.ws.send(json.dumps({"type": "KeepAlive"}))
                    s.last_send = time.time()
            else:
                await _send_audio(s, silence)
                s.keepalive_bytes += len(silence)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Flux keepalive failed: {e}")


def _get(session_id: str) -> Session:
    session = SESSIONS.get(session_id)
    if session is None:
        raise ValueError(f"unknown ASR session {session_id!r}; call asr_start first")
    return session


@mcp.tool()
async def asr_start(
    sample_rate: int = 16000,
    eot_threshold: float | None = None,
    eager_eot_threshold: float | None = None,
    eot_timeout_ms: int | None = None,
) -> dict[str, Any]:
    """Open a streaming ASR session (warm Deepgram Flux WebSocket). Returns {"session": id}."""
    session = Session(
        id=uuid.uuid4().hex[:8],
        sample_rate=sample_rate,
        eot_threshold=eot_threshold if eot_threshold is not None else DEFAULT_EOT,
        eager_eot_threshold=(
            eager_eot_threshold if eager_eot_threshold is not None else DEFAULT_EAGER_EOT
        ),
        eot_timeout_ms=eot_timeout_ms if eot_timeout_ms is not None else DEFAULT_EOT_TIMEOUT_MS,
    )
    SESSIONS[session.id] = session
    await _connect(session)
    return {"session": session.id, "model": FLUX_MODEL, "sample_rate": sample_rate}


@mcp.tool()
async def asr_push(session: str, pcm_base64: str) -> dict[str, Any]:
    """Push a chunk of 16-bit mono PCM (base64) into the session. Call every 60-80 ms while speech is possible."""
    t_recv = time.time()
    s = _get(session)
    audio = base64.b64decode(pcm_base64)
    conn, offset = await _send_audio(s, audio)
    s.bytes_pushed += len(audio)
    s.last_push = time.time()
    # conn + offset let the client map Flux word times (seconds of audio on this
    # connection) back to the wall-clock time each chunk was captured.
    return {"ok": True, "conn": conn, "offset": round(offset, 4), "t_recv": t_recv}


@mcp.tool()
async def asr_events(session: str, wait_ms: int = 1000) -> dict[str, Any]:
    """Long-poll for transcript events. Returns as soon as one event exists, or after wait_ms with an empty list."""
    s = _get(session)
    events: list[dict[str, Any]] = []
    try:
        first = await asyncio.wait_for(s.events.get(), timeout=max(wait_ms, 0) / 1000)
        events.append(first)
    except asyncio.TimeoutError:
        return {"events": events}
    while True:
        try:
            events.append(s.events.get_nowait())
        except asyncio.QueueEmpty:
            break
    return {"events": events}


@mcp.tool()
async def asr_finalize(session: str) -> dict[str, Any]:
    """Nudge Flux to commit the current turn by sending 500 ms of silence."""
    s = _get(session)
    if s.ws is None:
        return {"ok": False, "reason": "not connected"}
    silence = b"\x00" * (s.sample_rate * 2 // 2)  # 0.5 s of 16-bit mono
    await _send_audio(s, silence)
    return {"ok": True}


@mcp.tool()
async def asr_stop(session: str) -> dict[str, Any]:
    """Close the session and its Flux connection."""
    s = SESSIONS.pop(session, None)
    if s is None:
        return {"ok": True}
    s.closed = True
    if s.keepalive_task:
        s.keepalive_task.cancel()
    if s.ws is not None:
        try:
            await s.ws.send(json.dumps({"type": "CloseStream"}))
            await asyncio.wait_for(s.ws.close(), timeout=2)
        except Exception:  # noqa: BLE001
            pass
    if s.reader:
        s.reader.cancel()
    return {"ok": True, "bytes_pushed": s.bytes_pushed, "keepalive_bytes": s.keepalive_bytes}


@mcp.tool()
async def asr_health() -> dict[str, Any]:
    """Report server status: provider, model, open sessions."""
    return {
        "provider": "deepgram-flux",
        "model": FLUX_MODEL,
        "api_key_set": bool(DEEPGRAM_API_KEY),
        "sessions": {
            sid: {
                "connected": s.ws is not None,
                "bytes_pushed": s.bytes_pushed,
                "keepalive_bytes": s.keepalive_bytes,
                "connections": s.conn_id,
                "queued": s.events.qsize(),
            }
            for sid, s in SESSIONS.items()
        },
    }


if __name__ == "__main__":
    mcp.run(transport="stdio")
