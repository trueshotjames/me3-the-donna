"""Local WebSocket hub that feeds the UI (see assistant/EVENTS.md).

Backend -> UI: one JSON object per message, always with "type" and "ts" (unix ms).
UI -> backend: commands such as alarm_stop, reminder_done, mute, interrupt.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Awaitable, Callable

import websockets
from loguru import logger
from websockets.asyncio.server import ServerConnection, serve

CommandHandler = Callable[[dict[str, Any]], Awaitable[None]]


class UIBus:
    def __init__(self, host: str = "127.0.0.1", port: int = 8765):
        self.host = host
        self.port = port
        self._clients: set[ServerConnection] = set()
        self._server = None
        self._handler: CommandHandler | None = None
        self._snapshot_provider: Callable[[], Awaitable[list[dict[str, Any]]]] | None = None
        self._extra_snapshots: list[Callable[[], Awaitable[list[dict[str, Any]]]]] = []
        self._commands: dict[str, CommandHandler] = {}  # command type -> handler, tried before on_command's
        self.last_state = "idle"
        self._loop: asyncio.AbstractEventLoop | None = None

    def on_command(self, handler: CommandHandler) -> None:
        self._handler = handler

    def snapshot_provider(self, provider: Callable[[], Awaitable[list[dict[str, Any]]]]) -> None:
        """Messages to send to every client that connects (state and the reminder/alarm lists)."""
        self._snapshot_provider = provider

    def add_snapshot_provider(self, provider: Callable[[], Awaitable[list[dict[str, Any]]]]) -> None:
        """More messages for a new client, sent after the main snapshot (chat history, network status)."""
        self._extra_snapshots.append(provider)

    def add_command(self, type_: str, handler: CommandHandler) -> None:
        """Handle one command type here instead of passing it to the on_command handler."""
        self._commands[type_] = handler

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._server = await serve(self._serve, self.host, self.port, ping_interval=20, max_size=1 << 20)
        logger.info(f"UI WebSocket listening on ws://{self.host}:{self.port}")

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    @property
    def clients(self) -> int:
        return len(self._clients)

    async def _serve(self, ws: ServerConnection) -> None:
        self._clients.add(ws)
        logger.info(f"UI connected ({len(self._clients)} client(s))")
        try:
            await self._send(ws, {"type": "hello", "ts": _ms(), "version": 1})
            await self._send(ws, {"type": "state", "ts": _ms(), "state": self.last_state})
            for provider in ([self._snapshot_provider] if self._snapshot_provider else []) + self._extra_snapshots:
                try:
                    for msg in await provider():
                        await self._send(ws, msg)
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"snapshot for new UI client failed: {e}")
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    await self._send(ws, {"type": "error", "ts": _ms(), "message": "invalid JSON"})
                    continue
                if not isinstance(msg, dict) or "type" not in msg:
                    continue
                logger.debug(f"UI command: {msg}")
                handler = self._commands.get(msg["type"], self._handler)
                if handler:
                    try:
                        await handler(msg)
                    except Exception as e:  # noqa: BLE001
                        logger.error(f"UI command {msg.get('type')} failed: {e!r}")
                        await self._send(ws, {"type": "error", "ts": _ms(), "message": f"{msg.get('type')}: {e}"})
        except websockets.ConnectionClosed:
            pass
        finally:
            self._clients.discard(ws)
            logger.info(f"UI disconnected ({len(self._clients)} client(s))")

    async def _send(self, ws: ServerConnection, msg: dict[str, Any]) -> None:
        try:
            await ws.send(json.dumps(msg, ensure_ascii=False))
        except websockets.ConnectionClosed:
            self._clients.discard(ws)

    def emit(self, type_: str, **fields: Any) -> None:
        """Fire-and-forget broadcast; safe to call from any coroutine on the loop."""
        msg = {"type": type_, "ts": _ms(), **fields}
        if type_ == "state":
            self.last_state = fields.get("state", self.last_state)
        if not self._clients:
            return
        data = json.dumps(msg, ensure_ascii=False)
        for ws in list(self._clients):
            asyncio.ensure_future(self._raw_send(ws, data))

    async def _raw_send(self, ws: ServerConnection, data: str) -> None:
        try:
            await ws.send(data)
        except websockets.ConnectionClosed:
            self._clients.discard(ws)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"UI send failed: {e}")


def _ms() -> int:
    return int(time.time() * 1000)
