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

import asyncio
import logging
import os
import time
from typing import Awaitable, Callable

from loguru import logger
from pipecat.services.groq.llm import GroqLLMService

from assistant.turn_metrics import METRICS
from assistant.voice_gate import FLAGS

RateLimitHook = Callable[[float, dict], Awaitable[None]]  # (retry_after_secs, headers)


WAIT_TOKEN = "<wait"
# v6 (LLM_EOT): added to the turn note of a speculative request only (an early reply to a turn that may go on)
WAIT_NOTE = (
    "provisional: the user paused and may not be finished; if their message is clearly cut off mid-sentence, reply "
    "with exactly <wait/> and nothing else, otherwise answer normally"
)
# v6 smoke fix: the offer as its own system message after the user's message (WAIT_NOTE is no longer sent)
WAIT_SYSTEM = (
    "The user paused here and may not be finished. Only if the message above is cut off mid-sentence, output <wait/>."
)


class _TimedStream:
    def __init__(self, stream, call: dict, on_verdict=None):
        self._stream = stream
        self._call = call
        self._on_verdict = on_verdict  # v6: async callable("go" | "wait") on the first output of a speculative call
        self._buf = ""

    def __aiter__(self):
        return self._iterate()

    def _verdict(self, kind: str) -> None:
        cb, self._on_verdict = self._on_verdict, None
        self._call["verdict"] = kind
        self._call["t_verdict"] = time.time()
        if cb is not None:
            asyncio.get_running_loop().create_task(cb(kind))

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
                            call["text"] = call.get("text", "") + delta.content
                            if self._on_verdict is not None:
                                self._buf += delta.content
                                s = self._buf.lstrip().lower()
                                if s.startswith(WAIT_TOKEN):
                                    self._verdict("wait")
                                elif s and (len(s) >= len(WAIT_TOKEN) or not WAIT_TOKEN.startswith(s)):
                                    self._verdict("go")
                        if getattr(delta, "tool_calls", None):
                            call.setdefault("t_first_tool", now)
                            call["t_tool_done"] = now
                            if self._on_verdict is not None:
                                # v6 smoke (t6): not "go". Pipecat cancels a speculative inference that wants a tool;
                                # ending the turn here raced that cancel, so the turn ended with no inference left
                                # to answer it and the thermostat call landed in the next turn.
                                self._verdict("tool")
                yield chunk
        finally:
            call["t_end"] = time.time()
            if os.getenv("LLM_DUMP"):
                import json

                with open(os.environ["LLM_DUMP"], "a", encoding="utf-8") as f:
                    f.write(json.dumps({"response": call.get("text", ""), "speculative": call.get("speculative"),
                                        "verdict": call.get("verdict"), "t_end": call["t_end"]}) + "\n")

    async def close(self):
        if hasattr(self._stream, "close"):
            await self._stream.close()
        elif hasattr(self._stream, "aclose"):
            await self._stream.aclose()


class _Prefixed:
    """v3: a chunk stream whose first chunks were already read (by the first-token timeout)."""

    def __init__(self, early: list, it, stream):
        self._early = early
        self._it = it
        self._stream = stream

    def __aiter__(self):
        return self._iterate()

    async def _iterate(self):
        for chunk in self._early:
            yield chunk
        async for chunk in self._it:
            yield chunk

    async def close(self):
        if hasattr(self._stream, "close"):
            await self._stream.close()


