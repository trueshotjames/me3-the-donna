"""Per-turn timing record: one block in the log and one JSON line in data/turns.jsonl per turn.

All timestamps are ``time.time()`` on this machine (the MCP servers run here too), so
differences between them are exact to about a millisecond.

Marks (see reports/latency-logging-proposal.html):
    T1  your first word starts          (Flux word timing, mapped to wall clock)
    T2  your last word ends             (Flux word timing, mapped to wall clock)
    T3  early end-of-turn guesses       (received by the pipeline)
    T4  Flux end of turn at the ASR server
    T5  Flux end of turn in the pipeline
    T6  LLM request sent                (per call)
    T7  first token back                (per call)
    T8  first sentence handed to TTS
    T9  Aura's first bytes              (TTS server)
    T10 first audio chunk back in the pipeline
    T11 first reply audio handed to the speaker
    T12 first audible sample = T11 + leading silence + sound-card delay

End-to-end = T12 - T2 = end-of-turn wait (T5-T2) + LLM remaining (T8-T5)
                        + TTS first audio (T10-T8) + speaker (T12-T10)

The services call the ``METRICS`` recorder directly; ``TurnMetricsObserver`` adds what
only the pipeline sees (bot speaking, tool calls, interruptions).
"""

from __future__ import annotations

import asyncio
import json
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from loguru import logger
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    FunctionCallInProgressFrame,
    FunctionCallResultFrame,
    InterruptionFrame,
)
from pipecat.observers.base_observer import BaseObserver, FramePushed

FINALIZE_AFTER_BOT_STOP_S = 1.5  # wait this long after she stops before closing the record
FINALIZE_NO_REPLY_S = 20.0  # close a turn that never got a reply


def _ms(a: float | None, b: float | None) -> float | None:
    """(b - a) in milliseconds, or None if either is missing."""
    if a is None or b is None:
        return None
    return round((b - a) * 1000, 1)


def _stats(values: list[float]) -> dict | None:
    vals = [v for v in values if v is not None]
    if not vals:
        return None
    vals.sort()
    p90 = vals[min(len(vals) - 1, int(round(0.9 * (len(vals) - 1))))]
    return {
        "n": len(vals),
        "median": round(statistics.median(vals), 1),
        "p90": round(p90, 1),
        "max": round(vals[-1], 1),
    }


def _clock(t: float | None) -> str:
    if t is None:
        return "?"
    return time.strftime("%H:%M:%S", time.localtime(t)) + f".{int((t % 1) * 1000):03d}"


@dataclass
class Turn:
    n: int
    kind: str = "user"  # user | bot (bot-initiated speech, e.g. an alarm)
    created: float = field(default_factory=time.time)
    # user side
    flux_start: float | None = None
    flux_start_server: float | None = None
    speech_start: float | None = None  # T1
    speech_end: float | None = None  # T2
    speech_source: str | None = None
    last_word: str | None = None
    eager: list[dict] = field(default_factory=list)  # {t, t_server, transcript, outcome}
    eot: float | None = None  # T5
    eot_server: float | None = None  # T4
    transcript: str = ""
    eot_trigger: str | None = None
    vad_started: list[float] = field(default_factory=list)
    vad_stopped: list[float] = field(default_factory=list)
    asr_push_ms: list[float] = field(default_factory=list)
    asr_update_lag_ms: list[float] = field(default_factory=list)
    asr_dropped: int = 0
    gate_opened: list[tuple[float, str]] = field(default_factory=list)
    preroll_ms: list[float] = field(default_factory=list)
    word_map_warning: str | None = None
    # reply side
    llm_calls: list[dict] = field(default_factory=list)
    tools: dict[str, dict] = field(default_factory=dict)
    tts: list[dict] = field(default_factory=list)
    bot_started: float | None = None  # T11
    bot_stopped: float | None = None
    interrupted_at: float | None = None  # barge-in detected while she was speaking
    interrupted_stop: float | None = None
    closed: bool = False


