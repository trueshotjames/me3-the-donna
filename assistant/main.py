"""ME3 voice assistant, Design A: Pipecat on the Pi, speech and brain in the cloud.

    python -m assistant.main                # run
    python -m assistant.main --list-devices # print audio devices and exit
    python -m assistant.main --check        # check keys and MCP servers, then exit

Pipeline (all on the Pi):
    mic -> Silero VAD (gate only) -> Wav2Small voice tone -> MCPSTTService -> [ASR MCP server -> Deepgram Flux]
        -> user context aggregator -> voice hint -> fast LLM via OpenRouter (+ tools MCP server, + delegate, + recall)
        -> PassFilter -> MCPTTSService -> [TTS MCP server -> Deepgram Aura-2] -> speaker

    delegate(task) hands multi-step work to Hermes Agent (local API server) in the background;
    the result comes back as a message the fast model speaks (assistant/tasks.py).
    Everything she says on her own (results, reminders, alarms) goes through one queue that waits
    for the user's current query to be answered (assistant/announcer.py).
    Long-term memory: a note in the prompt plus raw transcripts and a recall tool (assistant/memory.py).

UI events go out over a local WebSocket (assistant/EVENTS.md).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time

from loguru import logger

from assistant.config import DATA_DIR, ROOT, Config, load

# ----------------------------------------------------------------------------- helpers


def setup_logging(level: str) -> None:
    import logging

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("mcp").setLevel(logging.WARNING)
    logger.remove()
    logger.add(sys.stderr, level=level, format="<green>{time:HH:mm:ss.SSS}</green> | <level>{level: <7}</level> | {message}")


def list_audio_devices() -> list[dict]:
    import pyaudio

    pa = pyaudio.PyAudio()
    devices = []
    try:
        for i in range(pa.get_device_count()):
            d = pa.get_device_info_by_index(i)
            devices.append(
                {
                    "index": i,
                    "name": d.get("name"),
                    "in": int(d.get("maxInputChannels", 0)),
                    "out": int(d.get("maxOutputChannels", 0)),
                    "rate": int(d.get("defaultSampleRate", 0)),
                }
            )
    finally:
        pa.terminate()
    return devices


def resolve_device(spec: int | str | None, *, want_input: bool) -> int | None:
    """Map an index or a case-insensitive name fragment to a PyAudio device index."""
    if spec is None:
        return None
    if isinstance(spec, int):
        return spec
    key = "in" if want_input else "out"
    for d in list_audio_devices():
        if d[key] > 0 and spec.lower() in str(d["name"]).lower():
            logger.info(f"audio {'input' if want_input else 'output'} device: [{d['index']}] {d['name']}")
            return d["index"]
    logger.warning(f"no {'input' if want_input else 'output'} device matches {spec!r}; using default")
    return None


async def connect_tools_server(cfg: Config):
    """Connect to the tools MCP server over HTTP, starting it if nobody else did."""
    from assistant.mcp_bridge import MCPBridge

    proc = None
    deadline = time.time() + 20
    attempt = 0
    while True:
        bridge = MCPBridge("tools", url=cfg.tools_mcp_url)
        try:
            await bridge.start(timeout=5)
            return bridge, proc
        except Exception as e:  # noqa: BLE001
            attempt += 1
            await bridge.close()
            if proc is None and cfg.tools_mcp_autostart:
                logger.info(f"tools MCP server not reachable at {cfg.tools_mcp_url}; starting it")
                env = dict(os.environ, PYTHONPATH=str(ROOT) + os.pathsep + os.environ.get("PYTHONPATH", ""))
                proc = subprocess.Popen([sys.executable, "-m", "mcp_servers.tools.server"], cwd=str(ROOT), env=env)
            if time.time() > deadline:
                raise RuntimeError(f"tools MCP server did not come up at {cfg.tools_mcp_url}: {e}") from e
            await asyncio.sleep(0.5 if attempt < 6 else 1.0)


# ------------------------------------------------------------------------------ main


def make_transport(cfg: Config):
    """Audio in/out through the USB speakerphone (hardware echo cancellation).

    No VAD here: Pipecat 1.13 ignores ``vad_analyzer`` on transport params. The VAD is a
    VADProcessor in the pipeline (see build()).
    """
    from pipecat.transports.local.audio import LocalAudioTransport, LocalAudioTransportParams

    return LocalAudioTransport(
        LocalAudioTransportParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_in_sample_rate=cfg.audio_in_sample_rate,
            audio_out_sample_rate=cfg.audio_out_sample_rate,
            audio_in_channels=1,
            audio_out_channels=1,
            input_device_index=resolve_device(cfg.audio_input_device, want_input=True),
            output_device_index=resolve_device(cfg.audio_output_device, want_input=False),
        )
    )


class Assistant:
    """Everything the pipeline needs, wired together. `build()` then `run()`."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.bus = None
        self.scheduler = None
        self.worker = None
        self.stt = self.tts = self.llm = self.vad = None
        self.tools_client = self.tools_bridge = None
        self.tools_proc = None
        self.hermes = self.tasks = None
        self.memory = None
        self.emotion = self.hint = None
        self.settings = None
        self.notifier = None
        self.announcer = None
        self.chat = self.net = self.levels = None

    async def build(self, transport=None, stt=None):
        """Wire the pipeline. ``transport``/``stt`` can be replaced (benchmark/replay.py feeds scripted turns)."""
        cfg = self.cfg
        from mcp.client.session_group import StreamableHttpParameters
        from pipecat.observers.user_bot_latency_observer import UserBotLatencyObserver
        from pipecat.pipeline.pipeline import Pipeline
        from pipecat.pipeline.worker import PipelineParams, PipelineWorker
        from pipecat.processors.aggregators.llm_context import LLMContext
        from pipecat.processors.aggregators.llm_response_universal import (
            LLMContextAggregatorPair,
            LLMUserAggregatorParams,
        )
        from pipecat.adapters.schemas.tools_schema import ToolsSchema
        from pipecat.audio.vad.vad_analyzer import VADParams
        from pipecat.processors.audio.vad_processor import VADProcessor
        from pipecat.services.mcp_service import MCPClient

        from assistant.emotion import EmotionProcessor, VoiceHintInjector, Wav2SmallScorer
        from assistant.mcp_bridge import stdio_params
        from assistant.memory import Memory, MemoryStore, Summarizer
        from assistant.processors import PassFilter
        from assistant.prompts import system_prompt
        from assistant.scheduler import Scheduler
        from assistant.settings_ops import SettingsOps
        from assistant.services.hermes_client import HermesClient, HermesError
        from assistant.services.mcp_stt import MCPSTTService
        from assistant.services.mcp_tts import MCPTTSService
        from assistant.services.timed_llm import TimedGroqLLMService as GroqLLMService
        from assistant.announcer import ActivityTracker, Announcer
        from assistant.tasks import TaskManager
        from assistant.turn_metrics import METRICS, TurnMetricsObserver
        from assistant.vad import RecordingSileroVAD
        from assistant.ui_bus import UIBus
        from assistant.ui_events import UIEventObserver
        from mcp_servers.tools.catalog import LLM_TOOLS

        # UI bus first so the launcher/UI can connect while the rest comes up.
        self.bus = bus = UIBus(cfg.ui_ws_host, cfg.ui_ws_port)
        await bus.start()
        bus.emit("state", state="starting")
        # Warnings/errors that only reach the log become UI notifications (after "assistant ready").
        from assistant.ui_notify import LogNotifier

        self.notifier = LogNotifier(bus)
        self.notifier.install()

        # UI extras: session chat log, mic/speaker level meter, internet status (observers and
        # background loops only; the pipeline itself is unchanged).
        from assistant.chat_log import ChatLog
        from assistant.levels import LevelMeter
        from assistant.netstatus import NetMonitor, targets_from

        self.chat = ChatLog(bus)
        bus.add_snapshot_provider(self.chat.snapshot)
        bus.add_command("chat_clear", self.chat.handle_command)
        self.net = NetMonitor(bus, targets_from(cfg), events_path=DATA_DIR / "net_events.jsonl")
        bus.add_snapshot_provider(self.net.snapshot)
        bus.add_command("net_check", self.net.handle_command)
        self.net.start()
        self.levels = LevelMeter(bus)

        self.tools_bridge, self.tools_proc = await connect_tools_server(cfg)
        self.scheduler = scheduler = Scheduler(
            self.tools_bridge, bus, poll_secs=cfg.scheduler_poll_secs, reannounce_secs=cfg.alarm_repeat_announce_secs
        )

        transport = transport or make_transport(cfg)

        # High-recall VAD: it only opens the audio gate to Flux. Flux decides turns and interruptions.
        self.vad = vad = RecordingSileroVAD(
            params=VADParams(
                confidence=cfg.vad_confidence,
                start_secs=cfg.vad_start_secs,
                stop_secs=cfg.vad_stop_secs,
                min_volume=cfg.vad_min_volume,
            )
        )
        METRICS.path = cfg.turns_log
        METRICS.tool_exec_path = DATA_DIR / "tool_exec.jsonl"
        METRICS.vad = vad
        METRICS.vad_thresholds = (cfg.vad_confidence, cfg.vad_min_volume)

        def output_latency() -> float | None:
            stream = getattr(transport.output(), "_out_stream", None)
            return stream.get_output_latency() if stream is not None else None

        METRICS.output_latency_fn = output_latency
        METRICS.on_problem = lambda msg: bus.emit("warning", message=msg, kind="turn_problem")
        METRICS.net_info = self.net.turn_block

        self.stt = stt = stt or MCPSTTService(
            server_params=stdio_params("mcp_servers.asr.server"),
            sample_rate=cfg.audio_in_sample_rate,
            push_interval_ms=cfg.asr_push_ms,
            eot_threshold=cfg.flux_eot_threshold,
            eager_eot_threshold=cfg.flux_eager_eot_threshold,
            eot_timeout_ms=cfg.flux_eot_timeout_ms,
            enable_eager_end_of_turn=cfg.eager_end_of_turn,
            turn_start=cfg.turn_start,
            gate_enabled=cfg.gate_enabled,
            preroll_ms=cfg.preroll_ms,
            bot_hangover_secs=cfg.bot_hangover_secs,
            gate_noise_tail_secs=cfg.gate_noise_tail_secs,
        )

        self.tts = tts = MCPTTSService(
            server_params=stdio_params("mcp_servers.tts.server"),
            voice=cfg.tts_voice,
            server_sample_rate=cfg.tts_sample_rate,
            playout_enabled=cfg.tts_playout,
            playout_start_ms=cfg.tts_playout_start_ms,
            playout_max_ms=cfg.tts_playout_max_ms,
            playout_max_hold_ms=cfg.tts_playout_max_hold_ms,
        )

        async def on_rate_limited(retry_after: float, info: dict) -> None:
            # Groq answered 429; the SDK waits retry-after and retries. Say so instead of going silent.
            bus.emit("llm_wait", seconds=retry_after, remaining_tokens=info.get("remaining_tokens"), limit_tokens=info.get("limit_tokens"))
            if retry_after >= 2.0 and self.scheduler is not None:
                await self.scheduler.say_now("One moment.", source="llm_wait")

        # Long-term memory: a note in the prompt, raw transcripts on disk, a recall tool. The note is
        # refreshed by a background model call when a conversation ends (assistant/memory.py).
        if cfg.memory_enabled:
            summarizer = Summarizer(
                cfg.llm_api_key,
                cfg.llm_base_url,
                cfg.memory_model or cfg.llm_model,
                reasoning_effort=cfg.llm_reasoning_effort if "gpt-oss" in (cfg.memory_model or cfg.llm_model) else "",
                extra_body=cfg.llm_extra_body,
            )
            self.memory = Memory(
                MemoryStore(cfg.memory_dir),
                summarizer,
                user_name=cfg.user_name,
                assistant_name=cfg.assistant_name,
                idle_secs=cfg.memory_idle_secs,
                episode_messages=cfg.memory_episode_messages,
                note_max_chars=cfg.memory_note_max_chars,
                on_note=lambda note: self._set_memory_note(note),
                on_event=bus.emit,
            )
            logger.info(f"memory on: {cfg.memory_dir} (note {len(self.memory.note)} chars; summaries by {summarizer.model})")

        # Fast model: OpenAI-compatible endpoint (OpenRouter by default; provider pin in llm_extra_body).
        self.llm = llm = GroqLLMService(
            api_key=cfg.llm_api_key,
            base_url=cfg.llm_base_url,
            history_messages=cfg.llm_history_messages,
            on_rate_limited=on_rate_limited,
            on_messages=self.memory.observe if self.memory else None,
            settings=GroqLLMService.Settings(
                model=cfg.llm_model,
                temperature=cfg.llm_temperature,
                max_tokens=cfg.llm_max_tokens,
                # "none" turns thinking off on Qwen; gpt-oss accepts low/medium/high; leave
                # LLM_REASONING_EFFORT empty for models that reject the parameter.
                **({"reasoning_effort": cfg.llm_reasoning_effort} if cfg.llm_reasoning_effort else {}),
                **({"extra": {"extra_body": cfg.llm_extra_body}} if cfg.llm_extra_body else {}),
            ),
        )
        logger.info(f"fast LLM: {cfg.llm_model} at {cfg.llm_base_url} {cfg.llm_extra_body or ''}")

        self.tools_client = MCPClient(StreamableHttpParameters(url=cfg.tools_mcp_url), tools_filter=LLM_TOOLS)
        tools = await self.tools_client.tools()
        standard_tools = list(tools.standard_tools)

        # Hermes: background tasks through delegate / task_status. Skipped (with a warning) when the
        # API server is not reachable, so the assistant still runs without it.
        if cfg.hermes_enabled:
            self.hermes = HermesClient(cfg.hermes_url, cfg.hermes_api_key)
            try:
                info = await self.hermes.check()
                logger.info(f"Hermes reachable at {cfg.hermes_url}: {info}")
                self.tasks = TaskManager(
                    self.hermes,
                    nudge_secs=cfg.hermes_nudge_secs,
                    max_secs=cfg.hermes_max_secs,
                    on_event=bus.emit,
                    on_record=METRICS.task_record,
                )
                standard_tools += self.tasks.register(llm)
            except HermesError as e:
                logger.warning(f"{e}; running without delegate (set HERMES_ENABLED=0 to silence this)")
                await self.hermes.close()
                self.hermes = None
        if self.memory is not None:
            standard_tools += self.memory.register(llm)
        logger.info(f"LLM tools: {[t.name for t in standard_tools]}")

        self._prompt_args = dict(
            assistant_name=cfg.assistant_name,
            user_name=cfg.user_name,
            engagement_window_secs=cfg.engagement_window_secs,
            delegate=self.tasks is not None,
        )
        base_prompt = system_prompt(**self._prompt_args, memory=self.memory.note if self.memory else None)
        self._context = context = LLMContext(
            messages=[{"role": "system", "content": base_prompt}], tools=ToolsSchema(standard_tools)
        )
        aggregators = LLMContextAggregatorPair(
            context,
            user_params=LLMUserAggregatorParams(user_turn_stop_timeout=cfg.user_turn_stop_timeout),
        )
        # Everything she says on her own (Hermes results, reminders, alarms, "One moment") goes through
        # one queue; the tracker tells it who is talking so nothing lands in the middle of a reply.
        activity = ActivityTracker()
        self.announcer = announcer = Announcer(activity, on_event=bus.emit)
        if self.tasks is not None:
            self.tasks.attach(announcer)
        if self.memory is not None:
            self.memory.attach(context, activity)

        # Voice tone: Wav2Small scores the user's speech; the hint goes into the system message.
        extra_processors_in: list = []
        extra_processors_llm: list = []
        if cfg.emotion_enabled:
            scorer = Wav2SmallScorer(cfg.emotion_model)
            self.emotion = emotion = EmotionProcessor(
                scorer,
                sample_rate=cfg.audio_in_sample_rate,
                window_secs=cfg.emotion_window_secs,
                every_secs=cfg.emotion_every_secs,
                smoothing=cfg.emotion_smoothing,
                on_scores=lambda s: bus.emit("voice", **s.as_dict()),
            )

            def on_hint(hint, scores):
                METRICS.voice_hint(hint, scores.as_dict() if scores else None)
                bus.emit("voice_hint", hint=hint)
                if hint:
                    logger.info(hint)

            self.hint = VoiceHintInjector(base_prompt, lambda: emotion.smoothed, cfg.emotion_thresholds, on_hint=on_hint)
            extra_processors_in, extra_processors_llm = [emotion], [self.hint]
            logger.info(f"voice tone hints on ({cfg.emotion_model.name}, thresholds {cfg.emotion_thresholds})")

        pipeline = Pipeline(
            [
                transport.input(),
                VADProcessor(vad_analyzer=vad),
                *extra_processors_in,
                stt,
                aggregators.user(),
                *extra_processors_llm,
                llm,
                activity,
                PassFilter(),
                tts,
                transport.output(),
                aggregators.assistant(),
            ]
        )

        latency = UserBotLatencyObserver()
        ui_observer = UIEventObserver(bus, on_tool_result=scheduler.on_tool_result, chat=self.chat)

        self.worker = worker = PipelineWorker(
            pipeline,
            params=PipelineParams(
                audio_in_sample_rate=cfg.audio_in_sample_rate,
                audio_out_sample_rate=cfg.audio_out_sample_rate,
                enable_metrics=True,
                enable_usage_metrics=True,
            ),
            observers=[latency, ui_observer, TurnMetricsObserver(), self.levels],
            idle_timeout_secs=None,  # always-on assistant; never cancel for silence
        )
        announcer.attach(worker, aggregators.assistant())
        scheduler.attach(worker, announcer)
        self.settings = SettingsOps(cfg, bus, vad, worker)
        scheduler.set_settings(self.settings)
        cfg.latency_log.parent.mkdir(parents=True, exist_ok=True)

        @latency.event_handler("on_latency_measured")
        async def on_latency(observer, seconds: float):
            ms = round(seconds * 1000, 1)
            bus.emit("latency", stage="voice_to_voice", ms=ms)
            logger.info(f"voice-to-voice latency: {ms} ms")

        @latency.event_handler("on_latency_breakdown")
        async def on_breakdown(observer, breakdown):
            for line in breakdown.turn_contribution_lines():
                logger.info(f"  {line}")
            record = {
                "t": time.time(),
                "total_ms": round(breakdown.total_secs * 1000, 1),
                "contributions": [
                    {"label": c.label, "owner": c.owner, "ms": round(c.duration_secs * 1000, 1)}
                    for c in breakdown.contributions
                ],
                "ttfb": [{"processor": t.processor, "ms": round(t.duration_secs * 1000, 1)} for t in breakdown.ttfb],
                "function_calls": [
                    {"name": f.function_name, "ms": round(f.duration_secs * 1000, 1)} for f in breakdown.function_calls
                ],
            }
            bus.emit("latency_breakdown", **record)
            try:
                with open(cfg.latency_log, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(record) + "\n")
            except OSError as e:
                logger.warning(f"could not write latency log: {e}")

        @aggregators.user().event_handler("on_user_turn_stop_timeout")
        async def on_turn_timeout(aggregator):
            logger.warning("user turn closed by timeout (no end-of-turn from ASR)")

        return worker

    def _set_memory_note(self, note: str) -> None:
        """A new memory note is ready: rebuild the system prompt in place (no restart, no pipeline change)."""
        from assistant.prompts import system_prompt

        prompt = system_prompt(**self._prompt_args, memory=note)
        if self.hint is not None:
            self.hint.set_base_prompt(prompt)  # the voice-hint injector rewrites the system message from this
        msgs = self._context.messages
        if msgs and msgs[0].get("role") == "system":
            msgs[0]["content"] = prompt
        logger.info(f"memory: prompt updated with the new note ({len(note)} chars)")

    async def run(self) -> None:
        from pipecat.pipeline.runner import WorkerRunner

        await self.scheduler.start()
        if self.memory:
            await self.memory.start()
        self.bus.emit("state", state="idle")
        if self.levels:
            self.levels.start()
        logger.info("assistant ready: say something")
        if self.notifier:
            self.notifier.enable()
        runner = WorkerRunner(handle_sigint=not sys.platform.startswith("win"))
        await runner.add_workers(self.worker)
        try:
            await runner.run()
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        if self.levels:
            await self.levels.stop()
        if self.net:
            await self.net.stop()
        if self.scheduler:
            await self.scheduler.stop()
        if self.announcer:
            await self.announcer.stop()
        from assistant.turn_metrics import METRICS

        METRICS.flush()
        if getattr(self.stt, "stats", None) is not None:
            logger.info(f"ASR stats: {self.stt.stats}")
        if getattr(self.tts, "stats", None) is not None:
            logger.info(f"TTS stats: {self.tts.stats}")
        if self.emotion:
            logger.info(f"voice tone stats: {self.emotion.stats}")
        if self.tasks:
            await self.tasks.close()
        if self.memory:
            await self.memory.stop()
        if self.hermes:
            await self.hermes.close()
        if self.tools_client:
            await self.tools_client.close()
        if self.tools_bridge:
            await self.tools_bridge.close()
        if self.notifier:
            self.notifier.remove()
        if self.bus:
            await self.bus.stop()
        if self.tools_proc is not None:
            self.tools_proc.terminate()


