"""ME3 voice assistant, Design A: Pipecat on the Pi, speech and brain in the cloud.

    python -m assistant.main                # run
    python -m assistant.main --list-devices # print audio devices and exit
    python -m assistant.main --check        # check keys and MCP servers, then exit

Pipeline (all on the Pi):
    mic -> Silero VAD (gate only) -> MCPSTTService -> [ASR MCP server -> Deepgram Flux]
        -> user context aggregator -> Groq LLM (+ tools MCP server)
        -> PassFilter -> MCPTTSService -> [TTS MCP server -> Deepgram Aura-2] -> speaker

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
        self.settings = None
        self.notifier = None

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
        from pipecat.audio.vad.vad_analyzer import VADParams
        from pipecat.processors.audio.vad_processor import VADProcessor
        from pipecat.services.mcp_service import MCPClient

        from assistant.mcp_bridge import stdio_params
        from assistant.processors import PassFilter
        from assistant.prompts import system_prompt
        from assistant.scheduler import Scheduler
        from assistant.settings_ops import SettingsOps
        from assistant.services.mcp_stt import MCPSTTService
        from assistant.services.mcp_tts import MCPTTSService
        from assistant.services.timed_llm import TimedGroqLLMService as GroqLLMService
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
        )

        async def on_rate_limited(retry_after: float, info: dict) -> None:
            # Groq answered 429; the SDK waits retry-after and retries. Say so instead of going silent.
            bus.emit("llm_wait", seconds=retry_after, remaining_tokens=info.get("remaining_tokens"), limit_tokens=info.get("limit_tokens"))
            if retry_after >= 2.0 and self.scheduler is not None:
                await self.scheduler.say("One moment.", interruptible=True, append_to_context=False)

        self.llm = llm = GroqLLMService(
            api_key=cfg.groq_api_key,
            base_url=cfg.llm_base_url,
            history_messages=cfg.llm_history_messages,
            on_rate_limited=on_rate_limited,
            settings=GroqLLMService.Settings(
                model=cfg.llm_model,
                temperature=cfg.llm_temperature,
                max_tokens=cfg.llm_max_tokens,
                # "none" turns thinking off on Qwen; gpt-oss accepts low/medium/high; leave
                # LLM_REASONING_EFFORT empty for models that reject the parameter.
                **({"reasoning_effort": cfg.llm_reasoning_effort} if cfg.llm_reasoning_effort else {}),
            ),
        )

        self.tools_client = MCPClient(StreamableHttpParameters(url=cfg.tools_mcp_url), tools_filter=LLM_TOOLS)
        tools = await self.tools_client.tools()
        logger.info(f"LLM tools: {[t.name for t in tools.standard_tools]}")

        context = LLMContext(
            messages=[
                {
                    "role": "system",
                    "content": system_prompt(cfg.assistant_name, cfg.user_name, cfg.engagement_window_secs),
                }
            ],
            tools=tools,
        )
        aggregators = LLMContextAggregatorPair(
            context,
            user_params=LLMUserAggregatorParams(user_turn_stop_timeout=cfg.user_turn_stop_timeout),
        )

        pipeline = Pipeline(
            [
                transport.input(),
                VADProcessor(vad_analyzer=vad),
                stt,
                aggregators.user(),
                llm,
                PassFilter(),
                tts,
                transport.output(),
                aggregators.assistant(),
            ]
        )

        latency = UserBotLatencyObserver()
        ui_observer = UIEventObserver(bus, on_tool_result=scheduler.on_tool_result)

        self.worker = worker = PipelineWorker(
            pipeline,
            params=PipelineParams(
                audio_in_sample_rate=cfg.audio_in_sample_rate,
                audio_out_sample_rate=cfg.audio_out_sample_rate,
                enable_metrics=True,
                enable_usage_metrics=True,
            ),
            observers=[latency, ui_observer, TurnMetricsObserver()],
            idle_timeout_secs=None,  # always-on assistant; never cancel for silence
        )
        scheduler.attach(worker)
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

    async def run(self) -> None:
        from pipecat.pipeline.runner import WorkerRunner

        await self.scheduler.start()
        self.bus.emit("state", state="idle")
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
        if self.scheduler:
            await self.scheduler.stop()
        from assistant.turn_metrics import METRICS

        METRICS.flush()
        if getattr(self.stt, "stats", None) is not None:
            logger.info(f"ASR stats: {self.stt.stats}")
        if getattr(self.tts, "stats", None) is not None:
            logger.info(f"TTS stats: {self.tts.stats}")
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