class Recorder:
    def __init__(self) -> None:
        self.path: Path | None = None
        self.tool_exec_path: Path | None = None
        self.output_latency_fn: Callable[[], float | None] | None = None
        self.vad = None  # RecordingSileroVAD, for per-turn confidence/loudness stats
        self.vad_thresholds: tuple[float, float] = (0.0, 0.0)
        self._n = 0
        self.current: Turn | None = None  # turn the latest user speech belongs to
        self.reply: Turn | None = None  # turn whose reply is being produced/spoken
        self.bot_speaking = False
        self._finalize_task: asyncio.Task | None = None
        self._pending_vad_start: float | None = None
        self._pending_gate: tuple[float, str, float] | None = None
        self.on_problem: Callable[[str], None] | None = None  # UI warning for a turn that failed quietly

    # ------------------------------------------------------------ turn lifecycle

    def _new_turn(self, kind: str = "user") -> Turn:
        self._n += 1
        return Turn(n=self._n, kind=kind)

    def _user_turn(self) -> Turn:
        if self.current is None or self.current.eot is not None:
            self.start_of_turn(time.time(), None)
        return self.current

    def _reply_turn(self) -> Turn:
        """The turn that reply-side events (TTS, speaker, tools) belong to."""
        if self.current is not None and self.current.eot is not None and not self.current.closed:
            return self.current
        if self.reply is not None and not self.reply.closed:
            return self.reply
        turn = self._new_turn("bot")
        self.reply = turn
        return turn

    def start_of_turn(self, t: float, t_server: float | None) -> None:
        """Flux StartOfTurn. Closes the previous reply (recording a barge-in if she was speaking)."""
        cur = self.current
        if cur is not None and not cur.closed and cur.eot is None:
            return  # already inside an open user turn
        prev = self.reply
        if prev is not None and not prev.closed:
            if self.bot_speaking:
                prev.interrupted_at = prev.interrupted_at or t  # finalized when her audio stops
            else:
                self._finalize(prev)
        turn = self._new_turn("user")
        turn.flux_start = t
        turn.flux_start_server = t_server
        self.current = turn
        # The detector and the gate usually act before Flux reports the start.
        if self._pending_vad_start is not None and t - self._pending_vad_start < 3.0:
            turn.vad_started.append(self._pending_vad_start)
        if self._pending_gate is not None and t - self._pending_gate[0] < 3.0:
            turn.gate_opened.append(self._pending_gate[:2])
            turn.preroll_ms.append(self._pending_gate[2])
        self._pending_vad_start = None
        self._pending_gate = None

    def eager(self, t: float, t_server: float | None, transcript: str) -> None:
        self._user_turn().eager.append({"t": t, "t_server": t_server, "transcript": transcript, "outcome": "open"})

    def turn_resumed(self, t: float) -> None:
        turn = self._user_turn()
        for e in reversed(turn.eager):
            if e["outcome"] == "open":
                e["outcome"] = "discarded"
                e["t_resumed"] = t
                break

    def end_of_turn(
        self,
        t: float,
        t_server: float | None,
        transcript: str,
        speech_start: float | None,
        speech_end: float | None,
        source: str | None,
        last_word: str | None,
        trigger: str | None,
        warning: str | None = None,
    ) -> None:
        turn = self._user_turn()
        turn.eot, turn.eot_server, turn.transcript = t, t_server, transcript
        turn.speech_start, turn.speech_end, turn.speech_source = speech_start, speech_end, source
        turn.last_word, turn.eot_trigger, turn.word_map_warning = last_word, trigger, warning
        for e in turn.eager:
            if e["outcome"] == "open":
                e["outcome"] = "kept"
        self.reply = turn
        self._schedule_finalize(FINALIZE_NO_REPLY_S)

    # ------------------------------------------------------------- ASR details

    def asr_push(self, ms: float) -> None:
        if self.current is not None and not self.current.closed and self.current.eot is None:
            self.current.asr_push_ms.append(ms)

    def asr_update_lag(self, ms: float) -> None:
        if self.current is not None and not self.current.closed and self.current.eot is None:
            self.current.asr_update_lag_ms.append(ms)

    def asr_dropped(self) -> None:
        if self.current is not None:
            self.current.asr_dropped += 1

    def gate_opened(self, t: float, reason: str, preroll_ms: float) -> None:
        turn = self.current if self.current is not None and not self.current.closed else None
        if turn is not None and turn.eot is None:
            turn.gate_opened.append((t, reason))
            turn.preroll_ms.append(preroll_ms)
        else:
            self._pending_gate = (t, reason, preroll_ms)

    def vad_started(self, t: float) -> None:
        if self.current is not None and not self.current.closed and self.current.eot is None:
            self.current.vad_started.append(t)
        else:
            self._pending_vad_start = t

    def vad_stopped(self, t: float) -> None:
        if self.current is not None and not self.current.closed and self.current.eot is None:
            self.current.vad_stopped.append(t)

    # --------------------------------------------------------------- LLM / TTS

    def llm_call(self) -> dict:
        call: dict[str, Any] = {"t_req": time.time(), "content_t": [], "chars": 0, "chunks": 0}
        cur = self.current
        if cur is not None and not cur.closed and cur.eot is None and cur.flux_start is not None:
            cur.llm_calls.append(call)  # early (speculative) call during an open user turn
        else:
            self._reply_turn().llm_calls.append(call)
        return call

    def tts_sentence(self, record: dict) -> None:
        self._reply_turn().tts.append(record)

    # -------------------------------------------------- events from the observer

    def on_bot_started(self, t: float) -> None:
        self.bot_speaking = True
        turn = self._reply_turn()
        if turn.bot_started is None:
            turn.bot_started = t
        if self._finalize_task:
            self._finalize_task.cancel()
            self._finalize_task = None

    def on_bot_stopped(self, t: float) -> None:
        self.bot_speaking = False
        turn = self.reply if self.reply is not None and not self.reply.closed else None
        if turn is None:
            return
        turn.bot_stopped = t
        if turn.interrupted_at is not None:
            turn.interrupted_stop = t
            self._finalize(turn)
        else:
            self._schedule_finalize(FINALIZE_AFTER_BOT_STOP_S)

    def on_interruption(self, t: float) -> None:
        if self.bot_speaking and self.reply is not None and not self.reply.closed:
            self.reply.interrupted_at = self.reply.interrupted_at or t

    def on_tool_start(self, tool_id: str, name: str, t: float) -> None:
        turn = self._reply_turn()
        turn.tools.setdefault(tool_id, {"name": name, "t_start": t})

    def on_tool_end(self, tool_id: str, t: float) -> None:
        for turn in (self.reply, self.current):
            if turn is not None and tool_id in turn.tools:
                turn.tools[tool_id].setdefault("t_end", t)
                return

    # ---------------------------------------------------------------- finalize

    def _schedule_finalize(self, delay: float) -> None:
        if self._finalize_task:
            self._finalize_task.cancel()
        turn = self.reply
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        async def later():
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                return
            if turn is not None and not turn.closed and not self.bot_speaking:
                # A follow-up LLM call can still be waiting (Groq 429 after "One moment."): keep the
                # record open for it, up to 30 s after she stopped.
                if self._llm_in_flight(turn) and time.time() - (turn.bot_stopped or 0) < 30:
                    self._schedule_finalize(1.0)
                    return
                self._finalize(turn)

        self._finalize_task = loop.create_task(later())

    @staticmethod
    def _llm_in_flight(turn: Turn) -> bool:
        t5 = turn.eot or 0
        return any(c["t_req"] >= t5 and c.get("t_end") is None and not c.get("error") for c in turn.llm_calls)

    def flush(self) -> None:
        for turn in (self.reply, self.current):
            if turn is not None and not turn.closed:
                self._finalize(turn)

    def _finalize(self, turn: Turn) -> None:
        if turn.closed:
            return
        turn.closed = True
        if not (turn.eot or turn.tts or turn.llm_calls):
            return  # Flux opened a turn on noise and never ended it with words
        try:
            record = self._build(turn)
        except Exception as e:  # noqa: BLE001
            logger.error(f"turn metrics: could not build record for turn {turn.n}: {e!r}")
            return
        for line in self._format(record):
            logger.info(line)
        if self.on_problem is not None:
            for msg in self._problems(turn, record):
                logger.warning(f"turn problem: turn {turn.n}: {msg}")
                try:
                    self.on_problem(msg)
                except Exception as e:  # noqa: BLE001
                    logger.debug(f"turn problem hook failed: {e!r}")
        if self.path is not None:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            except OSError as e:
                logger.warning(f"turn metrics: could not write {self.path}: {e}")

    @staticmethod
    def _problems(turn: Turn, record: dict) -> list[str]:
        """Plain-language warnings for a user turn that went wrong without an error frame."""
        if turn.kind != "user" or turn.eot is None:
            return []
        said = (turn.transcript or "").strip()
        said = f'"{said[:60]}{"..." if len(said) > 60 else ""}"'
        calls = [c for c in record["llm"]["calls"] if c["kind"] != "early-discarded"]
        tools = [t["name"] for t in record["tools"]]
        errors = [c["error"] for c in calls if c.get("error")]
        waited = sum(w.get("retry_after") or 0 for c in calls for w in (c.get("waits") or []))
        why = f" (Groq rate limit, waited {waited:.0f} s)" if waited else ""
        out = []
        if errors:
            out.append(f"The LLM call failed for {said}: {errors[-1][:120]}. Say it again.")
        elif tools and calls and calls[-1]["first_token_ms"] is None:
            out.append(
                f"{said} did not finish: it stopped after {', '.join(tools)}{why}."
                " Check your alarms and reminders, or say it again."
            )
        elif record.get("note"):
            out.append(f"No reply to {said}{why}. Say it again if you still need it.")
        return out

    # ------------------------------------------------------------ build record

    def _tool_exec(self, name: str, t0: float, t1: float) -> float | None:
        if self.tool_exec_path is None or not self.tool_exec_path.exists():
            return None
        try:
            lines = self.tool_exec_path.read_text(encoding="utf-8").splitlines()[-50:]
        except OSError:
            return None
        for line in reversed(lines):
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("tool") == name and t0 - 0.05 <= rec.get("t_end", 0) <= t1 + 0.05:
                return rec.get("exec_ms")
        return None

    def _vad_stats(self, t0: float | None, t1: float | None) -> dict | None:
        if self.vad is None or t0 is None or t1 is None or t1 <= t0:
            return None
        frames = self.vad.frames_between(t0, t1)
        before = self.vad.frames_between(t0 - 3.0, t0 - 0.3)
        if not frames:
            return None
        conf_thr, vol_thr = self.vad_thresholds
        n = len(frames)
        res = {
            "frames": n,
            "peak_confidence": round(max(f[1] for f in frames), 3),
            "peak_loudness": round(max(f[2] for f in frames), 3),
            "median_confidence": round(statistics.median(f[1] for f in frames), 3),
            "median_loudness": round(statistics.median(f[2] for f in frames), 3),
            "pct_confidence_pass": round(100 * sum(f[1] >= conf_thr for f in frames) / n, 1),
            "pct_loudness_pass": round(100 * sum(f[2] >= vol_thr for f in frames) / n, 1),
            "pct_both_pass": round(100 * sum(f[1] >= conf_thr and f[2] >= vol_thr for f in frames) / n, 1),
            "thresholds": {"confidence": conf_thr, "loudness": vol_thr},
        }
        if before:
            res["before_speech_median_confidence"] = round(statistics.median(f[1] for f in before), 3)
            res["before_speech_median_loudness"] = round(statistics.median(f[2] for f in before), 3)
        return res

    def _build(self, turn: Turn) -> dict:
        T2, T5 = turn.speech_end, turn.eot
        # LLM calls
        calls = []
        last_resume = max((e.get("t_resumed", 0) for e in turn.eager), default=0)
        tool_ends = sorted(v.get("t_end") for v in turn.tools.values() if v.get("t_end"))
        for c in turn.llm_calls:
            if T5 is not None and c["t_req"] < T5:
                kind = "early-discarded" if c["t_req"] < last_resume else "early"
            elif tool_ends and c["t_req"] >= tool_ends[0]:
                kind = "follow-up"
            else:
                kind = "normal"
            gaps = [(b - a) * 1000 for a, b in zip(c["content_t"], c["content_t"][1:])]
            first = c.get("t_first_content") or c.get("t_first_tool")
            end = c.get("t_end")
            tokens = c.get("completion_tokens")
            gen_s = (end - first) if (end and first) else None
            calls.append(
                {
                    "kind": kind,
                    "t_req": c["t_req"],
                    "first_token_ms": _ms(c["t_req"], first),
                    "first_chunk_ms": _ms(c["t_req"], c.get("t_first_chunk")),
                    "tool_call_written_ms": _ms(c["t_req"], c.get("t_tool_done")) if c.get("t_first_tool") else None,
                    "total_ms": _ms(c["t_req"], end),
                    "chunks": c["chunks"],
                    "chars": c["chars"],
                    "completion_tokens": tokens,
                    "tokens_per_s": round(tokens / gen_s, 1) if tokens and gen_s and gen_s > 0 else None,
                    "token_gap_ms": _stats(gaps),
                    "error": c.get("error"),
                    "prompt_tokens": c.get("prompt_tokens"),
                    "rate_limit": c.get("rate_limit"),
                    "waits": c.get("waits"),
                }
            )
        kept = [c for c in calls if c["kind"] != "early-discarded"]
        # tools
        tools = []
        for tid, v in turn.tools.items():
            t0, t1 = v.get("t_start"), v.get("t_end")
            exec_ms = self._tool_exec(v["name"], t0, t1) if (t0 and t1) else None
            rt = _ms(t0, t1)
            nxt = next((c["t_req"] for c in calls if t1 and c["t_req"] >= t1), None)
            nxt_call = next((c for c in calls if t1 and c["t_req"] >= t1), None)
            tools.append(
                {
                    "name": v["name"],
                    "id": tid,
                    "round_trip_ms": rt,
                    "exec_ms": exec_ms,
                    "mcp_overhead_ms": round(rt - exec_ms, 1) if (rt is not None and exec_ms is not None) else None,
                    "result_to_next_call_ms": _ms(t1, nxt),
                    "follow_up_first_token_ms": nxt_call["first_token_ms"] if nxt_call else None,
                }
            )
        # TTS sentences
        sentences = []
        for s in turn.tts:
            chunks = s.get("chunks", [])
            gaps = [(b[0] - a[0]) * 1000 for a, b in zip(chunks, chunks[1:])]
            first_t = chunks[0][0] if chunks else None
            last_t = chunks[-1][0] if chunks else None
            audio_s = s.get("audio_s") or 0.0
            synth_s = (last_t - s["t_call"]) if last_t else None
            sentences.append(
                {
                    "chars": s.get("chars"),
                    "t_call": s["t_call"],
                    "first_audio_ms": _ms(s["t_call"], first_t),
                    "start_call_ms": _ms(s["t_call"], s.get("t_started")),
                    "aura_first_byte_ms": _ms(s.get("t_speak_sent"), s.get("t_first_audio_server")),
                    "server_to_client_ms": _ms(s.get("t_first_audio_server"), first_t),
                    "chunks": len(chunks),
                    "chunk_gap_ms": _stats(gaps),
                    "read_ms": _stats(s.get("read_ms", [])),
                    "synthesis_ms": round(synth_s * 1000, 1) if synth_s is not None else None,
                    "audio_s": round(audio_s, 3),
                    "rtf": round(synth_s / audio_s, 3) if (synth_s and audio_s) else None,
                    "leading_silence_ms": s.get("leading_silence_ms"),
                    "interrupted": s.get("interrupted", False),
                }
            )
        # end to end
        reply_tts = [s for s in turn.tts if T5 is None or s["t_call"] >= T5]
        first_s = reply_tts[0] if reply_tts else None
        T8 = first_s["t_call"] if first_s else None
        T10 = first_s["chunks"][0][0] if first_s and first_s.get("chunks") else None
        T11 = turn.bot_started
        lead = (first_s or {}).get("leading_silence_ms") or 0.0
        out_lat = None
        if self.output_latency_fn is not None:
            try:
                out_lat = self.output_latency_fn()
            except Exception:  # noqa: BLE001
                out_lat = None
        T12 = (T11 + lead / 1000 + (out_lat or 0.0)) if T11 is not None else None
        e2e = None
        if None not in (T2, T5, T8, T10, T12):
            e2e = {
                "total_ms": _ms(T2, T12),
                "end_of_turn_ms": _ms(T2, T5),
                "llm_remaining_ms": _ms(T5, T8),
                "tts_first_audio_ms": _ms(T8, T10),
                "speaker_ms": _ms(T10, T12),
            }
        # LLM part split for tool turns: decide + tools + follow-up, inside [T5, T8]
        llm_split = None
        if tools and T5 is not None and T8 is not None:
            first_tool_t0 = min(v["t_start"] for v in turn.tools.values())
            last_tool_t1 = max(v.get("t_end", v["t_start"]) for v in turn.tools.values())
            llm_split = {
                "decide_ms": _ms(T5, max(T5, first_tool_t0)),
                "tools_ms": _ms(max(T5, first_tool_t0), last_tool_t1),
                "follow_up_ms": _ms(last_tool_t1, T8),
                "parallel_tools": len(turn.tools) > 1
                and any(
                    a.get("t_start", 0) < b.get("t_end", 0) and b.get("t_start", 0) < a.get("t_end", 0)
                    for i, a in enumerate(turn.tools.values())
                    for j, b in enumerate(turn.tools.values())
                    if i < j
                ),
            }
        kept_eager = next((e for e in reversed(turn.eager) if e["outcome"] == "kept"), None)
        vad_first = bool(turn.vad_started and turn.flux_start and turn.vad_started[0] <= turn.flux_start)
        vad_end_est = (turn.vad_stopped[-1] - self.vad.params.stop_secs) if (turn.vad_stopped and self.vad) else None
        record = {
            "turn": turn.n,
            "kind": turn.kind,
            "t": turn.created,
            "transcript": turn.transcript,
            "marks": {
                "T1_speech_start": turn.speech_start,
                "T2_speech_end": T2,
                "T3_eager": [e["t"] for e in turn.eager],
                "T4_eot_server": turn.eot_server,
                "T5_eot": T5,
                "T6_llm_req": [c["t_req"] for c in calls],
                "T8_first_sentence_to_tts": T8,
                "T10_first_audio_chunk": T10,
                "T11_audio_to_speaker": T11,
                "T12_first_audible": T12,
                "flux_start": turn.flux_start,
                "bot_stopped": turn.bot_stopped,
            },
            "speech": {
                "source": turn.speech_source,
                "duration_ms": _ms(turn.speech_start, T2),
                "last_word": turn.last_word,
                "vad_end_estimate": vad_end_est,
                "vad_vs_flux_end_ms": _ms(T2, vad_end_est),
                "mapping_warning": turn.word_map_warning,
            },
            "end_to_end": e2e,
            "asr": {
                "push_ms": _stats(turn.asr_push_ms),
                "update_lag_ms": _stats(turn.asr_update_lag_ms),
                "flux_decision_ms": _ms(T2, turn.eot_server),
                "delivery_ms": _ms(turn.eot_server, T5),
                "end_of_turn_wait_ms": _ms(T2, T5),
                "trigger": turn.eot_trigger,
                "eager_guesses": len(turn.eager),
                "eager_discarded": sum(e["outcome"] == "discarded" for e in turn.eager),
                "kept_guess_lead_ms": _ms(kept_eager["t"], T5) if kept_eager else None,
                "dropped_chunks": turn.asr_dropped,
                "gate_opened": [{"t": t, "reason": r} for t, r in turn.gate_opened],
                "preroll_ms": turn.preroll_ms,
            },
            "llm": {"calls": calls, "split": llm_split, "early_calls": sum(c["kind"].startswith("early") for c in calls)},
            "tools": tools,
            "tts": {
                "sentences": sentences,
                "total_audio_s": round(sum(s["audio_s"] for s in sentences), 3),
            },
            "output": {
                "leading_silence_ms": lead if first_s else None,
                "sound_card_latency_ms": round(out_lat * 1000, 1) if out_lat is not None else None,
            },
            "vad": {
                "fired_first": vad_first,
                "lead_over_flux_ms": _ms(turn.vad_started[0], turn.flux_start) if (turn.vad_started and turn.flux_start) else None,
                "stats": self._vad_stats(turn.speech_start, T2),
            },
            "barge_in": {
                "detected": turn.interrupted_at,
                "stop_ms": _ms(turn.interrupted_at, turn.interrupted_stop),
            }
            if turn.interrupted_at
            else None,
        }
        if kept and not e2e and turn.kind == "user" and T5 is not None:
            record["note"] = "no reply audio (passed, interrupted or failed)"
        return record

    # --------------------------------------------------------------- log block

    @staticmethod
    def _f(v: float | None, unit: str = "s", digits: int = 2) -> str:
        if v is None:
            return "?"
        if unit == "s":
            return f"{v / 1000:.{digits}f}"
        return f"{v:.0f}"

    def _format(self, r: dict) -> list[str]:
        f = self._f
        e = r["end_to_end"]
        head = f"turn {r['turn']}"
        if r["kind"] == "bot":
            head += " (bot-initiated)"
        if e:
            head += (
                f" | end-to-end {f(e['total_ms'])} s = end-of-turn {f(e['end_of_turn_ms'])}"
                f" + LLM {f(e['llm_remaining_ms'])} + TTS {f(e['tts_first_audio_ms'])} + speaker {f(e['speaker_ms'])}"
            )
        elif r.get("note"):
            head += f" | {r['note']}"
        lines = [head]
        sp, a, m = r["speech"], r["asr"], r["marks"]
        if r["kind"] == "user":
            lines.append(
                f"  you    [{r['transcript'][:70]}] {f(sp['duration_ms'])} s of speech, last word "
                f"\"{sp['last_word'] or '?'}\" ended {_clock(m['T2_speech_end'])} ({sp['source'] or 'unknown'})"
                + (f"  WARNING {sp['mapping_warning']}" if sp["mapping_warning"] else "")
            )
            push = a["push_ms"] or {}
            lag = a["update_lag_ms"] or {}
            lines.append(
                f"  asr    {push.get('n', 0)} chunks, push median {f(push.get('median'), 'ms')} ms / p90 {f(push.get('p90'), 'ms')} ms;"
                f" transcript lag median {f(lag.get('median'), 'ms')} ms; flux {f(a['flux_decision_ms'])}"
                f" + delivery {f(a['delivery_ms'])} s; trigger {a['trigger'] or '?'}"
            )
            lines.append(
                f"         early guesses {a['eager_guesses']} ({a['eager_discarded']} discarded)"
                + (f", kept guess {f(a['kept_guess_lead_ms'], 'ms')} ms before end of turn" if a["kept_guess_lead_ms"] is not None else "")
                + (f"; pre-roll {a['preroll_ms'][0]:.0f} ms" if a["preroll_ms"] else "")
                + (f"; DROPPED {a['dropped_chunks']} chunks" if a["dropped_chunks"] else "")
            )
        for i, c in enumerate(r["llm"]["calls"], 1):
            line = f"  llm    call {i} ({c['kind']}): first token {f(c['first_token_ms'])} s"
            if c["tool_call_written_ms"] is not None:
                line += f", tool call written at {f(c['tool_call_written_ms'])} s"
            if c["completion_tokens"]:
                line += f", {c['completion_tokens']} tokens"
            if c["tokens_per_s"]:
                line += f" ({c['tokens_per_s']:.0f} tok/s)"
            if c["total_ms"] is not None:
                line += f", done {f(c['total_ms'])} s"
            if c.get("prompt_tokens"):
                line += f", prompt {c['prompt_tokens']} tokens"
            rl = c.get("rate_limit") or {}
            if rl.get("remaining_tokens") is not None:
                line += f", {rl['remaining_tokens']:.0f}/{(rl.get('limit_tokens') or 0):.0f} tokens left this minute"
            for w in c.get("waits") or []:
                line += f", RATE LIMITED (429): waited {w['retry_after']:.1f} s"
            if c["error"]:
                line += f", ERROR {c['error']}"
            lines.append(line)
        for t in r["tools"]:
            lines.append(
                f"  tools  {t['name']}: round trip {f(t['round_trip_ms'], 'ms')} ms"
                f" (execution {f(t['exec_ms'], 'ms')} ms, MCP {f(t['mcp_overhead_ms'], 'ms')} ms);"
                f" result to next call {f(t['result_to_next_call_ms'], 'ms')} ms"
            )
        if r["llm"]["split"]:
            s = r["llm"]["split"]
            lines.append(
                f"         LLM part = decide {f(s['decide_ms'])} + tools {f(s['tools_ms'])} + follow-up {f(s['follow_up_ms'])} s"
                + (" (parallel tools)" if s["parallel_tools"] else "")
            )
        for i, s in enumerate(r["tts"]["sentences"], 1):
            gap = s["chunk_gap_ms"] or {}
            lines.append(
                f"  tts    sentence {i}: first audio {f(s['first_audio_ms'])} s (aura {f(s['aura_first_byte_ms'])} s),"
                f" {s['chunks']} chunks, gap median {f(gap.get('median'), 'ms')} ms, {s['audio_s']:.1f} s audio,"
                f" RTF {s['rtf'] if s['rtf'] is not None else '?'}" + (" INTERRUPTED" if s["interrupted"] else "")
            )
        o = r["output"]
        if o["leading_silence_ms"] is not None or o["sound_card_latency_ms"] is not None:
            lines.append(
                f"  out    leading silence {f(o['leading_silence_ms'], 'ms')} ms, sound-card delay {f(o['sound_card_latency_ms'], 'ms')} ms"
            )
        v = r["vad"]
        if r["kind"] == "user":
            st = v["stats"]
            line = "  vad    " + (
                f"fired first (Flux +{v['lead_over_flux_ms']:.0f} ms)" if v["fired_first"] and v["lead_over_flux_ms"] is not None
                else ("fired after Flux" if v["lead_over_flux_ms"] is not None else "did not fire")
            )
            if st:
                line += (
                    f"; peak confidence {st['peak_confidence']}, peak loudness {st['peak_loudness']};"
                    f" {st['pct_both_pass']:.0f}% of speech frames passed"
                )
            lines.append(line)
            if sp["vad_vs_flux_end_ms"] is not None:
                lines.append(
                    f"         speech end by VAD {_clock(sp['vad_end_estimate'])} (Flux words {_clock(m['T2_speech_end'])},"
                    f" differ {sp['vad_vs_flux_end_ms']:+.0f} ms)"
                )
        if r["barge_in"]:
            lines.append(f"  barge  you cut her off; her audio stopped {f(r['barge_in']['stop_ms'], 'ms')} ms after Flux heard you")
        return lines