async def run(cfg: Config) -> None:
    app = Assistant(cfg)
    await app.build()
    await app.run()


async def check(cfg: Config) -> int:
    """Start each MCP server, call its health tool, print the result."""
    from assistant.mcp_bridge import MCPBridge, stdio_params

    ok = True
    for name, module, tool in (("asr", "mcp_servers.asr.server", "asr_health"), ("tts", "mcp_servers.tts.server", "tts_health")):
        bridge = MCPBridge(name, stdio=stdio_params(module))
        try:
            await bridge.start(timeout=20)
            print(f"{name}: tools={await bridge.list_tools()}")
            print(f"{name}: {await bridge.call_json(tool)}")
        except Exception as e:  # noqa: BLE001
            ok = False
            print(f"{name}: FAILED {e!r}")
        finally:
            await bridge.close()
    try:
        bridge, proc = await connect_tools_server(cfg)
        print(f"tools: tools={await bridge.list_tools()}")
        print(f"tools: {await bridge.call_json('get_current_time')}")
        await bridge.close()
        if proc:
            proc.terminate()
    except Exception as e:  # noqa: BLE001
        ok = False
        print(f"tools: FAILED {e!r}")
    if cfg.hermes_enabled:
        from assistant.services.hermes_client import HermesClient, HermesError

        client = HermesClient(cfg.hermes_url, cfg.hermes_api_key)
        try:
            print(f"hermes: {await client.check()}")
        except HermesError as e:
            ok = False
            print(f"hermes: FAILED {e}")
        finally:
            await client.close()
    else:
        print("hermes: disabled (HERMES_ENABLED=0)")
    if cfg.emotion_enabled:
        try:
            import numpy as np

            from assistant.emotion import Wav2SmallScorer

            scorer = Wav2SmallScorer(cfg.emotion_model)
            t0 = time.perf_counter()
            a, d, v = scorer(np.zeros(16000 * 3, dtype=np.float32))
            print(f"emotion: {cfg.emotion_model.name} ok, A={a:.2f} D={d:.2f} V={v:.2f} on silence, {(time.perf_counter() - t0) * 1000:.1f} ms for 3 s")
        except Exception as e:  # noqa: BLE001
            ok = False
            print(f"emotion: FAILED {e!r}")
    else:
        print("emotion: disabled (EMOTION_ENABLED=0)")
    return 0 if ok else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="ME3 voice assistant (Design A)")
    parser.add_argument("--list-devices", action="store_true", help="print audio devices and exit")
    parser.add_argument("--check", action="store_true", help="check keys and MCP servers, then exit")
    args = parser.parse_args()

    cfg = load()
    setup_logging(cfg.log_level)

    if args.list_devices:
        for d in list_audio_devices():
            print(f"[{d['index']:>2}] in={d['in']} out={d['out']} rate={d['rate']}  {d['name']}")
        return

    problems = cfg.validate()
    for p in problems:
        logger.error(p)
    if problems and not args.check:
        logger.error("fix .env (copy .env.example) and try again")
        sys.exit(2)

    if sys.platform.startswith("win"):
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

    if args.check:
        sys.exit(asyncio.run(check(cfg)))

    try:
        asyncio.run(run(cfg))
    except KeyboardInterrupt:
        logger.info("stopped")


if __name__ == "__main__":
    main()
