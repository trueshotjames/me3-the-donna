"""Client for the Hermes Agent API server (http://127.0.0.1:8642 by default).

Hermes is the "capable" brain. The pipeline's fast model stays the voice; this client
only starts background runs and reads their progress and result:

    POST /v1/runs                      {"input": task, "session_id", "instructions"} -> run_id
    GET  /v1/runs/{id}/events          SSE: tool.started, tool.completed, message.delta, run.completed ...
    GET  /v1/runs/{id}                 status: running | completed | failed | cancelled | interrupted
    POST /v1/runs/{id}/stop            cancel

Older Hermes builds without /v1/runs fall back to a streamed POST /v1/chat/completions held
open for the length of the task (same result, only no server-side status to poll).

Every request carries ``Authorization: Bearer <API_SERVER_KEY>``; Hermes requires the key
even on loopback.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable

import httpx
from loguru import logger

TERMINAL = {"completed", "failed", "cancelled", "interrupted"}


@dataclass
class RunEvent:
    kind: str  # tool.started | tool.completed | message.delta | run.completed | ...
    data: dict[str, Any] = field(default_factory=dict)
    t: float = field(default_factory=time.time)


class HermesError(RuntimeError):
    pass


class HermesClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        self._client = httpx.AsyncClient(
            base_url=self.base_url, headers=self._headers, timeout=timeout, transport=transport
        )
        self.has_runs: bool | None = None  # None = not probed yet

    async def close(self) -> None:
        await self._client.aclose()

    # --------------------------------------------------------------- health

    async def health(self) -> dict[str, Any]:
        r = await self._client.get("/health")
        r.raise_for_status()
        return r.json()

    async def models(self) -> list[str]:
        r = await self._client.get("/v1/models")
        r.raise_for_status()
        data = r.json()
        return [m.get("id", "?") for m in data.get("data", [])]

    async def check(self) -> dict[str, Any]:
        """Health plus model name, raising HermesError with a plain message when unreachable."""
        try:
            health = await self.health()
            models = await self.models()
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (401, 403):
                raise HermesError("Hermes rejected the key (HERMES_API_KEY must equal API_SERVER_KEY in Hermes's .env)") from e
            raise HermesError(f"Hermes answered {e.response.status_code} on {e.request.url.path}") from e
        except httpx.HTTPError as e:
            raise HermesError(f"Hermes not reachable at {self.base_url}: {e.__class__.__name__}") from e
        return {"status": health.get("status"), "models": models}

    # ----------------------------------------------------------------- runs

    async def start_run(self, task: str, *, session_id: str, instructions: str | None = None) -> str:
        """Start a background run. Returns the run id (falls back to a local id for chat-only servers)."""
        body: dict[str, Any] = {"input": task, "session_id": session_id}
        if instructions:
            body["instructions"] = instructions
        if self.has_runs is not False:
            r = await self._client.post("/v1/runs", json=body, headers={"X-Hermes-Session-Id": session_id})
            if r.status_code == 404:
                self.has_runs = False
                logger.warning("Hermes has no /v1/runs; using a held-open chat completion instead")
            else:
                r.raise_for_status()
                self.has_runs = True
                return str(r.json()["run_id"])
        return "chat:" + session_id

    async def events(
        self, run_id: str, *, task: str = "", session_id: str = "", instructions: str | None = None
    ) -> AsyncIterator[RunEvent]:
        """Yield events until the run ends. For chat-only servers, streams a chat completion."""
        if run_id.startswith("chat:"):
            async for ev in self._chat_events(task, session_id, instructions):
                yield ev
            return
        async with self._client.stream(
            "GET", f"/v1/runs/{run_id}/events", headers={"Accept": "text/event-stream"}, timeout=None
        ) as r:
            r.raise_for_status()
            async for ev in _sse(r.aiter_lines()):
                yield ev
                if ev.kind in ("run.completed", "run.interrupted", "run.failed", "run.cancelled"):
                    return

    async def get_run(self, run_id: str) -> dict[str, Any]:
        if run_id.startswith("chat:"):
            return {"status": "running"}
        r = await self._client.get(f"/v1/runs/{run_id}")
        r.raise_for_status()
        return r.json()

    async def stop_run(self, run_id: str) -> None:
        if run_id.startswith("chat:"):
            return
        try:
            await self._client.post(f"/v1/runs/{run_id}/stop")
        except httpx.HTTPError as e:
            logger.warning(f"could not stop Hermes run {run_id}: {e!r}")

    # ---------------------------------------------------------- chat fallback

    async def _chat_events(self, task: str, session_id: str, instructions: str | None) -> AsyncIterator[RunEvent]:
        messages = []
        if instructions:
            messages.append({"role": "system", "content": instructions})
        messages.append({"role": "user", "content": task})
        body = {"model": "hermes-agent", "messages": messages, "stream": True}
        text = []
        async with self._client.stream(
            "POST",
            "/v1/chat/completions",
            json=body,
            headers={"X-Hermes-Session-Id": session_id, "Accept": "text/event-stream"},
            timeout=None,
        ) as r:
            r.raise_for_status()
            async for ev in _sse(r.aiter_lines()):
                if ev.kind == "hermes.tool.progress":
                    yield RunEvent("tool.started", ev.data)
                    continue
                choices = ev.data.get("choices") or []
                if choices:
                    delta = (choices[0].get("delta") or {}).get("content")
                    if delta:
                        text.append(delta)
                        yield RunEvent("message.delta", {"delta": delta})
        yield RunEvent("run.completed", {"output": "".join(text)})


async def _sse(lines: AsyncIterator[str]) -> AsyncIterator[RunEvent]:
    """Parse server-sent events: 'event:' names, 'data:' JSON, ': keepalive' comments."""
    name: str | None = None
    data: list[str] = []
    async for raw in lines:
        line = raw.rstrip("\r")
        if line == "":
            if data:
                payload = "\n".join(data)
                if payload.strip() == "[DONE]":
                    return
                try:
                    obj = json.loads(payload)
                except json.JSONDecodeError:
                    obj = {"raw": payload}
                kind = name or obj.get("event") or obj.get("type") or obj.get("object") or "message"
                yield RunEvent(kind, obj if isinstance(obj, dict) else {"data": obj})
            name, data = None, []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            name = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].lstrip())


def extract_output(ev: RunEvent) -> str | None:
    """Final text from a run.completed event, whatever the field is called."""
    d = ev.data
    for key in ("output", "text", "content", "result"):
        v = d.get(key)
        if isinstance(v, str) and v.strip():
            return v
        if isinstance(v, list):  # Responses-style output items
            parts = [p.get("text", "") for item in v for p in item.get("content", []) if isinstance(p, dict)]
            if any(parts):
                return "".join(parts)
    run = d.get("run") if isinstance(d.get("run"), dict) else None
    if run:
        return extract_output(RunEvent(ev.kind, run))
    return None


ProgressHook = Callable[[RunEvent], Any]


async def run_task(
    client: HermesClient,
    task: str,
    *,
    session_id: str,
    instructions: str | None = None,
    on_event: ProgressHook | None = None,
) -> tuple[str, str]:
    """Start a run and wait for its text. Returns (run_id, output). Raises HermesError on failure."""
    run_id = await client.start_run(task, session_id=session_id, instructions=instructions)
    text: list[str] = []
    final: str | None = None
    status = "completed"
    try:
        async for ev in client.events(run_id, task=task, session_id=session_id, instructions=instructions):
            if on_event:
                try:
                    res = on_event(ev)
                    if asyncio.iscoroutine(res):
                        await res
                except Exception as e:  # noqa: BLE001
                    logger.debug(f"hermes progress hook failed: {e!r}")
            if ev.kind == "message.delta":
                delta = ev.data.get("delta") or ev.data.get("text") or ""
                if isinstance(delta, str):
                    text.append(delta)
            elif ev.kind == "run.completed":
                final = extract_output(ev)
            elif ev.kind in ("run.interrupted", "run.failed", "run.cancelled"):
                status = ev.kind.split(".", 1)[1]
    except httpx.HTTPError as e:
        raise HermesError(f"Hermes stream failed: {e!r}") from e
    if final is None and not run_id.startswith("chat:"):
        # The event stream may not carry the text; ask for the run record.
        try:
            rec = await client.get_run(run_id)
            status = rec.get("status", status)
            final = extract_output(RunEvent("run", rec))
        except httpx.HTTPError:
            pass
    output = (final or "".join(text)).strip()
    if status != "completed" and not output:
        raise HermesError(f"Hermes run {status}")
    if not output:
        raise HermesError("Hermes returned no text")
    return run_id, output