METRICS = Recorder()


class TurnMetricsObserver(BaseObserver):
    """Feeds the recorder with what only the pipeline sees."""

    def __init__(self, recorder: Recorder = METRICS, **kwargs):
        super().__init__(observe_every_push=False, **kwargs)
        self._r = recorder
        self._seen: set[int] = set()

    async def on_push_frame(self, data: FramePushed):
        frame = data.frame
        if isinstance(frame, (BotStartedSpeakingFrame, BotStoppedSpeakingFrame, InterruptionFrame)):
            # Broadcast frames are pushed twice (upstream and downstream copies).
            sib = getattr(frame, "broadcast_sibling_id", None)
            if frame.id in self._seen or (sib is not None and sib in self._seen):
                return
            self._seen.add(frame.id)
            if len(self._seen) > 5000:
                self._seen.clear()
            now = time.time()
            if isinstance(frame, BotStartedSpeakingFrame):
                self._r.on_bot_started(now)
            elif isinstance(frame, BotStoppedSpeakingFrame):
                self._r.on_bot_stopped(now)
            else:
                self._r.on_interruption(now)
        elif isinstance(frame, FunctionCallInProgressFrame):
            self._r.on_tool_start(frame.tool_call_id, frame.function_name, time.time())
        elif isinstance(frame, FunctionCallResultFrame):
            self._r.on_tool_end(frame.tool_call_id, time.time())
