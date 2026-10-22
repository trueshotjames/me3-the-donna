"""A small MCP client used by the custom STT/TTS services and the scheduler.

Pipecat's own MCPClient is for LLM tools. The ASR and TTS servers are called
directly and often (every 60-80 ms while speaking), so this wrapper keeps one
ClientSession open for the life of the pipeline and lets any task call tools
on it. The MCP SDK binds a session to the task that opened it, so an owner
task holds the transport open until close() is called.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import timedelta
from typing import Any

from loguru import logger
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import AudioContent, CallToolResult, TextContent

from assistant.config import ROOT


def stdio_params(module: str, *args: str) -> StdioServerParameters:
    """Spawn `python -m <module>` from the project root with the same interpreter."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONUNBUFFERED"] = "1"
    return StdioServerParameters(command=sys.executable, args=["-m", module, *args], env=env, cwd=str(ROOT))


class MCPBridge:
    def __init__(self, name: str, *, stdio: StdioServerParameters | None = None, url: str | None = None):
        if (stdio is None) == (url is None):
            raise ValueError("give exactly one of stdio= or url=")
        self.name = name
        self._stdio = stdio
        self._url = url
        self._session: ClientSession | None = None
        self._owner: asyncio.Task | None = None
        self._ready: asyncio.Future | None = None
        self._closing = asyncio.Event()

    @property
    def connected(self) -> bool:
        return self._session is not None

    async def start(self, timeout: float = 30.0) -> None:
        if self._session is not None:
            return
        loop = asyncio.get_running_loop()
        self._ready = loop.create_future()
        self._closing = asyncio.Event()
        self._owner = asyncio.create_task(self._run(), name=f"mcp-bridge-{self.name}")
        await asyncio.wait_for(self._ready, timeout=timeout)

    async def _run(self) -> None:
        try:
            if self._stdio is not None:
                transport = stdio_client(self._stdio, errlog=sys.stderr)
            else:
                transport = streamable_http_client(self._url)
            async with transport as streams:
                read, write = streams[0], streams[1]
                async with ClientSession(read, write, read_timeout_seconds=30) as session:
                    await session.initialize()
                    self._session = session
                    logger.info(f"MCP bridge '{self.name}' connected")
                    if not self._ready.done():
                        self._ready.set_result(True)
                    await self._closing.wait()
        except Exception as e:  # noqa: BLE001
            logger.error(f"MCP bridge '{self.name}' failed: {e!r}")
            if self._ready and not self._ready.done():
                self._ready.set_exception(e)
        finally:
            self._session = None
            logger.info(f"MCP bridge '{self.name}' closed")

    async def close(self) -> None:
        self._closing.set()
        if self._owner:
            try:
                await asyncio.wait_for(self._owner, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._owner.cancel()
            self._owner = None

    async def call(self, tool: str, arguments: dict[str, Any] | None = None, *, timeout: float = 30.0) -> CallToolResult:
        if self._session is None:
            raise RuntimeError(f"MCP bridge '{self.name}' is not connected")
        return await self._session.call_tool(tool, arguments or {}, read_timeout_seconds=timeout)

    async def call_json(self, tool: str, arguments: dict[str, Any] | None = None, *, timeout: float = 30.0) -> Any:
        """Call a tool and parse its text content as JSON (or return the raw text)."""
        result = await self.call(tool, arguments, timeout=timeout)
        return parse_result(result)

    async def list_tools(self) -> list[str]:
        if self._session is None:
            raise RuntimeError(f"MCP bridge '{self.name}' is not connected")
        result = await self._session.list_tools()
        return [t.name for t in result.tools]


def parse_result(result: CallToolResult) -> Any:
    """Return the tool's JSON value: structured content, else the first text block (JSON-decoded when possible)."""
    sc = getattr(result, "structured_content", None) or getattr(result, "structuredContent", None)
    if sc:
        value = sc.get("result", sc) if isinstance(sc, dict) and set(sc) == {"result"} else sc
        return _maybe_json(value)
    for block in result.content:
        if isinstance(block, TextContent):
            return _maybe_json(block.text)
    return None


def _maybe_json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return value
    return value


def audio_blocks(result: CallToolResult) -> list[AudioContent]:
    return [b for b in result.content if isinstance(b, AudioContent)]
