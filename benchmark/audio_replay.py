"""Audio scenario test: real Flux ASR + the whole assistant, with the microphone replaced by WAVs.

Like benchmark/replay.py, but the scripted part is the *audio input*: a FileInputTransport streams
synthesized prompt WAVs (Deepgram Aura, voice orion; side-talk helena) in 20 ms frames at real-time
pace, with optional background noise, gain, and echo (the bot's own output mixed back into the
input, as a speaker/mic without echo cancellation would). Everything after the input transport is
the real assistant: VAD gate, MCP ASR server + Flux, LLM, tools, TTS, speaker.

    .venv\\Scripts\\python -m benchmark.audio_replay benchmark/audio_scenarios/01_baseline.json --out <dir>
    .venv\\Scripts\\python -m benchmark.audio_replay --all                 # every scenario, 75 s apart

Isolated like replay.py: tools server on port 8767 with its own data/audio_replay.db (deleted
before every scenario, so old alarms/reminders cannot fire mid-test), UI events on
ws://127.0.0.1:8768, turn records in data/replay_turns.jsonl.

Scenario file:
  {"name": "...", "noise_db": -30 | null, "echo_db": -10 | null, "lead_secs": 20,
   "steps": [{"say": "...", "voice": "orion"|"helena", "gain_db": 0,
              "parts": [{"say": "..."}, {"silence": 1.5}, ...],      # instead of "say"
              "when": "idle"|"speaking"|"after_prompt"|"alarm_fired", "after": 1.0,
              "reply_cap": 30, "expect_tool": "create_alarm", "expect_args": {"time": "07:00"},
              "expect_reply": true}]}
  "say" may contain {next_minute} (HH:MM of the next full minute, computed at run time).
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import re
import subprocess
import sys
import time
import wave
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("ASSISTANT_DB", "audio_replay.db")  # reset before every scenario
os.environ.setdefault("TOOLS_MCP_PORT", "8767")
os.environ.setdefault("TOOLS_MCP_URL", "http://127.0.0.1:8767/mcp")
os.environ.setdefault("UI_WS_PORT", "8768")

import numpy as np  # noqa: E402
from loguru import logger  # noqa: E402

from assistant.config import DATA_DIR, load  # noqa: E402
from assistant.main import Assistant, setup_logging  # noqa: E402

SR = 16000
FRAME = SR // 50  # 20 ms
PROMPT_DIR = ROOT / "benchmark" / "audio" / "prompts"
SCEN_DIR = ROOT / "benchmark" / "audio_scenarios"
VOICES = {"orion": "aura-2-orion-en", "helena": "aura-2-helena-en"}


# ------------------------------------------------------------------ prompt audio


def slug(text: str, voice: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60]
    return f"{voice}-{s}"


def synth(text: str, voice: str, api_key: str) -> np.ndarray:
    """Deepgram Aura REST -> int16 mono 16 kHz, cached in benchmark/audio/prompts/."""
    PROMPT_DIR.mkdir(parents=True, exist_ok=True)
    path = PROMPT_DIR / f"{slug(text, voice)}.wav"
    if not path.exists():
        import httpx

        r = httpx.post(
            "https://api.deepgram.com/v1/speak",
            params={"model": VOICES[voice], "encoding": "linear16", "sample_rate": SR, "container": "wav"},
            headers={"Authorization": f"Token {api_key}", "Content-Type": "application/json"},
            json={"text": text},
            timeout=30,
        )
        r.raise_for_status()
        path.write_bytes(r.content)
    with wave.open(str(path), "rb") as w:
        assert w.getframerate() == SR and w.getnchannels() == 1 and w.getsampwidth() == 2, path
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).copy()


def pink_noise(secs: float, seed: int = 1) -> np.ndarray:
    """Unit-RMS pink noise (1/f spectrum) as float32."""
    n = int(secs * SR)
    rng = np.random.default_rng(seed)
    spec = rng.normal(size=n // 2 + 1) + 1j * rng.normal(size=n // 2 + 1)
    f = np.arange(n // 2 + 1)
    f[0] = 1
    x = np.fft.irfft(spec / np.sqrt(f), n).astype(np.float32)
    return x / (np.sqrt(np.mean(x**2)) + 1e-9)


def db(v: float | None) -> float:
    return 0.0 if v is None else float(10 ** (v / 20))


def rms_dbfs(x: np.ndarray) -> float:
    r = np.sqrt(np.mean((x.astype(np.float64) / 32768) ** 2)) if len(x) else 0
    return round(20 * np.log10(r + 1e-12), 1)


def build_prompt(step: dict, api_key: str, ctx: dict) -> np.ndarray:
    voice = step.get("voice", "orion")
    parts = step.get("parts") or [{"say": step["say"]}]
    pieces = []
    for p in parts:
        if "silence" in p:
            pieces.append(np.zeros(int(p["silence"] * SR), dtype=np.int16))
        else:
            pieces.append(synth(p["say"].format(**ctx), p.get("voice", voice), api_key))
    audio = np.concatenate(pieces).astype(np.float32) * db(step.get("gain_db") or 0.0)
    return np.clip(audio, -32768, 32767).astype(np.int16)


def step_text(step: dict, ctx: dict) -> str:
    if "parts" in step:
        return " ".join(p["say"].format(**ctx) for p in step["parts"] if "say" in p)
    return step["say"].format(**ctx)


# --------------------------------------------------------------------- mixer


class Mixer:
    """Builds each 20 ms input frame: prompt (if playing) + looped noise + bot echo."""

    def __init__(self, noise_db: float | None, echo_db: float | None):
        self.noise = pink_noise(30.0) * 32768 * db(noise_db) if noise_db is not None else None
        self.noise_pos = 0
        self.echo_gain = db(echo_db) if echo_db is not None else None
        self.echo = np.zeros(0, dtype=np.float32)
        self.prompt: np.ndarray | None = None
        self.prompt_pos = 0
        self.prompt_done: asyncio.Event | None = None

    def play(self, audio: np.ndarray) -> asyncio.Event:
        self.prompt = audio
        self.prompt_pos = 0
        self.prompt_done = asyncio.Event()
        return self.prompt_done

    @property
    def playing(self) -> bool:
        return self.prompt is not None

    def add_echo(self, pcm: bytes) -> None:
        if self.echo_gain is None:
            return
        x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) * self.echo_gain
        self.echo = np.concatenate([self.echo, x])[-SR * 30 :]

    def next_frame(self) -> bytes:
        out = np.zeros(FRAME, dtype=np.float32)
        if self.prompt is not None:
            chunk = self.prompt[self.prompt_pos : self.prompt_pos + FRAME].astype(np.float32)
            out[: len(chunk)] += chunk
            self.prompt_pos += FRAME
            if self.prompt_pos >= len(self.prompt):
                self.prompt = None
                if self.prompt_done:
                    self.prompt_done.set()
        if self.noise is not None:
            idx = (np.arange(FRAME) + self.noise_pos) % len(self.noise)
            out += self.noise[idx]
            self.noise_pos = (self.noise_pos + FRAME) % len(self.noise)
        if self.echo_gain is not None and len(self.echo):
            e = self.echo[:FRAME]
            out[: len(e)] += e
            self.echo = self.echo[FRAME:]
        return np.clip(out, -32768, 32767).astype(np.int16).tobytes()


# ----------------------------------------------------------------- transport


def make_file_transport(cfg, mixer: Mixer):
    import pyaudio
    from pipecat.frames.frames import InputAudioRawFrame, OutputAudioRawFrame, StartFrame
    from pipecat.transports.base_input import BaseInputTransport
    from pipecat.transports.base_transport import BaseTransport
    from pipecat.transports.local.audio import LocalAudioOutputTransport, LocalAudioTransportParams

    from assistant.main import resolve_device

    params = LocalAudioTransportParams(
        audio_in_enabled=True,
        audio_out_enabled=True,
        audio_in_sample_rate=SR,
        audio_out_sample_rate=cfg.audio_out_sample_rate,
        audio_in_channels=1,
        audio_out_channels=1,
        output_device_index=resolve_device(cfg.audio_output_device, want_input=False),
    )

    class FileInputTransport(BaseInputTransport):
        def __init__(self, params):
            super().__init__(params)
            self._feeder: asyncio.Task | None = None

        async def start(self, frame: StartFrame):
            await super().start(frame)
            await self.set_transport_ready(frame)
            if self._feeder is None:
                self._feeder = asyncio.create_task(self._feed(), name="file-input-feeder")

        async def _feed(self):
            t = time.perf_counter()
            try:
                while True:
                    await self.push_audio_frame(InputAudioRawFrame(audio=mixer.next_frame(), sample_rate=SR, num_channels=1))
                    t += FRAME / SR
                    await asyncio.sleep(max(0.0, t - time.perf_counter()))
            except asyncio.CancelledError:
                return

        async def _stop_feeder(self):
            if self._feeder:
                self._feeder.cancel()
                self._feeder = None

        async def stop(self, frame):
            await self._stop_feeder()
            await super().stop(frame)

        async def cancel(self, frame):
            await self._stop_feeder()
            await super().cancel(frame)

    class EchoOutputTransport(LocalAudioOutputTransport):
        async def write_audio_frame(self, frame: OutputAudioRawFrame) -> bool:
            ok = await super().write_audio_frame(frame)
            if ok and frame.sample_rate == SR:
                mixer.add_echo(frame.audio)
            return ok

    class FileTransport(BaseTransport):
        def __init__(self):
            super().__init__()
            self._pa = pyaudio.PyAudio()
            self._in = FileInputTransport(params)
            self._out = EchoOutputTransport(self._pa, params)

        def input(self):
            return self._in

        def output(self):
            return self._out

    return FileTransport()


# ------------------------------------------------------------------ observer


def make_observer(state: dict, log):
    from pipecat.frames.frames import BotStartedSpeakingFrame, BotStoppedSpeakingFrame
    from pipecat.observers.base_observer import BaseObserver, FramePushed

    class BotStateObserver(BaseObserver):
        def __init__(self):
            super().__init__(observe_every_push=False)
            self._seen: set = set()

        async def on_push_frame(self, data: FramePushed):
            fr = data.frame
            if isinstance(fr, (BotStartedSpeakingFrame, BotStoppedSpeakingFrame)):
                sib = getattr(fr, "broadcast_sibling_id", None)
                if fr.id in self._seen or (sib is not None and sib in self._seen):
                    return
                self._seen.add(fr.id)
                if isinstance(fr, BotStartedSpeakingFrame):
                    state["bot_speaking"] = True
                    state["reply_pending"] = False
                    log("bot_start")
                else:
                    state["bot_speaking"] = False
                    state["t_bot_stopped"] = time.time()
                    log("bot_stop")

    return BotStateObserver()


# --------------------------------------------------------------------- WER


def _words(s: str) -> list[str]:
    return re.sub(r"[^a-z0-9' ]+", " ", s.lower().replace("-", " ")).split()


def wer(ref: str, hyp: str) -> float | None:
    r, h = _words(ref), _words(hyp)
    if not r:
        return None
    d = list(range(len(h) + 1))
    for i, rw in enumerate(r, 1):
        prev, d[0] = d[0], i
        for j, hw in enumerate(h, 1):
            cur = min(d[j] + 1, d[j - 1] + 1, prev + (rw != hw))
            prev, d[j] = d[j], cur
    return round(d[len(h)] / len(r), 3)


# ------------------------------------------------------------------------ run


async def run_scenario(path: Path, out_dir: Path) -> dict:
    sc = json.loads(path.read_text(encoding="utf-8"))
    for suffix in ("", "-wal", "-shm"):
        (DATA_DIR / (os.environ["ASSISTANT_DB"] + suffix)).unlink(missing_ok=True)
    cfg = load()
    cfg.turns_log = DATA_DIR / "replay_turns.jsonl"
    cfg.latency_log = DATA_DIR / "replay_latency.jsonl"
    setup_logging(cfg.log_level)
    log_path = out_dir / f"{path.stem}.console.log"
    sink = logger.add(str(log_path), level="DEBUG", format="{time:HH:mm:ss.SSS} | {level: <7} | {message}", encoding="utf-8")

    nm = (datetime.now() + timedelta(minutes=1)).replace(second=0, microsecond=0)
    if (nm - datetime.now()).total_seconds() < 25:  # leave time to say it and for the LLM
        nm += timedelta(minutes=1)
    ctx = {"next_minute": nm.strftime("%H:%M"), "next_minute_spoken": nm.strftime("%I:%M %p").lstrip("0")}

    steps = sc["steps"]
    prompts = [build_prompt(s, cfg.deepgram_api_key, ctx) for s in steps]
    mixer = Mixer(sc.get("noise_db"), sc.get("echo_db"))
    t0 = time.time()
    timeline: list[dict] = []

    def log(kind: str, **f):
        timeline.append({"t": round(time.time() - t0, 3), "kind": kind, **f})

    state = {"bot_speaking": False, "reply_pending": False, "t_bot_stopped": time.time(), "t_spoke": 0.0}
    app = Assistant(cfg)
    worker = await app.build(transport=make_file_transport(cfg, mixer))
    worker.add_observer(make_observer(state, log))

    ws_events: list[dict] = []
    alarm_fired = asyncio.Event()

    async def ws_capture():
        import websockets

        for _ in range(50):
            try:
                async with websockets.connect(f"ws://127.0.0.1:{cfg.ui_ws_port}", max_size=None) as ws:
                    async for raw in ws:
                        m = json.loads(raw)
                        m["_t"] = round(time.time() - t0, 3)
                        m["_prompt_playing"] = mixer.playing
                        ws_events.append(m)
                        if m.get("type") == "alarm_fired":
                            alarm_fired.set()
                return
            except (OSError, Exception):  # noqa: BLE001
                await asyncio.sleep(0.2)

    steps_out: list[dict] = []
    done = asyncio.Event()

    async def wait_for(step: dict):
        when = step.get("when", "idle")
        after = float(step.get("after", 1.0))
        cap = float(step.get("reply_cap", 30))
        deadline = time.time() + 150
        if when == "speaking":
            while not state["bot_speaking"] and time.time() < deadline:
                await asyncio.sleep(0.02)
            await asyncio.sleep(after)
        elif when == "after_prompt":
            await asyncio.sleep(after)
        elif when == "alarm_fired":
            try:
                await asyncio.wait_for(alarm_fired.wait(), timeout=float(step.get("timeout", 120)))
            except asyncio.TimeoutError:
                log("alarm_wait_timeout")
            await asyncio.sleep(after)
        else:
            while time.time() < deadline:
                if (
                    (not state["reply_pending"] or time.time() - state["t_spoke"] > cap)
                    and not state["bot_speaking"]
                    and time.time() - state["t_bot_stopped"] >= after
                ):
                    return
                await asyncio.sleep(0.05)

    async def script():
        try:
            lead = float(sc.get("lead_secs", 2.0))
            await asyncio.sleep(lead)
            for i, (step, audio) in enumerate(zip(steps, prompts)):
                await wait_for(step)
                text = step_text(step, ctx)
                rec = {
                    "i": i,
                    "text": text,
                    "voice": step.get("voice", "orion"),
                    "gain_db": step.get("gain_db"),
                    "prompt_secs": round(len(audio) / SR, 2),
                    "prompt_dbfs": rms_dbfs(audio),
                    "t_start": round(time.time() - t0, 3),
                    "bot_speaking_at_start": state["bot_speaking"],
                }
                log("prompt_start", i=i, text=text)
                ev = mixer.play(audio)
                await ev.wait()
                rec["t_end"] = round(time.time() - t0, 3)
                log("prompt_end", i=i)
                state["reply_pending"] = True
                state["t_spoke"] = time.time()
                steps_out.append(rec)
            # let the last reply finish
            until = time.time() + float(sc.get("tail_cap", 30))
            while state["reply_pending"] and time.time() < until:
                await asyncio.sleep(0.1)
            while state["bot_speaking"]:
                await asyncio.sleep(0.1)
            await asyncio.sleep(float(sc.get("tail_secs", 3.0)))
        except asyncio.CancelledError:
            return
        except Exception as e:  # noqa: BLE001
            logger.exception(f"audio replay script failed: {e!r}")
        finally:
            done.set()

    from pipecat.pipeline.runner import WorkerRunner

    await app.scheduler.start()
    app.bus.emit("state", state="idle")
    if getattr(app, "notifier", None):
        app.notifier.enable()  # as Assistant.run() does: log-only warnings become UI events
    started = time.time()
    logger.info(f"audio replay {path.name}: {len(steps)} steps, model {cfg.llm_model}, noise {sc.get('noise_db')}, echo {sc.get('echo_db')}")
    runner = WorkerRunner(handle_sigint=not sys.platform.startswith("win"))
    await runner.add_workers(worker)
    cap_task = asyncio.create_task(ws_capture())
    script_task = asyncio.create_task(script())

    async def stopper():
        await done.wait()
        await asyncio.sleep(0.5)
        from assistant.turn_metrics import METRICS

        METRICS.flush()
        await worker.cancel(reason="audio replay finished")

    stop_task = asyncio.create_task(stopper())
    try:
        await asyncio.wait_for(runner.run(), timeout=float(sc.get("timeout", 420)))
    except asyncio.TimeoutError:
        logger.error("audio replay timed out")
    finally:
        script_task.cancel()
        stop_task.cancel()
        cap_task.cancel()
        try:
            await app.shutdown()
        except Exception as e:  # noqa: BLE001
            logger.error(f"shutdown failed: {e!r}")

    rep = build_report(sc, path, steps, steps_out, ws_events, timeline, app.llm, cfg, started, t0, ctx)
    (out_dir / f"{path.stem}.json").write_text(json.dumps(rep, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
    logger.remove(sink)
    return rep


# --------------------------------------------------------------------- report


def build_report(sc, path, steps, steps_out, ws_events, timeline, llm, cfg, started, t0, ctx) -> dict:
    recs = []
    if cfg.turns_log.exists():
        for line in cfg.turns_log.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (r.get("t") or 0) >= started:
                recs.append(r)
    user_recs = [r for r in recs if r.get("kind") == "user"]

    # prompt windows (with 1.5 s grace after each prompt: Flux reports start/end a bit late)
    windows = [(s["t_start"], s.get("t_end", s["t_start"]) + 1.5) for s in steps_out]

    def in_prompt(t: float) -> bool:
        return any(a - 0.2 <= t <= b for a, b in windows)

    listening = [e for e in ws_events if e.get("type") == "state" and e.get("state") == "listening"]
    # The UI bus sends "listening" once per user turn start (UserStartedSpeakingFrame).
    false_turns = [e["_t"] for e in listening if not in_prompt(e["_t"])]
    interrupts = [e for e in ws_events if e.get("type") == "interrupted"]
    self_interrupts = [e["_t"] for e in interrupts if not in_prompt(e["_t"])]

    # assign events to steps by time
    bounds = [s["t_start"] for s in steps_out] + [1e9]
    for k, s in enumerate(steps_out):
        a, b = bounds[k], bounds[k + 1]
        evs = [e for e in ws_events if a <= e["_t"] < b]
        finals = [e["text"] for e in evs if e.get("type") == "transcript" and e.get("final")]
        s["transcripts"] = finals
        heard = " ".join(finals)
        s["wer"] = wer(s["text"], heard) if finals else None
        s["turn_detected"] = bool(finals) or any(e.get("type") == "state" and e.get("state") == "listening" for e in evs)
        s["tools"] = [{"name": e.get("name"), "args": e.get("args")} for e in evs if e.get("type") == "tool_call"]
        s["bot"] = [e.get("text") for e in evs if e.get("type") == "assistant_text"]
        s["warnings"] = [e.get("message") for e in evs if e.get("type") in ("warning", "error")]
        s["llm_waits"] = [e.get("seconds") for e in evs if e.get("type") == "llm_wait"]
        s["interrupted"] = [e["_t"] for e in evs if e.get("type") == "interrupted"]
        s["alarm_events"] = [{"type": e["type"], "t": e["_t"]} for e in evs if e.get("type", "").startswith("alarm_")]
        # turn record whose transcript matches one of the finals (or the first user record in the window)
        tr = None
        for r in user_recs:
            rt = (r.get("t") or 0) - t0
            # time-bounded too: identical prompts in two steps must not share the first one's record
            if a <= rt < b + 60 and r.get("transcript") and any(r["transcript"].strip() == f.strip() for f in finals):
                tr = r
                break
        if tr is None:
            cands = [r for r in user_recs if a <= (r.get("t") or 0) - t0 < b + 60]
            tr = cands[0] if cands else None
        s["e2e_ms"] = (tr.get("end_to_end") or {}).get("total_ms") if tr else None
        s["barge_in_stop_ms"] = (tr.get("barge_in") or {}).get("stop_ms") if tr and tr.get("barge_in") else None
        s["turn_note"] = tr.get("note") if tr else None
        want = steps[k].get("expect_tool")
        if want:
            names = [t["name"] for t in s["tools"]]
            ok = want in names
            exp_args = steps[k].get("expect_args") or {}
            if ok and exp_args:
                ok = any(
                    t["name"] == want and all(str((t["args"] or {}).get(kk)) == str(vv).format(**ctx) for kk, vv in exp_args.items())
                    for t in s["tools"]
                )
            s["tool_check"] = {"expect": want, "args": exp_args, "ok": ok}
        if steps[k].get("expect_reply") is False:
            s["no_reply_check"] = {"ok": not s["bot"]}

    # alarm: ring -> announcement
    ring = next((e for e in ws_events if e.get("type") == "alarm_fired"), None)
    ring_info = None
    if ring:
        ann = next((x for x in timeline if x["kind"] == "bot_start" and x["t"] >= ring["_t"] - 0.05), None)
        stop = next((e for e in ws_events if e.get("type") == "alarm_stopped" and e["_t"] >= ring["_t"]), None)
        ring_info = {
            "fired_t": ring["_t"],
            "announcement_ms": round((ann["t"] - ring["_t"]) * 1000) if ann else None,
            "stopped_t": stop["_t"] if stop else None,
        }

    waits = [w.get("retry_after") for w in getattr(llm, "waits", []) if (w.get("t") or 0) >= started]
    e2e = [s["e2e_ms"] for s in steps_out if s.get("e2e_ms")]
    wers = [s["wer"] for s in steps_out if s.get("wer") is not None]
    return {
        "scenario": sc.get("name", path.stem),
        "file": path.name,
        "model": cfg.llm_model,
        "noise_db": sc.get("noise_db"),
        "echo_db": sc.get("echo_db"),
        "context": ctx,
        "steps": steps_out,
        "turns_detected": sum(1 for s in steps_out if s["turn_detected"]),
        "wer_mean": round(sum(wers) / len(wers), 3) if wers else None,
        "e2e_ms": e2e,
        "barge_in_ms": [s["barge_in_stop_ms"] for s in steps_out if s.get("barge_in_stop_ms") is not None],
        "false_turns": false_turns,
        "self_interruptions": self_interrupts,
        "llm_429_waits": waits,
        "alarm": ring_info,
        "user_turn_records": len(user_recs),
        "timeline": timeline,
        "ws_events": ws_events,
    }


# ----------------------------------------------------------------------- main


def main() -> None:
    ap = argparse.ArgumentParser(description="audio scenario test through the real pipeline")
    ap.add_argument("scenario", nargs="?")
    ap.add_argument("--all", action="store_true", help="run every scenario in benchmark/audio_scenarios, one process each")
    ap.add_argument("--only", help="comma-separated scenario name prefixes with --all, e.g. 01,03")
    ap.add_argument("--out", help="output folder")
    ap.add_argument("--pause", type=float, default=75.0, help="seconds between scenarios (Groq tokens/min reset)")
    args = ap.parse_args()
    if args.all:
        out = Path(args.out) if args.out else ROOT / "benchmark" / "audio_results" / time.strftime("%Y-%m-%d_%H%M%S")
        out.mkdir(parents=True, exist_ok=True)
        files = sorted(SCEN_DIR.glob("*.json"))
        if args.only:
            keep = tuple(args.only.split(","))
            files = [f for f in files if f.stem.startswith(keep)]
        for i, f in enumerate(files):
            if i:
                time.sleep(args.pause)
            print(f"=== {f.name} ===", flush=True)
            with open(out / f"{f.stem}.stdout.log", "w", encoding="utf-8") as fh:
                subprocess.run([sys.executable, "-m", "benchmark.audio_replay", str(f), "--out", str(out)], cwd=ROOT, stdout=fh, stderr=subprocess.STDOUT)
        from benchmark.audio_summary import write_summary

        print(write_summary(out))
        return
    out = Path(args.out) if args.out else ROOT / "benchmark" / "audio_results" / time.strftime("%Y-%m-%d_%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    if sys.platform.startswith("win"):
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    rep = asyncio.run(run_scenario(Path(args.scenario), out))
    print(json.dumps({k: v for k, v in rep.items() if k not in ("timeline", "ws_events")}, indent=1, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
