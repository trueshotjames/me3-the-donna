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
from assistant.engagement import addressed_elsewhere

LATENCY_SANE_SECS = 60.0  # v9: a voice-to-voice "latency" above this is a missed turn start, not a measurement

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


class NullToolsBridge:
    """BENCHMARK_MODE stands in for the tools server: never connected, so the scheduler polls nothing."""

    connected = False

    async def call_json(self, name: str, arguments: dict | None = None, timeout: float | None = None) -> dict:
        return {"error": f"{name} is not available in benchmark mode"}

    async def close(self) -> None:
        pass


# ------------------------------------------------------------------------------ main


def make_transport(cfg: Config):
    """Audio in/out through the USB speakerphone (hardware echo cancellation).

    No VAD here: Pipecat 1.13 ignores ``vad_analyzer`` on transport params. The VAD is a
    VADProcessor in the pipeline (see build()).
    """
    from pipecat.transports.local.audio import (
        LocalAudioOutputTransport,
        LocalAudioTransport,
        LocalAudioTransportParams,
    )

    buffer_ms = cfg.audio_out_buffer_ms

    class BufferedAudioOutput(LocalAudioOutputTransport):
        """Pipecat's PyAudio output, opened with a speaker buffer of ``buffer_ms``.

        Pipecat opens the stream without frames_per_buffer, which gave the Jabra on the Pi a
        ~30 ms ALSA buffer (period 287, buffer 1435 frames at 48 kHz): any stall longer than
        that underran (XRUN) and her voice broke up. PortAudio's ALSA buffer is two periods,
        so a period of buffer_ms / 2 gives a buffer of buffer_ms.
        """

        async def setup(self, setup):
            if buffer_ms <= 0:
                return await super().setup(setup)
            # Skip LocalAudioOutputTransport.setup (it opens the stream with the default buffer).
            await super(LocalAudioOutputTransport, self).setup(setup)
            self._out_stream = self._py_audio.open(
                format=self._py_audio.get_format_from_width(2),
                channels=self._params.audio_out_channels,
                rate=self.sample_rate,
                output=True,
                output_device_index=self._params.output_device_index,
                frames_per_buffer=max(64, int(self.sample_rate * buffer_ms / 2000)),
            )

    class BufferedAudioTransport(LocalAudioTransport):
        def output(self):
            if not self._output:
                self._output = BufferedAudioOutput(self._pyaudio, self._params)
            return self._output

    return BufferedAudioTransport(
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
        self.cues = self.cue_player = None
        self.exit_code = 0  # 3 = could not connect (the supervisor restarts her and plays the "down" cue)

    async def build(self, transport=None, stt=None):
        """Wire the pipeline. ``transport``/``stt`` can be replaced (benchmark/replay.py feeds scripted turns)."""
        cfg = self.cfg
        if cfg.benchmark_mode and transport is None:
            # make_transport would open the speakerphone
            raise RuntimeError("BENCHMARK_MODE needs a transport from the benchmark: it never opens the speakerphone")
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
        from assistant.prompts import system_prompt, turn_note
        from assistant.fast_confirm import recent_delete_note
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
        if cfg.cues_enabled:
            from assistant.cues import CueState, make_cue_player

            self.cue_player = make_cue_player(volume=cfg.cue_volume)
            self.cues = CueState(
                self.cue_player.play, DATA_DIR,
                recovered=os.getenv("DONNA_RECOVERED") == "1", repeat_secs=cfg.cue_down_repeat_secs,
            )
            self.net.listeners.append(self.cues.on_net)
        self.net.start()
        self.levels = LevelMeter(bus)

        if cfg.benchmark_mode:
            # The tools server owns Telegram, Spotify, the lights, the Jabra buttons and the database: never start it.
            self.tools_bridge, self.tools_proc = NullToolsBridge(), None
            logger.info("benchmark mode: no tools, no delegation, no memory, no cues, no audio devices")
        else:
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

        # v6: near-voice gate (VOICE_GATE) and per-feature switches; see config.py
        from assistant import fast_confirm as _fc, tasks as _tasks
        from assistant.mcp_bridge import server_params
        from assistant.voice_gate import VoiceGate

        _fc.THERMO_GUARD = cfg.v6_fixes
        _tasks.FIXES = cfg.v6_fixes
        self.voice_gate = None
        from assistant.calibrate import load_calibration
        from assistant.music import MUSIC

        calibration = load_calibration(cfg.calibration_file)
        MUSIC.enabled = cfg.music_duck
        if cfg.voice_gate or cfg.voice_gate_speaker == "1":
            self.voice_gate = VoiceGate(
                calibration=calibration,
                music_gap_db=cfg.music_gap_db,
                music_speaker_min=cfg.music_speaker_min,
                enabled=cfg.voice_gate,
                gap_db=cfg.voice_gate_db,
                seed_db=cfg.voice_gate_seed_db,
                speaker_mode=cfg.voice_gate_speaker,
                speaker_min=cfg.voice_gate_speaker_min,
                sample_rate=cfg.audio_in_sample_rate,
            )
        logger.info(
            f"v6 switches: LLM_EOT={cfg.llm_eot} LLM_EOT_PARTIAL={cfg.llm_eot_partial} SMART_TURN={cfg.smart_turn}"
            f" VOICE_GATE={cfg.voice_gate} (speaker {cfg.voice_gate_speaker}) FLOOR_CONTROL={cfg.floor_control}"
            f" MCP_TRANSPORT={cfg.mcp_transport} V6_FIXES={cfg.v6_fixes} SPOKEN_HISTORY={cfg.spoken_history}"
            f" DANGLING_HOLD_SECS={cfg.dangling_hold_secs} EOT_CHECK={cfg.eot_check}"
        )
        logger.info(
            f"v7 switches: DUCK_BARGE_IN={cfg.duck_barge_in} (conf {cfg.duck_conf}, {cfg.duck_frames} frames, decide"
            f" {cfg.duck_decide_secs} s, pace {cfg.duck_pace}) EOT_ADAPTIVE={cfg.eot_adaptive}"
            f" (statement hold {cfg.statement_hold_secs} s) LLM_HEDGE_SECS={cfg.llm_hedge_secs} META_FILTER={cfg.meta_filter}"
        )
        logger.info(
            f"v8 switches: VAD_HOOK_FILTER={cfg.vad_hook_filter} WEATHER_DIRECT={cfg.weather_direct}"
            f" SPEAKING_RATE={cfg.speaking_rate} TOOL_SLOW_SECS={cfg.tool_slow_secs} SIDE_TALK_RULE={cfg.side_talk_rule}"
            f" V8_FIXES={cfg.v8_fixes}"
        )
        logger.info(f"v10 switches: DONNA_PERSONA={cfg.donna_persona} TTS_SPEED={cfg.tts_speed} voice={cfg.tts_voice}")
        # v7 (DUCK_BARGE_IN): her audio pauses on the user's speech onset while the STT service decides
        self.duck = None
        if cfg.duck_barge_in:
            from assistant.duck import Duck

            self.duck = Duck(pace=cfg.duck_pace, max_secs=cfg.duck_max_secs)
            self.duck.install(transport.output())
        import assistant.processors as _proc

        _proc.META = cfg.meta_filter

        self.stt = stt = stt or MCPSTTService(
            server_params=server_params("mcp_servers.asr.server", cfg.mcp_transport),
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
            smart_turn=cfg.smart_turn,
            smart_turn_threshold=cfg.smart_turn_threshold,
            vad_stop_secs=cfg.vad_stop_secs,
            smart_turn_threads=cfg.smart_turn_threads,
            dangling_hold_secs=cfg.dangling_hold_secs,
            unsure_hold_p=cfg.unsure_hold_p,
            unsure_hold_secs=cfg.unsure_hold_secs,
            unsure_hold_min_words=cfg.unsure_hold_min_words,
            llm_eot=cfg.llm_eot,
            llm_eot_partial=cfg.llm_eot_partial,
            llm_wait_hold_secs=cfg.llm_wait_hold_secs,
            voice_gate=self.voice_gate,
            floor_control=cfg.floor_control,
            floor_max_defer_secs=cfg.floor_max_defer_secs,
            duck=self.duck,
            duck_conf=cfg.duck_conf,
            duck_frames=cfg.duck_frames,
            duck_min_volume=cfg.vad_min_volume,
            duck_decide_secs=cfg.duck_decide_secs,
            duck_no_turn_secs=cfg.duck_no_turn_secs,
            duck_max_secs=cfg.duck_max_secs,
            eot_adaptive=cfg.eot_adaptive,
            statement_hold_secs=cfg.statement_hold_secs,
        )
        if (self.duck is not None or cfg.music_duck) and hasattr(stt, "vad_frame"):
            vad.on_frame = stt.vad_frame  # v9: also while music plays, to duck it on a near voice
            stt.vad_hook_filter = cfg.vad_hook_filter

        self.tts = tts = MCPTTSService(
            server_params=server_params("mcp_servers.tts.server", cfg.mcp_transport),
            voice=cfg.tts_voice,
            server_sample_rate=cfg.tts_sample_rate,
            playout_enabled=cfg.tts_playout,
            playout_start_ms=cfg.tts_playout_start_ms,
            playout_max_ms=cfg.tts_playout_max_ms,
            playout_max_hold_ms=cfg.tts_playout_max_hold_ms,
            trim_lead_max_ms=cfg.tts_trim_lead_max_ms,
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
            # v2: time and voice hint ride on the latest user message, so the system prompt stays cacheable
            turn_note=lambda: turn_note(
                self.hint.last_hint if self.hint is not None else None,
                self.tasks.note() if self.tasks is not None else None,
                recent_delete_note(),
            ),
            first_token_timeout_secs=cfg.llm_first_token_timeout_secs,
            hedge_secs=cfg.llm_hedge_secs,
            hold_tool_calls=cfg.hold_tool_calls,  # v11
            # v5: "Kayla, you're talking to the radio." -> the turn note asks for <pass/> (contacts are not strangers)
            engagement=lambda text: addressed_elsewhere(
                text, cfg.assistant_name, ("Mom", "Dad", "Alex", "Sam", "Work", "Reyes", "Dr")
            ),
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
        if cfg.llm_eot and not cfg.smart_turn and hasattr(stt, "llm_verdict"):
            # v6: the first output of an early (speculative) reply decides the end of turn
            llm.on_verdict = stt.llm_verdict
            llm.wait_note = True
            if cfg.eot_check == "separate":
                # v6 Pi run: <wait/> inside the answer request suppressed tool calls; a parallel check decides instead
                from assistant.eot_check import CutOffCheck

                llm.wait_note = False
                llm.cut_check = CutOffCheck(
                    llm._client, model=cfg.eot_check_model, extra_body=cfg.llm_extra_body,
                    reasoning_effort=cfg.llm_reasoning_effort, timeout_secs=cfg.eot_check_timeout_secs,
                    adaptive=cfg.eot_adaptive,
                )

        standard_tools: list = []
        if not cfg.benchmark_mode:
            self.tools_client = MCPClient(StreamableHttpParameters(url=cfg.tools_mcp_url), tools_filter=LLM_TOOLS)
            tools = await self.tools_client.tools()
            standard_tools = list(tools.standard_tools)
            if cfg.fast_confirm:
                import assistant.fast_confirm as _fcm
                from assistant.fast_confirm import wrap_tools

                _fcm.WEATHER_DIRECT = cfg.weather_direct
                _fcm.TOOL_SLOW_SECS = cfg.tool_slow_secs
                _fcm.V8_FIXES = cfg.v8_fixes

                async def on_tool_slow(name: str) -> None:
                    bus.emit("tool_slow", tool=name, seconds=cfg.tool_slow_secs)
                    if self.scheduler is not None:
                        await self.scheduler.say_now("One moment.", source="tool_slow")

                _fcm.ON_SLOW = on_tool_slow
                # v1: action tools speak their confirmation directly (no second LLM call)
                standard_tools = wrap_tools(standard_tools, self.tools_client)

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
                    workdir=cfg.hermes_workdir or None,
                )
                standard_tools += self.tasks.register(llm)
            except HermesError as e:
                logger.warning(f"{e}; running without delegate (set HERMES_ENABLED=0 to silence this)")
                await self.hermes.close()
                self.hermes = None
        if self.memory is not None:
            standard_tools += self.memory.register(llm)
        if cfg.speaking_rate:
            from assistant.speaking_rate import SpeakingRate

            # v8 (SPEAKING_RATE): "speak slower" changes Aura's speed through the TTS MCP server
            self.rate = SpeakingRate(tts, base=cfg.tts_speed)
            standard_tools += self.rate.register(llm)
        logger.info(f"LLM tools: {[t.name for t in standard_tools]}")

        self._prompt_args = dict(
            assistant_name=cfg.assistant_name,
            user_name=cfg.user_name,
            engagement_window_secs=cfg.engagement_window_secs,
            delegate=self.tasks is not None,
            wait_rule=bool(getattr(llm, "on_verdict", None)),  # kept with EOT_CHECK=separate: same prompt as the Pi run
            fixes=cfg.v6_fixes,
            speaking_rate=cfg.speaking_rate,
            side_talk=cfg.side_talk_rule,
            no_tools=cfg.benchmark_mode,
            persona=cfg.donna_persona,
        )
        base_prompt = system_prompt(**self._prompt_args, memory=self.memory.note if self.memory else None)
        if cfg.benchmark_mode and standard_tools:
            raise RuntimeError(f"benchmark mode must offer no tools, got {[t.name for t in standard_tools]}")
        context_args = {"tools": ToolsSchema(standard_tools)} if standard_tools else {}
        self._context = context = LLMContext(messages=[{"role": "system", "content": base_prompt}], **context_args)
        if hasattr(llm, "tool_hold"):
            # v11 (HOLD_TOOL_CALLS): a held tool call runs against the conversation, for the same words only
            llm.real_context = context
            if hasattr(stt, "_last_commit"):
                llm.committed_text = lambda: stt._last_commit
        logger.info(f"v11 switch: HOLD_TOOL_CALLS={cfg.hold_tool_calls}")
        if hasattr(stt, "prev_reply"):
            # v7 (EOT_ADAPTIVE): an answer to her question ("Sam.", "Ten minutes.") is never held as a statement
            def prev_reply() -> str:
                for m in reversed(context.get_messages()):
                    if isinstance(m, dict) and m.get("role") == "assistant" and isinstance(m.get("content"), str):
                        return m["content"]
                return ""

            stt.prev_reply = prev_reply
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

        # v8 (PLAYED_TEXT): her sentences reach the chat and the spoken history only after they have played
        from assistant.processors import PlayedTextTap

        played_tap = PlayedTextTap() if cfg.played_text else None
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
                PassFilter(on_pass=self._on_pass),
                tts,
                *([self.cue_player] if self.cue_player else []),
                transport.output(),
                *([played_tap] if played_tap else []),
                aggregators.assistant(),
            ]
        )

        latency = UserBotLatencyObserver()
        ui_observer = UIEventObserver(bus, on_tool_result=scheduler.on_tool_result, chat=self.chat)
        from assistant.reprompt import Reprompter

        # v4: a question left unanswered is asked once more after REPROMPT_SECS of silence (0 = off)
        self.reprompter = Reprompter(
            lambda text: scheduler.say(text, source="reprompt", wait="turn", ttl=5), secs=cfg.reprompt_secs
        )

        # v6 (SPOKEN_HISTORY): her spoken replies back into the history when Pipecat dropped them (assistant/spoken.py)
        observers = [latency, ui_observer, TurnMetricsObserver(), self.levels, self.reprompter]
        self.spoken = None
        if cfg.spoken_history and hasattr(llm, "spoken"):
            from assistant.spoken import SpokenHistory

            self.spoken = llm.spoken = SpokenHistory(context)
            observers.append(self.spoken)
        if played_tap is not None:
            ui_observer.played_tap = True
            played_tap.listeners.append(ui_observer.played)
            if self.spoken is not None:
                self.spoken.played_tap = True
                played_tap.listeners.append(self.spoken.played)

        self.worker = worker = PipelineWorker(
            pipeline,
            params=PipelineParams(
                audio_in_sample_rate=cfg.audio_in_sample_rate,
                audio_out_sample_rate=cfg.audio_out_sample_rate,
                enable_metrics=True,
                enable_usage_metrics=True,
            ),
            observers=observers,
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
            if seconds > LATENCY_SANE_SECS:
                logger.warning(f"voice-to-voice latency {ms} ms not reported: the turn start was missed (a barge-in)")
                return
            bus.emit("latency", stage="voice_to_voice", ms=ms)
            logger.info(f"voice-to-voice latency: {ms} ms")

        @latency.event_handler("on_latency_breakdown")
        async def on_breakdown(observer, breakdown):
            if breakdown.total_secs > LATENCY_SANE_SECS:
                # v9: King's Spotify run showed "232465 ms": Pipecat counted from client connect because the turn began
                # as a barge-in and its start was missed. Not a real latency; leave it out of the UI and the log file.
                logger.warning(f"latency breakdown skipped: total {breakdown.total_secs:.1f} s (turn start missed)")
                return
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

        # Sound cues and supervised restarts (assistant/cues.py, assistant/supervisor.py)
        if isinstance(stt, MCPSTTService):
            if self.cues is not None:

                @stt.event_handler("on_connected")
                async def on_asr_connected(service):
                    await self.cues.on_live()

            @stt.event_handler("on_connection_error")
            async def on_asr_failed(service, error):
                if os.getenv("DONNA_SUPERVISED") == "1":
                    self._fatal(f"ASR could not connect: {error}")

        @aggregators.user().event_handler("on_user_turn_stop_timeout")
        async def on_turn_timeout(aggregator):
            logger.warning("user turn closed by timeout (no end-of-turn from ASR)")

        return worker

    def _fatal(self, msg: str) -> None:
        """Under the supervisor: stop with exit code 3 so it restarts her (and plays the "down" cue) instead of
        running on with no ears or voice."""
        logger.error(f"{msg}; exiting so the supervisor restarts her")
        self.exit_code = 3
        if self.worker is not None:
            asyncio.get_running_loop().create_task(self.worker.cancel(reason=msg))

    async def _on_pass(self) -> None:
        """v9: the LLM passed on a turn (TV, lyrics, side talk): the screen goes back to idle instead of "Thinking"."""
        if self.bus is not None:
            from assistant.ui_events import emit_state

            emit_state(self.bus, "idle")

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
        if self.cfg.music_duck and self.tools_bridge is not None:
            from assistant.music import MUSIC

            tools = self.tools_bridge

            async def duck(on: bool, level: int):
                res = await tools.call_json("duck_music", {"on": on, "level": level}, timeout=5)
                if isinstance(res, dict) and res.get("error"):
                    raise RuntimeError(res["error"])
                return res

            MUSIC.attach(duck, level=self.cfg.music_duck_level, restore_secs=self.cfg.music_restore_secs)
            MUSIC.start()
            logger.info(f"music-aware listening on: duck to {self.cfg.music_duck_level}%, restore after "
                        f"{self.cfg.music_restore_secs} s idle; gate {'calibrated' if self.voice_gate is not None and self.voice_gate.calibrated else 'NOT calibrated (python -m assistant.calibrate)'}")
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
        if getattr(self, "duck", None) is not None:
            logger.info(f"duck stats: {self.duck.stats}")
        if getattr(self.llm, "hedges", None) and self.llm.hedges.get("fired"):
            logger.info(f"LLM hedge stats: {self.llm.hedges}")
        if getattr(self.llm, "cut_check", None) is not None:
            logger.info(f"cut-off check stats: {self.llm.cut_check.stats}")
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


async def run(cfg: Config) -> int:
    app = Assistant(cfg)
    await app.build()
    await app.run()
    return app.exit_code


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

    code = 0
    try:
        code = asyncio.run(run(cfg))
    except KeyboardInterrupt:
        logger.info("stopped")
    sys.exit(code or 0)


if __name__ == "__main__":
    main()
