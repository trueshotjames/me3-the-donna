"""Groq LLM service that times every request: sent, first chunk, first token, each token, end.

Wraps the chunk stream returned by ``get_chat_completions`` so Pipecat's own streaming
code is untouched. Each call is one entry in the turn record (see turn_metrics.py).

Also:
- reads Groq's rate-limit headers on every response (x-ratelimit-remaining-tokens ...)
  and records them on the call; a 429 is logged with its retry-after and reported to
  ``on_rate_limited`` so the assistant can say "one moment" instead of going silent
  (the OpenAI SDK retries a 429 by itself, after the retry-after wait, up to 2 times);
- hands the full history to ``on_messages`` (long-term memory), then trims the conversation to
  the last ``history_messages`` messages before each call,
  because every call re-sends the whole history and the free tier counts tokens per minute.
"""

from __future__ import annotations

import logging
import time
from typing import Awaitable, Callable

from loguru import logger
from pipecat.services.groq.llm import GroqLLMService

from assistant.turn_metrics import METRICS

RateLimitHook = Callable[[float, dict], Awaitable[None]]  # (retry_after_secs, headers)


class _TimedStream:
    def __init__(self, stream, call: dict):
        self._stream = stream
        self._call = call

    def __aiter__(self):
        return self._iterate()

    async def _iterate(self):
        call = self._call
        try:
            async for chunk in self._stream:
                now = time.time()
                usage = getattr(chunk, "usage", None)
                if usage is not None and getattr(usage, "completion_tokens", None):
                    call["completion_tokens"] = usage.completion_tokens
                    call["prompt_tokens"] = getattr(usage, "prompt_tokens", None)
                choices = getattr(chunk, "choices", None)
                if choices:
                    call.setdefault("t_first_chunk", now)
                    delta = choices[0].delta
                    if delta is not None:
                        if getattr(delta, "content", None):
                            call.setdefault("t_first_content", now)
                            call["content_t"].append(now)
                            call["chunks"] += 1
                            call["chars"] += len(delta.content)
                        if getattr(delta, "tool_calls", None):
                            call.setdefault("t_first_tool", now)
                            call["t_tool_done"] = now
                yield chunk
        finally:
            call["t_end"] = time.time()

    async def close(self):
        if hasattr(self._stream, "close"):
            await self._stream.close()
        elif hasattr(self._stream, "aclose"):
            await self._stream.aclose()


def trim_history(messages: list, keep: int) -> list:
    """Keep system messages and the last ``keep`` conversation messages, starting at a user turn.

    Starting at a user message keeps every assistant tool call next to its tool result;
    the API rejects a ``tool`` message whose call was cut off.
    """
    if keep <= 0:
        return messages
    system = [m for m in messages if m.get("role") == "system"]
    rest = [m for m in messages if m.get("role") != "system"]
    if len(rest) <= keep:
        return messages
    tail = rest[-keep:]
    while tail and tail[0].get("role") != "user":
        tail.pop(0)
    return system + tail


class TimedGroqLLMService(GroqLLMService):
    def __init__(
        self,
        *args,
        history_messages: int = 12,
        on_rate_limited: RateLimitHook | None = None,
        on_messages=None,  # callable(messages): sees the full history before the trim (assistant/memory.py)
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self._history_messages = history_messages
        self._on_messages = on_messages
        self._on_rate_limited = on_rate_limited
        self._pending_call: dict | None = None
        self.rate_limit: dict = {}  # last headers seen: remaining tokens/requests, reset times
        self.waits: list[dict] = []  # every 429: {t, retry_after}
        _show_sdk_retries()
        try:
            self._client._client.event_hooks["response"].append(self._on_http_response)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"{self}: could not hook Groq responses for rate-limit headers: {e!r}")

    async def _on_http_response(self, response) -> None:
        h = response.headers
        info = {
            k: _num(h.get(src))
            for k, src in (
                ("remaining_tokens", "x-ratelimit-remaining-tokens"),
                ("limit_tokens", "x-ratelimit-limit-tokens"),
                ("remaining_requests", "x-ratelimit-remaining-requests"),
                ("limit_requests", "x-ratelimit-limit-requests"),
            )
        }
        info["reset_tokens"] = h.get("x-ratelimit-reset-tokens")
        info["reset_requests"] = h.get("x-ratelimit-reset-requests")
        info["status"] = response.status_code
        self.rate_limit = info
        call = self._pending_call
        if call is not None:
            call["rate_limit"] = info
        if response.status_code == 429:
            retry_after = _num(h.get("retry-after")) or 0.0
            wait = {"t": time.time(), "retry_after": retry_after}
            self.waits.append(wait)
            if call is not None:
                call.setdefault("waits", []).append(wait)
            logger.warning(
                f"{self}: Groq 429 rate limit, SDK retries in {retry_after:.1f} s"
                f" (tokens left {info['remaining_tokens']}/{info['limit_tokens']} per minute,"
                f" resets in {info['reset_tokens']}; requests left {info['remaining_requests']}/{info['limit_requests']})"
            )
            if self._on_rate_limited:
                try:
                    await self._on_rate_limited(retry_after, info)
                except Exception as e:  # noqa: BLE001
                    logger.debug(f"on_rate_limited failed: {e!r}")
        elif response.status_code >= 400:
            logger.warning(f"{self}: Groq HTTP {response.status_code}")

    async def get_chat_completions(self, context):
        msgs = context.get_messages() if hasattr(context, "get_messages") else context.messages
        if self._on_messages:
            try:
                self._on_messages(list(msgs))
            except Exception as e:  # noqa: BLE001
                logger.debug(f"{self}: on_messages hook failed: {e!r}")
        if self._history_messages:
            trimmed = trim_history(list(msgs), self._history_messages)
            if len(trimmed) < len(msgs):
                context.set_messages(trimmed)
                logger.debug(f"{self}: history trimmed to {len(trimmed)} messages")
        call = METRICS.llm_call()
        self._pending_call = call
        try:
            stream = await super().get_chat_completions(context)
        except Exception as e:
            call["error"] = repr(e)[:200]
            call["t_end"] = time.time()
            raise
        finally:
            self._pending_call = None
        return _TimedStream(stream, call)


def _num(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


_sdk_logging_installed = False


def _show_sdk_retries() -> None:
    """Route the OpenAI SDK's "Retrying request in N seconds" messages into our log."""
    global _sdk_logging_installed
    if _sdk_logging_installed:
        return
    _sdk_logging_installed = True

    class _ToLoguru(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if "Retrying" in record.getMessage():
                logger.warning(f"openai sdk: {record.getMessage()}")

    lg = logging.getLogger("openai")
    lg.setLevel(logging.INFO)
    lg.addHandler(_ToLoguru())
