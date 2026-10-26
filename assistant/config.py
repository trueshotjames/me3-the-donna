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
    # OpenRouter host preference (ignored by other endpoints): "groq,cerebras" pins hosts in order;
    # sort = latency | throughput | price lets OpenRouter choose.
    llm_provider_order: str = field(default_factory=lambda: _get("LLM_PROVIDER_ORDER", "groq"))
    llm_provider_sort: str = field(default_factory=lambda: _get("LLM_PROVIDER_SORT", ""))
    llm_allow_fallbacks: bool = field(default_factory=lambda: _bool("LLM_ALLOW_FALLBACKS", True))
    # Hermes Agent (capable brain for delegated tasks), reached over its API server
    hermes_enabled: bool = field(default_factory=lambda: _bool("HERMES_ENABLED", True))
    hermes_url: str = field(default_factory=lambda: _get("HERMES_URL", "http://127.0.0.1:8642"))
    hermes_api_key: str = field(default_factory=lambda: _get("HERMES_API_KEY"))
    hermes_nudge_secs: float = field(default_factory=lambda: _float("HERMES_NUDGE_SECS", 30.0))
    hermes_max_secs: float = field(default_factory=lambda: _float("HERMES_MAX_SECS", 300.0))
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
    assistant_name: str = field(default_factory=lambda: _get("ASSISTANT_NAME", "Pi"))
    user_name: str = field(default_factory=lambda: _get("USER_NAME", "King"))
    # audio
    audio_in_sample_rate: int = field(default_factory=lambda: _int("AUDIO_IN_SAMPLE_RATE", 16000))
    audio_out_sample_rate: int = field(default_factory=lambda: _int("AUDIO_OUT_SAMPLE_RATE", 16000))
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
