"""Configuration for the assistant, read from .env (python-dotenv) and the environment."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
load_dotenv(ROOT / ".env")


def _get(name: str, default: str = "") -> str:
    value = os.getenv(name)
    return default if value in (None, "") else value


def _int(name: str, default: int) -> int:
    try:
        return int(_get(name, str(default)))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(_get(name, str(default)))
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    return _get(name, "1" if default else "0").lower() in ("1", "true", "yes", "on")


def _device(name: str) -> int | str | None:
    value = _get(name)
    if not value:
        return None
    return int(value) if value.lstrip("-").isdigit() else value


@dataclass
class Config:
    # keys
    deepgram_api_key: str = field(default_factory=lambda: _get("DEEPGRAM_API_KEY"))
    # Fast LLM: any OpenAI-compatible endpoint. Default is OpenRouter; LLM_* names win, the old
    # GROQ_* names still work for a direct Groq setup.
    llm_api_key: str = field(default_factory=lambda: _get("LLM_API_KEY") or _get("OPENROUTER_API_KEY") or _get("GROQ_API_KEY"))
    llm_model: str = field(default_factory=lambda: _get("LLM_MODEL") or _get("GROQ_MODEL", "meta-llama/llama-3.1-8b-instruct"))
    llm_base_url: str = field(
        default_factory=lambda: _get("LLM_BASE_URL") or _get("GROQ_BASE_URL", "https://openrouter.ai/api/v1")
    )
    llm_temperature: float = field(default_factory=lambda: _float("LLM_TEMPERATURE", 0.4))
    llm_max_tokens: int = field(default_factory=lambda: _int("LLM_MAX_TOKENS", 300))
    llm_reasoning_effort: str = field(default_factory=lambda: _get("LLM_REASONING_EFFORT", ""))
    # v3: no first token by then -> one retry with LLM_PROVIDER_ORDER reversed (0 = off)
    llm_first_token_timeout_secs: float = field(default_factory=lambda: _float("LLM_FIRST_TOKEN_TIMEOUT_SECS", 3.0))
    # v1: speak action-tool confirmations from a template instead of a second LLM call (assistant/fast_confirm.py)
    fast_confirm: bool = field(default_factory=lambda: _bool("FAST_CONFIRM", True))
    # OpenRouter host preference (ignored by other endpoints): "groq,cerebras" pins hosts in order;
    # sort = latency | throughput | price lets OpenRouter choose.
    llm_provider_order: str = field(default_factory=lambda: _get("LLM_PROVIDER_ORDER", "groq"))
    llm_provider_sort: str = field(default_factory=lambda: _get("LLM_PROVIDER_SORT", ""))
    llm_allow_fallbacks: bool = field(default_factory=lambda: _bool("LLM_ALLOW_FALLBACKS", True))
    # Hermes Agent (capable brain for delegated tasks), reached over its API server
    # BENCHMARK_MODE: no tools, no delegation, no memory, no cues, no audio devices (set by fd-benchmark's launcher)
    benchmark_mode: bool = field(default_factory=lambda: _bool("BENCHMARK_MODE", False))
    hermes_enabled: bool = field(default_factory=lambda: _bool("HERMES_ENABLED", True))
    hermes_url: str = field(default_factory=lambda: _get("HERMES_URL", "http://127.0.0.1:8642"))
    hermes_api_key: str = field(default_factory=lambda: _get("HERMES_API_KEY"))
    hermes_nudge_secs: float = field(default_factory=lambda: _float("HERMES_NUDGE_SECS", 30.0))
    hermes_max_secs: float = field(default_factory=lambda: _float("HERMES_MAX_SECS", 300.0))
    # v2: Hermes's working folder (its TERMINAL_CWD), where relative paths in its results live (site check)
    hermes_workdir: str = field(default_factory=lambda: _get("HERMES_WORKDIR"))
    # Voice tone hints (Wav2Small on the Pi)
    emotion_enabled: bool = field(default_factory=lambda: _bool("EMOTION_ENABLED", True))
    emotion_model: Path = field(default_factory=lambda: Path(_get("EMOTION_MODEL", str(ROOT / "models" / "wav2small.onnx"))))
    emotion_ad_low: float = field(default_factory=lambda: _float("EMOTION_AD_LOW", 0.35))
    emotion_ad_high: float = field(default_factory=lambda: _float("EMOTION_AD_HIGH", 0.65))
    emotion_v_low: float = field(default_factory=lambda: _float("EMOTION_V_LOW", 0.20))
    emotion_v_high: float = field(default_factory=lambda: _float("EMOTION_V_HIGH", 0.80))
    emotion_window_secs: float = field(default_factory=lambda: _float("EMOTION_WINDOW_SECS", 3.0))
    emotion_every_secs: float = field(default_factory=lambda: _float("EMOTION_EVERY_SECS", 1.0))
    emotion_smoothing: float = field(default_factory=lambda: _float("EMOTION_SMOOTHING", 0.5))
    # Messages of history re-sent on each call (system prompt not counted). Fewer = fewer tokens per
    # minute on Groq's free tier (8K TPM on most models), which otherwise answers 429 + a multi-second wait.
    llm_history_messages: int = field(default_factory=lambda: _int("LLM_HISTORY_MESSAGES", 12))
    # Long-term memory (assistant/memory.py): a note in the prompt, raw transcripts on disk, recall tool.
    memory_enabled: bool = field(default_factory=lambda: _bool("MEMORY_ENABLED", True))
    memory_dir: Path = field(default_factory=lambda: Path(_get("MEMORY_DIR", str(DATA_DIR / "memory"))))
    memory_model: str = field(default_factory=lambda: _get("MEMORY_MODEL"))  # empty = the fast model
    memory_idle_secs: float = field(default_factory=lambda: _float("MEMORY_IDLE_SECS", 1800.0))
    memory_episode_messages: int = field(default_factory=lambda: _int("MEMORY_EPISODE_MESSAGES", 40))
    memory_note_max_chars: int = field(default_factory=lambda: _int("MEMORY_NOTE_MAX_CHARS", 4000))
    assistant_name: str = field(default_factory=lambda: _get("ASSISTANT_NAME", "Donna"))
    user_name: str = field(default_factory=lambda: _get("USER_NAME", "King"))
    # audio
    audio_in_sample_rate: int = field(default_factory=lambda: _int("AUDIO_IN_SAMPLE_RATE", 16000))
    audio_out_sample_rate: int = field(default_factory=lambda: _int("AUDIO_OUT_SAMPLE_RATE", 16000))
    # Speaker buffer in ms (0 = PortAudio default, about 25-30 ms on a Pi: too small, any CPU stall
    # longer than that is an underrun and she sounds choppy). 100 ms rides out stalls on a busy Pi 4.
    audio_out_buffer_ms: int = field(default_factory=lambda: _int("AUDIO_OUT_BUFFER_MS", 100))
    # Sound cues (assistant/cues.py): beeps for live / stopped working / back, for running without a screen
    cues_enabled: bool = field(default_factory=lambda: _bool("CUES_ENABLED", True))
    cue_volume: float = field(default_factory=lambda: _float("CUE_VOLUME", 0.3))
    cue_down_repeat_secs: float = field(default_factory=lambda: _float("CUE_DOWN_REPEAT_SECS", 60.0))
    audio_input_device: int | str | None = field(default_factory=lambda: _device("AUDIO_INPUT_DEVICE"))
    audio_output_device: int | str | None = field(default_factory=lambda: _device("AUDIO_OUTPUT_DEVICE"))
    # VAD (Silero on the Pi). It only decides when audio goes to Flux (the gate), so it is
    # tuned for high recall. Loudness is Pipecat's 0..1 scale (-110..-10 dB); speech is ~0.7-0.9.
    vad_confidence: float = field(default_factory=lambda: _float("VAD_CONFIDENCE", 0.4))
    vad_start_secs: float = field(default_factory=lambda: _float("VAD_START_SECS", 0.1))
    vad_stop_secs: float = field(default_factory=lambda: _float("VAD_STOP_SECS", 0.4))
    vad_min_volume: float = field(default_factory=lambda: _float("VAD_MIN_VOLUME", 0.45))
    # audio gate in front of Flux (assistant/services/mcp_stt.py)
    gate_enabled: bool = field(default_factory=lambda: _bool("GATE_ENABLED", True))
    preroll_ms: int = field(default_factory=lambda: _int("PREROLL_MS", 500))
    bot_hangover_secs: float = field(default_factory=lambda: _float("BOT_HANGOVER_SECS", 1.5))
    gate_noise_tail_secs: float = field(default_factory=lambda: _float("GATE_NOISE_TAIL_SECS", 0.8))
    # turn taking
    turn_start: str = field(default_factory=lambda: _get("TURN_START_STRATEGY", "asr"))  # asr = Flux | vad
    eager_end_of_turn: bool = field(default_factory=lambda: _bool("EAGER_END_OF_TURN", True))
    flux_eot_threshold: float = field(default_factory=lambda: _float("FLUX_EOT_THRESHOLD", 0.7))
    flux_eager_eot_threshold: float = field(default_factory=lambda: _float("FLUX_EAGER_EOT_THRESHOLD", 0.5))
    flux_eot_timeout_ms: int = field(default_factory=lambda: _int("FLUX_EOT_TIMEOUT_MS", 5000))
    # v2: Smart Turn v3 (local ONNX model) ends the turn when Silero hears silence and the audio sounds finished
    smart_turn: bool = field(default_factory=lambda: _bool("SMART_TURN", True))
    smart_turn_threshold: float = field(default_factory=lambda: _float("SMART_TURN_THRESHOLD", 0.5))
    smart_turn_threads: int = field(default_factory=lambda: _int("SMART_TURN_THREADS", 1))  # v3: Pi 4 has 4 cores
    # v3: hold a turn that ends on "the", "to", "um" or a bare "Turn on" this long for the rest (0 = off)
    dangling_hold_secs: float = field(default_factory=lambda: _float("DANGLING_HOLD_SECS", 1.0))
    # v4: hold Flux's end of turn while Smart Turn says the sentence is clearly unfinished (0 = off)
    unsure_hold_p: float = field(default_factory=lambda: _float("UNSURE_HOLD_P", 0.0))
    unsure_hold_secs: float = field(default_factory=lambda: _float("UNSURE_HOLD_SECS", 1.2))
    unsure_hold_min_words: int = field(default_factory=lambda: _int("UNSURE_HOLD_MIN_WORDS", 4))
    # v6 (wild): every change below has its own switch so v7 can keep what works.
    # LLM_EOT: the fast LLM judges the end of turn instead of Smart Turn. A speculative call may answer <wait/>
    # (the sentence is cut off); any other first output ends the turn locally, before Flux's EndOfTurn.
    llm_eot: bool = field(default_factory=lambda: _bool("LLM_EOT", False))
    # LLM_EOT_PARTIAL: think while listening: start that speculative call on Flux's latest partial transcript as soon
    # as Silero hears VAD_STOP_SECS of silence, instead of waiting for Flux's EagerEndOfTurn.
    llm_eot_partial: bool = field(default_factory=lambda: _bool("LLM_EOT_PARTIAL", False))
    # after a <wait/>, Flux's end of turn for that same silence is held this long for the rest of the sentence
    llm_wait_hold_secs: float = field(default_factory=lambda: _float("LLM_WAIT_HOLD_SECS", 0.8))
    # VOICE_GATE: a turn whose speech is VOICE_GATE_DB quieter than the user's usual level (TV, radio, another room)
    # is passed without an LLM call and never interrupts her. The user's level is learned from answered turns.
    voice_gate: bool = field(default_factory=lambda: _bool("VOICE_GATE", False))
    voice_gate_db: float = field(default_factory=lambda: _float("VOICE_GATE_DB", 10.0))
    voice_gate_seed_db: float = field(default_factory=lambda: _float("VOICE_GATE_SEED_DB", -16.0))
    # VOICE_GATE_SPEAKER: also compare a tiny spectral voice print (numpy only); "shadow" logs it, "1" enforces it
    voice_gate_speaker: str = field(default_factory=lambda: _get("VOICE_GATE_SPEAKER", "shadow"))
    voice_gate_speaker_min: float = field(default_factory=lambda: _float("VOICE_GATE_SPEAKER_MIN", 0.80))
    # v9 music-aware listening (assistant/music.py): while music plays, a near voice (or her speaking) lowers it to
    # MUSIC_DUCK_LEVEL % until MUSIC_RESTORE_SECS after the conversation goes idle; the gate is stricter
    # (calibrated music leak from CALIBRATION_FILE, else MUSIC_GAP_DB under the user) and the voice print is enforced
    music_duck: bool = field(default_factory=lambda: _bool("MUSIC_DUCK", True))
    music_duck_level: int = field(default_factory=lambda: _int("MUSIC_DUCK_LEVEL", 15))
    music_restore_secs: float = field(default_factory=lambda: _float("MUSIC_RESTORE_SECS", 2.5))
    music_gap_db: float = field(default_factory=lambda: _float("MUSIC_GAP_DB", 5.0))
    music_speaker_min: float = field(default_factory=lambda: _float("MUSIC_SPEAKER_MIN", 0.70))
    # v9: written by `python -m assistant.calibrate` (your voice level and print, room noise, music leak)
    calibration_file: str = field(default_factory=lambda: _get("CALIBRATION_FILE", "calibration.json"))
    # FLOOR_CONTROL: while she speaks, Flux's StartOfTurn no longer interrupts at once; a backchannel ("okay",
    # "mm-hmm") or a far-away voice lets her keep talking. Real words or FLOOR_MAX_DEFER_SECS of speech interrupt.
    floor_control: bool = field(default_factory=lambda: _bool("FLOOR_CONTROL", False))
    floor_max_defer_secs: float = field(default_factory=lambda: _float("FLOOR_MAX_DEFER_SECS", 0.6))
    # MCP_TRANSPORT: stdio = ASR/TTS servers as child processes (v5); inproc = the same MCP servers in this process
    # over in-memory MCP streams (real MCP protocol, no extra Python processes).
    mcp_transport: str = field(default_factory=lambda: _get("MCP_TRANSPORT", "stdio").lower())
    # V6_FIXES: list-before-delete prompt rule, plain edit-task text for Hermes, "warmer" thermostat guard
    v6_fixes: bool = field(default_factory=lambda: _bool("V6_FIXES", True))
    # SPOKEN_HISTORY: what she said aloud goes into the history even when Pipecat dropped it (speculative replies,
    # replies the user cut off); see assistant/spoken.py
    spoken_history: bool = field(default_factory=lambda: _bool("SPOKEN_HISTORY", False))
    # v8 (PLAYED_TEXT): the chat and the spoken history take her sentences after they have played (not when
    # synthesised), so words she never got to say are not shown or remembered when she is cut off
    played_text: bool = field(default_factory=lambda: _bool("PLAYED_TEXT", True))
    # EOT_CHECK (with LLM_EOT): inline = <wait/> offered inside the answer request (the v6 Pi run); separate = a tiny
    # parallel "cut off?" request decides instead (assistant/eot_check.py), so the answer request stays as in v5b
    eot_check: str = field(default_factory=lambda: _get("EOT_CHECK", "inline").lower())
    eot_check_model: str = field(default_factory=lambda: _get("EOT_CHECK_MODEL", "openai/gpt-oss-120b"))
    eot_check_timeout_secs: float = field(default_factory=lambda: _float("EOT_CHECK_TIMEOUT_SECS", 1.5))
    # v7 (wild): every change below has its own switch, defaults = v6 behaviour, so v8 can cherry-pick.
    # DUCK_BARGE_IN: duck-and-decide barge-in. When Silero hears the user over her (DUCK_FRAMES voiced frames at
    # DUCK_CONF, near voice), her audio pauses at once; Flux's words then decide within DUCK_DECIDE_SECS: real words
    # interrupt, a backchannel ("okay", "mm-hmm") or a far voice resumes her where she paused (assistant/duck.py).
    duck_barge_in: bool = field(default_factory=lambda: _bool("DUCK_BARGE_IN", False))
    duck_conf: float = field(default_factory=lambda: _float("DUCK_CONF", 0.6))
    duck_frames: int = field(default_factory=lambda: _int("DUCK_FRAMES", 2))  # Silero frames are 32 ms
    duck_decide_secs: float = field(default_factory=lambda: _float("DUCK_DECIDE_SECS", 0.5))
    duck_no_turn_secs: float = field(default_factory=lambda: _float("DUCK_NO_TURN_SECS", 0.8))
    duck_max_secs: float = field(default_factory=lambda: _float("DUCK_MAX_SECS", 2.5))
    duck_pace: bool = field(default_factory=lambda: _bool("DUCK_PACE", True))  # output at sound-card pace
    # EOT_ADAPTIVE: turn-end tuning without a local model. Statements (not a request or question, not an answer to
    # her question) are held STATEMENT_HOLD_SECS after Flux's end of turn, with the early reply kept running, and the
    # cut-off check's "done" no longer ends them early; trailing words ("so", "because", "like") count as dangling;
    # a long sentence ending on a stranded preposition ("... talking about.") does not.
    eot_adaptive: bool = field(default_factory=lambda: _bool("EOT_ADAPTIVE", False))
    statement_hold_secs: float = field(default_factory=lambda: _float("STATEMENT_HOLD_SECS", 0.6))
    # LLM_HEDGE_SECS: no first token by then -> a second, identical request with the provider order reversed; the
    # first to produce a token wins, the other is closed (0 = v3's retry after LLM_FIRST_TOKEN_TIMEOUT_SECS)
    llm_hedge_secs: float = field(default_factory=lambda: _float("LLM_HEDGE_SECS", 0.0))
    # META_FILTER: gpt-oss narration leaking into the reply ("We are done. (no further)Done. Stop.") ends the reply
    meta_filter: bool = field(default_factory=lambda: _bool("META_FILTER", False))
    # v8 (consolidation): every new change has its own switch, defaults = v7 behaviour.
    # VAD_HOOK_FILTER: Silero's per-frame hook only wakes the event loop for a voiced frame (or to reset a run), not
    # for every 32 ms frame while she talks
    vad_hook_filter: bool = field(default_factory=lambda: _bool("VAD_HOOK_FILTER", False))
    # WEATHER_DIRECT: a plain weather question ("What's the weather in Berlin?") is answered with the weather tool's
    # own spoken sentence, without the second LLM call (the tool turns were the reply-latency tail in v7)
    weather_direct: bool = field(default_factory=lambda: _bool("WEATHER_DIRECT", False))
    # SPEAKING_RATE: "speak slower" / "faster" really changes Aura's speed (set_speaking_rate tool -> TTS MCP server)
    speaking_rate: bool = field(default_factory=lambda: _bool("SPEAKING_RATE", False))
    # TOOL_SLOW_SECS: a tool that has not answered after this long gets a spoken "One moment." (0 = off)
    tool_slow_secs: float = field(default_factory=lambda: _float("TOOL_SLOW_SECS", 0.0))
    # SIDE_TALK_RULE: prompt rule: when part of a turn is said to someone else, answer only the part meant for her
    side_talk_rule: bool = field(default_factory=lambda: _bool("SIDE_TALK_RULE", False))
    # V8_FIXES: bugs found in the v8 run (the same music "play" twice in one turn is not run or spoken again)
    v8_fixes: bool = field(default_factory=lambda: _bool("V8_FIXES", False))
    reprompt_secs: float = field(default_factory=lambda: _float("REPROMPT_SECS", 8.0))  # v4: 0 = never re-prompt
    user_turn_stop_timeout: float = field(default_factory=lambda: _float("USER_TURN_STOP_TIMEOUT", 5.0))
    engagement_window_secs: float = field(default_factory=lambda: _float("ENGAGEMENT_WINDOW_SECS", 8.0))
    # streaming over MCP
    asr_push_ms: int = field(default_factory=lambda: _int("ASR_PUSH_MS", 60))
    tts_voice: str = field(default_factory=lambda: _get("DEEPGRAM_AURA_VOICE", "aura-2-thalia-en"))
    tts_sample_rate: int = field(default_factory=lambda: _int("TTS_SAMPLE_RATE", 16000))
    # adaptive playout buffer against stutter on slow links (0 ms added while audio arrives fast)
    tts_playout: bool = field(default_factory=lambda: _bool("TTS_PLAYOUT_BUFFER", True))
    tts_playout_start_ms: float = field(default_factory=lambda: _float("TTS_PLAYOUT_START_MS", 0.0))
    tts_playout_max_ms: float = field(default_factory=lambda: _float("TTS_PLAYOUT_MAX_MS", 1500.0))
    tts_playout_max_hold_ms: float = field(default_factory=lambda: _float("TTS_PLAYOUT_MAX_HOLD_MS", 1000.0))
    # v3: drop up to this much silence at the start of each sentence (0 = off)
    tts_trim_lead_max_ms: float = field(default_factory=lambda: _float("TTS_TRIM_LEAD_MAX_MS", 400.0))
    # servers
    tools_mcp_url: str = field(
        default_factory=lambda: _get(
            "TOOLS_MCP_URL", f"http://{_get('TOOLS_MCP_HOST', '127.0.0.1')}:{_get('TOOLS_MCP_PORT', '8766')}/mcp"
        )
    )
    tools_mcp_autostart: bool = field(default_factory=lambda: _bool("TOOLS_MCP_AUTOSTART", True))
    ui_ws_host: str = field(default_factory=lambda: _get("UI_WS_HOST", "127.0.0.1"))
    ui_ws_port: int = field(default_factory=lambda: _int("UI_WS_PORT", 8765))
    scheduler_poll_secs: float = field(default_factory=lambda: _float("SCHEDULER_POLL_SECS", 1.0))
    alarm_repeat_announce_secs: float = field(default_factory=lambda: _float("ALARM_REANNOUNCE_SECS", 45.0))
    log_level: str = field(default_factory=lambda: _get("LOG_LEVEL", "INFO"))
    latency_log: Path = field(default_factory=lambda: DATA_DIR / "latency.jsonl")
    turns_log: Path = field(default_factory=lambda: DATA_DIR / "turns.jsonl")

    @property
    def python(self) -> str:
        return sys.executable

    def __post_init__(self) -> None:
        if self.benchmark_mode:
            # Everything that can act on the world or touch her real data is off, whatever .env says.
            self.hermes_enabled = False
            self.memory_enabled = False
            self.cues_enabled = False
            self.music_duck = False
            self.speaking_rate = False
        # gpt-oss models must reason; OpenRouter answers 400 "Reasoning is mandatory" to "none".
        # An old Qwen-era LLM_REASONING_EFFORT=none in .env would otherwise break every turn.
        if "gpt-oss" in self.llm_model and self.llm_reasoning_effort.strip().lower() in ("", "none", "off", "0"):
            import logging

            logging.getLogger(__name__).warning("gpt-oss needs a reasoning effort (none/empty is rejected); using low")
            self.llm_reasoning_effort = "low"

    def validate(self) -> list[str]:
        problems = []
        if not self.deepgram_api_key:
            problems.append("DEEPGRAM_API_KEY is missing (ASR and TTS servers need it)")
        if not self.llm_api_key:
            problems.append("LLM_API_KEY is missing (fast model; an OpenRouter key, or GROQ_API_KEY for Groq direct)")
        if self.turn_start not in ("vad", "asr"):
            problems.append("TURN_START_STRATEGY must be 'vad' or 'asr'")
        if self.hermes_enabled and not self.hermes_api_key:
            problems.append("HERMES_API_KEY is missing (same value as API_SERVER_KEY in Hermes's .env), or set HERMES_ENABLED=0")
        if self.emotion_enabled and not self.emotion_model.exists():
            problems.append(f"EMOTION_MODEL not found at {self.emotion_model} (run tools/export_wav2small.py), or set EMOTION_ENABLED=0")
        return problems

    @property
    def llm_extra_body(self) -> dict:
        """OpenRouter provider routing; empty for any other endpoint."""
        if "openrouter" not in self.llm_base_url:
            return {}
        provider: dict = {"allow_fallbacks": self.llm_allow_fallbacks}
        if self.llm_provider_sort:
            provider["sort"] = self.llm_provider_sort
        order = [p.strip() for p in self.llm_provider_order.split(",") if p.strip()]
        if order:
            provider["order"] = order
        return {"provider": provider}

    @property
    def emotion_thresholds(self):
        from assistant.emotion import Thresholds

        return Thresholds(self.emotion_ad_low, self.emotion_ad_high, self.emotion_v_low, self.emotion_v_high)


def load() -> Config:
    DATA_DIR.mkdir(exist_ok=True)
    return Config()