class _Canned:
    """v5b: a one-chunk reply made here instead of by the LLM (the engagement check's <pass/>)."""

    def __init__(self, text: str):
        from openai.types.chat import ChatCompletionChunk

        self._chunk = ChatCompletionChunk.model_validate({
            "id": "canned", "object": "chat.completion.chunk", "created": int(time.time()), "model": "engagement-check",
            "choices": [{"index": 0, "delta": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        })

    def __aiter__(self):
        return self._iterate()

    async def _iterate(self):
        yield self._chunk

    async def close(self):
        pass


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
        turn_note: Callable[[], str | None] | None = None,  # v2: per-turn note (time, voice hint) for the request only
        first_token_timeout_secs: float = 0.0,  # v3: no first token by then -> one retry with the providers swapped
        engagement: Callable[[str], str | None] | None = None,  # v5: assistant/engagement.py check on the user text
        hedge_secs: float = 0.0,  # v7: no first token by then -> a second request, providers swapped; first one wins
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self._turn_note = turn_note
        self._engagement = engagement
        # v6 (LLM_EOT): async callable(kind, user_text) told whether a speculative reply starts with <wait/>;
        # set by main.py to the STT service's llm_verdict. wait_note: offer <wait/> in speculative requests.
        self.on_verdict = None
        self.wait_note = False
        self.spoken = None  # v6 (SPOKEN_HISTORY): assistant.spoken.SpokenHistory, set by main.py
        self.cut_check = None  # v6 (EOT_CHECK=separate): assistant.eot_check.CutOffCheck, set by main.py
        self._cut_tasks: set = set()
        self._first_token_timeout = first_token_timeout_secs
        self._swap_providers = False
        self._swap_once = False
        self._hedge_secs = hedge_secs
        self.hedges = {"fired": 0, "won_by_hedge": 0, "won_by_first": 0, "both_failed": 0}
        self.first_token_retries = 0
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
        dump = os.getenv("LLM_DUMP")  # v6 debugging: append every request body (JSON) to this file
        if dump:

            async def _dump(request):
                with open(dump, "ab") as f:
                    f.write(b'{"t": %.3f, "request": ' % time.time() + request.content + b"}\n")

            self._client._client.event_hooks["request"].append(_dump)

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
        if self.spoken is not None:
            # v6 (SPOKEN_HISTORY): her spoken replies that Pipecat did not record (speculative or cut off) go back in,
            # in the real context and in this request's (a speculative request runs on a provisional copy)
            try:
                for ctx in {id(c): c for c in (self.spoken.context, context)}.values():
                    fixed = self.spoken.repair(ctx.get_messages())
                    if fixed is not None:
                        ctx.set_messages(fixed)
                msgs = context.get_messages()
            except Exception as e:  # noqa: BLE001
                logger.warning(f"{self}: spoken history repair failed: {e!r}")
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
        # v6: an early reply to a turn that may not be over (Pipecat's speculation gate holds its output)
        gate = getattr(self, "_speculation_gate", None)
        call["speculative"] = speculative = bool(gate is not None and gate.is_speculating)
        user_text = _last_user_text(msgs)
        noted = self._add_turn_note(context, call)
        wait_msg = None
        separate = self.cut_check is not None and speculative and self.on_verdict is not None and bool(user_text)
        if separate:
            # v6 (EOT_CHECK=separate): the end-of-turn verdict comes from its own request, started now, in parallel
            prev = next((m["content"] for m in reversed(msgs[:-1]) if isinstance(m, dict) and m.get("role") == "assistant"
                         and isinstance(m.get("content"), str)), "")
            verdict_cb, check = self.on_verdict, self.cut_check

            async def _cut() -> None:
                v = await check.verdict(user_text, prev)
                call["cut_check"] = v
                if v is not None:
                    try:
                        await verdict_cb(v, user_text)  # a late "wait" is stale there; never a re-run here
                    except Exception as e:  # noqa: BLE001
                        logger.warning(f"{self}: cut-off verdict failed: {e!r}")

            t = asyncio.get_running_loop().create_task(_cut())
            self._cut_tasks.add(t)
            t.add_done_callback(self._cut_tasks.discard)
        elif self.wait_note and speculative and msgs and isinstance(msgs[-1], dict) and msgs[-1].get("role") == "user":
            # v6 smoke: the <wait/> offer inside the user's turn note made the model worse at the request itself
            # ("fifteen percent of eighty" -> "Twelve point five" 6/6; "Wait, I meant Tokyo" -> Rome 10/10). As a
            # separate system line after the user's message it is 5/5 right on both and still catches cut-offs 10/10.
            wait_msg = {"role": "system", "content": WAIT_SYSTEM}
            context.add_message(wait_msg)
            call["wait_offered"] = True
        try:
            if call.get("engagement"):
                stream = _Canned("<pass/>")
            elif self._hedge_secs > 0:
                stream = await self._hedged(context, call)
            elif self._first_token_timeout > 0:
                stream = await self._first_token_or_retry(context, call)
            else:
                stream = await super().get_chat_completions(context)
        except Exception as e:
            call["error"] = repr(e)[:200]
            call["t_end"] = time.time()
            raise
        finally:
            self._pending_call = None
            if noted is not None:
                noted[0]["content"] = noted[1]  # the note was for this request only; the history stays clean
            if wait_msg is not None:
                context.set_messages([m for m in context.get_messages() if m is not wait_msg])
        on_verdict = None
        if speculative and self.on_verdict is not None and user_text and not separate:
            verdict_cb = self.on_verdict

            async def on_verdict(kind: str) -> None:
                try:
                    if await verdict_cb(kind, user_text) == "rerun":
                        # v6: the turn was confirmed before this <wait/> arrived; answer it with a fresh,
                        # non-speculative inference (no <wait/> option). The user aggregator pushes the context.
                        from pipecat.frames.frames import LLMRunFrame
                        from pipecat.processors.frame_processor import FrameDirection

                        call["wait_rerun"] = True
                        await self.push_frame(LLMRunFrame(), FrameDirection.UPSTREAM)
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"{self}: LLM end-of-turn verdict failed: {e!r}")

        return _TimedStream(stream, call, on_verdict=on_verdict)

    async def _first_token_or_retry(self, context, call: dict):
        """v3: the v2 route test saw two 14-15 s first-token stalls at OpenRouter. Wait up to the timeout, counted
        from sending the request (the full benchmark showed the stall can sit before the response even starts), for
        the first chunk that carries text, a tool call or reasoning; if none comes, drop the request and send it once
        more with the provider order reversed (no timeout on the retry). Nothing has run yet, so a retry is safe."""
        early: list = []
        held: dict = {}

        async def first_real():
            held["stream"] = await super(TimedGroqLLMService, self).get_chat_completions(context)
            held["it"] = it = held["stream"].__aiter__()
            while True:
                chunk = await it.__anext__()
                early.append(chunk)
                for c in getattr(chunk, "choices", None) or []:
                    d = c.delta
                    if c.finish_reason or (d is not None and (d.content or d.tool_calls or getattr(d, "reasoning", None))):
                        return

        t0 = time.time()
        try:
            await asyncio.wait_for(first_real(), self._first_token_timeout)
        except StopAsyncIteration:
            pass
        except asyncio.TimeoutError:
            waited = (time.time() - t0) * 1000
            self.first_token_retries += 1
            call["first_token_retry_after_ms"] = round(waited)
            call["first_token_stalled_in"] = "stream" if "stream" in held else "request"
            logger.warning(
                f"{self}: no first token after {waited:.0f} ms (stalled in the {call['first_token_stalled_in']});"
                " retrying once with the providers swapped"
            )
            try:
                if "stream" in held:
                    await held["stream"].close()
            except Exception:  # noqa: BLE001
                pass
            self._swap_providers = True
            try:
                return await super().get_chat_completions(context)
            finally:
                self._swap_providers = False
        return _Prefixed(early, held["it"], held["stream"])

    async def _hedged(self, context, call: dict):
        """v7 (LLM_HEDGE_SECS): v6's p90 reply was 2.6 s, mostly first-token tails (14% of calls over 800 ms, max
        4.4 s). Send the request; if it has produced nothing real (text, tool call, reasoning) after the hedge time,
        send the same request again with the provider order reversed, and keep whichever produces a token first.
        The loser is closed before it streams anything, so only one reply is ever used (cost: one extra prompt
        on ~1 in 7 turns)."""

        async def attempt(swap: bool):
            if swap:
                self._swap_once = True  # read once by build_chat_completion_params for this request only
            stream = await super(TimedGroqLLMService, self).get_chat_completions(context)
            it = stream.__aiter__()
            early: list = []
            while True:
                try:
                    chunk = await it.__anext__()
                except StopAsyncIteration:
                    return early, it, stream
                early.append(chunk)
                for c in getattr(chunk, "choices", None) or []:
                    d = c.delta
                    if c.finish_reason or (d is not None and (d.content or d.tool_calls or getattr(d, "reasoning", None))):
                        return early, it, stream

        t0 = time.time()
        first = asyncio.create_task(attempt(False))
        done, _ = await asyncio.wait({first}, timeout=self._hedge_secs)
        if first in done and not first.exception():
            early, it, stream = first.result()
            return _Prefixed(early, it, stream)
        self.hedges["fired"] += 1
        call["hedge_after_ms"] = round((time.time() - t0) * 1000)
        logger.info(f"{self}: no first token after {call['hedge_after_ms']} ms; hedging with the providers swapped")
        second = asyncio.create_task(attempt(True))
        pending = {t for t in (first, second) if not t.done() or not t.exception()}
        winner = None
        while pending and winner is None:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                if not t.exception():
                    winner = t
                    break
        for t in (first, second):
            if t is winner:
                continue
            if not t.done():
                t.cancel()
            elif not t.exception():
                try:
                    await t.result()[2].close()
                except Exception:  # noqa: BLE001
                    pass
        if winner is None:
            self.hedges["both_failed"] += 1
            raise (second.exception() or first.exception())
        by = "hedge" if winner is second else "first"
        self.hedges["won_by_" + by] += 1
        call["hedge_won_by"] = by
        logger.info(f"{self}: first token after {(time.time() - t0) * 1000:.0f} ms from the {by} request")
        early, it, stream = winner.result()
        return _Prefixed(early, it, stream)

    def build_chat_completion_params(self, params_from_context) -> dict:
        params = super().build_chat_completion_params(params_from_context)
        swap_once, self._swap_once = self._swap_once, False
        if self._swap_providers or swap_once:
            body = dict(params.get("extra_body") or {})
            provider = dict(body.get("provider") or {})
            order = list(provider.get("order") or [])
            if len(order) > 1:
                provider["order"] = order[1:] + order[:1]
                body["provider"] = provider
                params["extra_body"] = body
        return params

    def _add_turn_note(self, context, call: dict):
        """v2: append the per-turn note to the latest user message, for this request only.

        The system prompt and the history before the latest user message are then byte-for-byte the same on every
        call, so the provider's prompt cache covers them (v1 rewrote the system message on every turn).
        Returns (message, original content) to restore, or None.
        """
        if self._turn_note is None:
            return None
        try:
            note = self._turn_note()
        except Exception as e:  # noqa: BLE001
            logger.debug(f"{self}: turn note failed: {e!r}")
            return None
        if not note:
            return None
        msgs = context.get_messages() if hasattr(context, "get_messages") else context.messages
        for m in reversed(msgs):
            if isinstance(m, dict) and m.get("role") == "user" and isinstance(m.get("content"), str):
                original = m["content"]
                if self._engagement is not None:  # v5: speech meant for someone else -> a pass hint in the note
                    try:
                        why = self._engagement(original)
                    except Exception:  # noqa: BLE001
                        why = None
                    if why:
                        call["engagement"] = why  # v5b: answered with <pass/> in get_chat_completions, no LLM call
                        logger.info(f"{self}: engagement check: {why}; passing without the LLM: [{original[:80]}]")
                flags = FLAGS.get(original)  # v6: the STT's verdicts on this turn (voice gate, floor control)
                if flags.get("passed") and not call.get("engagement"):
                    call["engagement"] = flags["passed"]
                    logger.info(f"{self}: voice gate: {flags['passed']}; passing without the LLM: [{original[:80]}]")
                extra = []
                if flags.get("note"):
                    extra.append(flags["note"])
                    call["floor_note"] = True
                if extra:
                    note = note[:-1] + "; " + "; ".join(extra) + "]" if note.endswith("]") else f"{note} [{'; '.join(extra)}]"
                m["content"] = f"{original}\n\n{note}"
                call["turn_note"] = note
                return m, original
        return None


def _last_user_text(msgs) -> str:
    for m in reversed(msgs):
        if isinstance(m, dict) and m.get("role") == "user" and isinstance(m.get("content"), str):
            return m["content"]
    return ""


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
