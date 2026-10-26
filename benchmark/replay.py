"""Replay a conversation through the real pipeline (LLM, tools, TTS, speaker) from text.

No microphone or ASR: a scripted STT service plays each phrase the way Flux would report it
(start of turn, interim words, eager end of turn, end of turn), including barge-ins while
the bot is speaking. Everything after the STT is the real assistant, so this exercises the
LLM's tool use, the tools MCP server, Groq's rate limiting and the UI events.

    .venv\\Scripts\\python -m benchmark.replay benchmark/scenarios/2026-10-23-session.json
    .venv\\Scripts\\python -m benchmark.replay <scenario> --model openai/gpt-oss-20b --effort low
    .venv\\Scripts\\python -m benchmark.replay <scenario> --speed 1.5     # talk faster

Scenario file: {"steps": [{"say": "...", "when": "idle"|"speaking", "after": secs,
                           "speech_secs": secs, "expect_tool": "create_alarm"|null, "note": "..."}]}
  when=idle      wait until the bot has been quiet for `after` seconds, then speak.
  when=speaking  wait until the bot is speaking, then `after` seconds later cut in (barge-in).

Runs against its own copies of the shared state so the real assistant can keep running:
tools server on port 8767 with data/replay.db, UI events on ws://127.0.0.1:8768
(open the UI with ?ws=8768 to watch), turn records in data/replay_turns.jsonl.
Writes a report to benchmark/replay_results/<time>-<model>.json and prints a transcript.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Isolate from a running assistant before assistant.config loads .env.
os.environ.setdefault("ASSISTANT_DB", "replay.db")
os.environ.setdefault("TOOLS_MCP_PORT", "8767")
os.environ.setdefault("TOOLS_MCP_URL", "http://127.0.0.1:8767/mcp")
os.environ.setdefault("UI_WS_PORT", "8768")

from loguru import logger  # noqa: E402

from assistant.config import DATA_DIR, load  # noqa: E402
from assistant.main import Assistant, setup_logging  # noqa: E402


# ------------------------------------------------------------------ scripted STT


def make_stt(steps: list[dict], speed: float, done: asyncio.Event, events: list[dict]):
    from pipecat.frames.frames import (
        BotStartedSpeakingFrame,
        BotStoppedSpeakingFrame,
        Frame,
        InterimTranscriptionFrame,
        ProposedUserStartedSpeakingFrame,
        ProposedUserStoppedSpeakingFrame,
        StartFrame,
        STTMetadataFrame,
        TranscriptionFrame,
    )
    from pipecat.processors.frame_processor import FrameDirection
    from pipecat.services.settings import STTSettings
    from pipecat.services.stt_service import STTService
    from pipecat.turns.eager_end_of_turn_mixin import EagerEndOfTurnSTTServiceMixin
    from pipecat.utils.time import time_now_iso8601

    from assistant.turn_metrics import METRICS

    class ReplaySTTService(EagerEndOfTurnSTTServiceMixin, STTService):
        def __init__(self, **kwargs):
            super().__init__(
                sample_rate=16000,
                enable_eager_end_of_turn=True,
                settings=STTSettings(model="replay", language=None),
                **kwargs,
            )
            self._bot_speaking = False
            self._t_bot_stopped = time.time()
            self._reply_pending = False  # set after each user turn until the bot starts speaking
            self._t_spoke = 0.0
            self._task: asyncio.Task | None = None
            self.t0 = time.time()

        def can_generate_metrics(self) -> bool:
            return True

        @property
        def supports_ttfs(self) -> bool:
            return False

        def service_metadata_frame(self) -> STTMetadataFrame:
            frame = super().service_metadata_frame()
            frame.user_turn_strategies = self.recommended_user_turn_strategies(enable_interruptions=True)
            return frame

        async def start(self, frame: StartFrame):
            await super().start(frame)
            self.t0 = time.time()
            self._task = asyncio.create_task(self._run(), name="replay-script")

        async def stop(self, frame):
            await super().stop(frame)
            if self._task:
                self._task.cancel()

        async def cancel(self, frame):
            await super().cancel(frame)
            if self._task:
                self._task.cancel()

        async def process_frame(self, frame: Frame, direction: FrameDirection):
            if isinstance(frame, BotStartedSpeakingFrame):
                self._bot_speaking = True
                self._reply_pending = False
            elif isinstance(frame, BotStoppedSpeakingFrame):
                self._bot_speaking = False
                self._t_bot_stopped = time.time()
            await super().process_frame(frame, direction)

        async def run_stt(self, audio: bytes):
            yield None

        def _ev(self, kind: str, **f):
            events.append({"t": round(time.time() - self.t0, 2), "kind": kind, **f})

        async def _wait(self, step: dict) -> None:
            when = step.get("when", "idle")
            after = float(step.get("after", 1.0))
            deadline = time.time() + 90
            if when == "speaking":
                while not self._bot_speaking and time.time() < deadline:
                    await asyncio.sleep(0.05)
                await asyncio.sleep(after)
                self._ev("barge_in", note=step.get("note"))
            else:
                # The reply may still be in the LLM (or a Groq 429 retry): wait for it to start
                # first (up to 30 s), otherwise the next phrase overtakes it.
                deadline = time.time() + 120
                while time.time() < deadline:
                    if (
                        (not self._reply_pending or time.time() - self._t_spoke > 30)
                        and not self._bot_speaking
                        and time.time() - self._t_bot_stopped >= after
                    ):
                        return
                    await asyncio.sleep(0.05)

        async def _speak(self, step: dict) -> None:
            text = step["say"]
            words = text.split()
            secs = float(step.get("speech_secs") or max(0.6, 0.33 * len(words))) / speed
            now = time.time()
            self._ev("you_start", text=text)
            METRICS.start_of_turn(now, None)
            await self.broadcast_frame(ProposedUserStartedSpeakingFrame)
            t_start = time.time()
            for i in range(1, len(words) + 1):
                await asyncio.sleep(secs / len(words))
                await self.push_frame(InterimTranscriptionFrame(" ".join(words[:i]), self._user_id, time_now_iso8601(), None))
            t_end = time.time()
            METRICS.eager(t_end, None, text)
            await self._push_eager_end_of_turn(text, user_id=self._user_id)
            await asyncio.sleep(0.35 / speed)  # Flux's eager -> final gap, roughly
            now = time.time()
            METRICS.end_of_turn(now, None, text, t_start, t_end, "replay", words[-1], "replay", None)
            self._clear_eager_end_of_turn()
            self._ev("you_end", text=text)
            await self.push_frame(TranscriptionFrame(text, self._user_id, time_now_iso8601(), None, finalized=True))
            await self.broadcast_frame(ProposedUserStoppedSpeakingFrame)
            self._reply_pending = True
            self._t_spoke = time.time()

        async def _run(self):
            try:
                await asyncio.sleep(2.0)
                for i, step in enumerate(steps):
                    await self._wait(step)
                    await self._speak(step)
                # let the last reply finish
                wait_until = time.time() + 30
                while self._reply_pending and time.time() < wait_until:
                    await asyncio.sleep(0.1)
                while self._bot_speaking:
                    await asyncio.sleep(0.1)
                await asyncio.sleep(2.0)
            except asyncio.CancelledError:
                return
            except Exception as e:  # noqa: BLE001
                logger.error(f"replay script failed: {e!r}")
            finally:
                done.set()

    return ReplaySTTService()


# --------------------------------------------------------------------- observer


def make_observer(events: list[dict], t0: list[float]):
    from pipecat.frames.frames import (
        BotStartedSpeakingFrame,
        BotStoppedSpeakingFrame,
        ErrorFrame,
        FunctionCallInProgressFrame,
        FunctionCallResultFrame,
        InterruptionFrame,
        TTSTextFrame,
    )
    from pipecat.observers.base_observer import BaseObserver, FramePushed

    class ReplayObserver(BaseObserver):
        def __init__(self):
            super().__init__(observe_every_push=False)
            self._seen: set = set()

        def _ev(self, kind: str, **f):
            events.append({"t": round(time.time() - t0[0], 2), "kind": kind, **f})

        async def on_push_frame(self, data: FramePushed):
            fr = data.frame
            if isinstance(fr, TTSTextFrame) and fr.text.strip():
                self._ev("bot", text=fr.text)
            elif isinstance(fr, FunctionCallInProgressFrame):
                if fr.tool_call_id not in self._seen:
                    self._seen.add(fr.tool_call_id)
                    self._ev("tool", name=fr.function_name, args=fr.arguments)
            elif isinstance(fr, FunctionCallResultFrame):
                key = ("r", fr.tool_call_id)
                if key not in self._seen:
                    self._seen.add(key)
                    self._ev("tool_result", name=fr.function_name, result=str(fr.result)[:200])
            elif isinstance(fr, (BotStartedSpeakingFrame, BotStoppedSpeakingFrame, InterruptionFrame)):
                sib = getattr(fr, "broadcast_sibling_id", None)
                if fr.id in self._seen or (sib is not None and sib in self._seen):
                    return
                self._seen.add(fr.id)
                self._ev({BotStartedSpeakingFrame: "bot_start", BotStoppedSpeakingFrame: "bot_stop"}.get(type(fr), "interruption"))
            elif isinstance(fr, ErrorFrame):
                self._ev("error", message=str(fr.error))

    return ReplayObserver()


# ------------------------------------------------------------------------ report


def build_report(steps: list[dict], events: list[dict], llm, turns_path: Path, cfg, started: float) -> dict:
    # group events by user turn
    turns: list[dict] = []
    cur: dict | None = None
    for e in events:
        k = e["kind"]
        if k == "you_start":
            cur = {"say": e["text"], "t": e["t"], "tools": [], "bot": [], "interrupted_bot": False, "errors": []}
            turns.append(cur)
        elif cur is None:
            continue
        elif k == "tool":
            cur["tools"].append(e["name"])
        elif k == "bot":
            cur["bot"].append(e["text"])
        elif k == "error":
            cur["errors"].append(e["message"])
        elif k == "barge_in":
            cur["barge_in"] = True
    checks = []
    for step, turn in zip(steps, turns):
        want = step.get("expect_tool")
        if want:
            ok = want in turn["tools"]
            checks.append({"say": step["say"], "expect_tool": want, "called": turn["tools"], "ok": ok})
        claimed = any(re.search(r"\b(alarm|reminder)\b.{0,40}\b(set|created|added)\b", b, re.I) for b in turn["bot"])
        if claimed and not turn["tools"]:
            checks.append({"say": step["say"], "expect_tool": "(none claimed)", "called": [], "ok": False, "note": "claimed set without a tool call"})
    recs = []
    if turns_path.exists():
        for line in turns_path.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (r.get("t") or 0) >= started:
                recs.append(r)
    e2e = [r["end_to_end"]["total_ms"] for r in recs if r.get("end_to_end")]
    barge = [r["barge_in"]["stop_ms"] for r in recs if r.get("barge_in") and r["barge_in"].get("stop_ms") is not None]
    prompt_tokens = [c["prompt_tokens"] for r in recs for c in ((r.get("llm") or {}).get("calls") or []) if c.get("prompt_tokens")]
    calls_per_turn = [len((r.get("llm") or {}).get("calls") or []) for r in recs]
    waits = list(getattr(llm, "waits", []))
    import statistics

    def med(v):
        return round(statistics.median(v), 0) if v else None

    def p90(v):
        if not v:
            return None
        s = sorted(v)
        return round(s[min(len(s) - 1, int(0.9 * (len(s) - 1) + 0.999))], 0)

    return {
        "model": cfg.llm_model,
        "reasoning_effort": cfg.llm_reasoning_effort or None,
        "history_messages": cfg.llm_history_messages,
        "started": started,
        "turns": turns,
        "checks": checks,
        "checks_passed": sum(c["ok"] for c in checks),
        "checks_total": len(checks),
        "e2e_ms": {"n": len(e2e), "median": med(e2e), "p90": p90(e2e), "values": [round(v) for v in e2e]},
        "barge_in_stop_ms": {"n": len(barge), "median": med(barge), "values": [round(v) for v in barge]},
        "prompt_tokens": {"n": len(prompt_tokens), "median": med(prompt_tokens), "max": max(prompt_tokens) if prompt_tokens else None},
        "calls_per_turn": calls_per_turn,
        "rate_limit_waits": [{"t": round(w["t"] - started, 1), "retry_after": w["retry_after"]} for w in waits],
        "last_rate_limit_headers": getattr(llm, "rate_limit", {}),
        "events": events,
    }


def print_report(rep: dict) -> None:
    print(f"\n=== replay: {rep['model']} (effort {rep['reasoning_effort'] or 'unset'}, history {rep['history_messages']} msgs) ===")
    for t in rep["turns"]:
        flag = " [barge-in]" if t.get("barge_in") else ""
        print(f"[{t['t']:6.1f}s] you: {t['say']}{flag}")
        for name in t["tools"]:
            print(f"           tool: {name}")
        for b in t["bot"]:
            print(f"           bot: {b}")
        for e in t["errors"]:
            print(f"           ERROR: {e}")
    print()
    for c in rep["checks"]:
        print(f"  {'PASS' if c['ok'] else 'FAIL'}  {c['say'][:50]!r}: expected {c['expect_tool']}, called {c['called']}" + (f" ({c['note']})" if c.get("note") else ""))
    e = rep["e2e_ms"]
    print(f"\n  tool checks {rep['checks_passed']}/{rep['checks_total']} passed")
    print(f"  voice-to-voice (text replay, no ASR): n={e['n']} median {e['median']} ms p90 {e['p90']} ms  {e['values']}")
    b = rep["barge_in_stop_ms"]
    print(f"  barge-in stop: n={b['n']} median {b['median']} ms {b['values']}")
    pt = rep["prompt_tokens"]
    print(f"  prompt tokens per LLM call: n={pt['n']} median {pt['median']} max {pt['max']}")
    print(f"  LLM calls per turn: {rep['calls_per_turn']}")
    w = rep["rate_limit_waits"]
    print(f"  Groq 429 waits: {len(w)}" + (f" -> {[x['retry_after'] for x in w]} s" if w else ""))
    h = rep["last_rate_limit_headers"]
    if h:
        print(f"  last rate-limit headers: {h.get('remaining_tokens')}/{h.get('limit_tokens')} tokens, {h.get('remaining_requests')}/{h.get('limit_requests')} requests")


# -------------------------------------------------------------------------- main


async def run(args) -> int:
    scenario = json.loads(Path(args.scenario).read_text(encoding="utf-8"))
    steps = scenario["steps"]
    cfg = load()
    if args.model:
        cfg.llm_model = args.model
    if args.effort is not None:
        cfg.llm_reasoning_effort = args.effort
    if args.history is not None:
        cfg.llm_history_messages = args.history
    cfg.turns_log = DATA_DIR / "replay_turns.jsonl"
    cfg.latency_log = DATA_DIR / "replay_latency.jsonl"
    setup_logging(cfg.log_level)
    problems = cfg.validate()
    for p in problems:
        logger.error(p)
    if problems:
        return 2

    events: list[dict] = []
    done = asyncio.Event()
    t0 = [time.time()]
    stt = make_stt(steps, args.speed, done, events)
    app = Assistant(cfg)
    worker = await app.build(stt=stt)
    worker.add_observer(make_observer(events, t0))
    t0[0] = time.time()
    started = time.time()

    from pipecat.pipeline.runner import WorkerRunner

    await app.scheduler.start()
    app.bus.emit("state", state="idle")
    logger.info(f"replay: {len(steps)} steps, model {cfg.llm_model}, watch at the UI with ?ws={cfg.ui_ws_port}")
    runner = WorkerRunner(handle_sigint=not sys.platform.startswith("win"))
    await runner.add_workers(worker)

    async def stop_when_done():
        await done.wait()
        await asyncio.sleep(0.5)
        from assistant.turn_metrics import METRICS

        METRICS.flush()
        await worker.cancel(reason="replay finished")

    stopper = asyncio.create_task(stop_when_done())
    try:
        await asyncio.wait_for(runner.run(), timeout=args.timeout)
    except asyncio.TimeoutError:
        logger.error("replay timed out")
    finally:
        stopper.cancel()
        await app.shutdown()

    rep = build_report(steps, events, app.llm, cfg.turns_log, cfg, started)
    print_report(rep)
    out = ROOT / "benchmark" / "replay_results"
    out.mkdir(parents=True, exist_ok=True)
    name = time.strftime("%Y-%m-%d_%H%M%S") + "-" + re.sub(r"[^a-z0-9]+", "-", cfg.llm_model.lower()) + ".json"
    (out / name).write_text(json.dumps(rep, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"\n  report: {out / name}")
    return 0 if rep["checks_passed"] == rep["checks_total"] else 1


def main() -> None:
    ap = argparse.ArgumentParser(description="replay a text conversation through the real pipeline")
    ap.add_argument("scenario")
    ap.add_argument("--model", help="override GROQ_MODEL")
    ap.add_argument("--effort", help="override LLM_REASONING_EFFORT ('' to unset)")
    ap.add_argument("--history", type=int, help="override LLM_HISTORY_MESSAGES")
    ap.add_argument("--speed", type=float, default=1.0, help="speech speed factor")
    ap.add_argument("--timeout", type=float, default=600.0)
    args = ap.parse_args()
    if sys.platform.startswith("win"):
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    try:
        sys.exit(asyncio.run(run(args)))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
