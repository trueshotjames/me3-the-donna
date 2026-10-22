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
    groq_api_key: str = field(default_factory=lambda: _get("GROQ_API_KEY"))
    # LLM
    llm_model: str = field(default_factory=lambda: _get("GROQ_MODEL", "openai/gpt-oss-20b"))
    llm_base_url: str = field(default_factory=lambda: _get("GROQ_BASE_URL", "https://api.groq.com/openai/v1"))
    llm_temperature: float = field(default_factory=lambda: _float("LLM_TEMPERATURE", 0.4))
    llm_max_tokens: int = field(default_factory=lambda: _int("LLM_MAX_TOKENS", 300))
    assistant_name: str = field(default_factory=lambda: _get("ASSISTANT_NAME", "Pi"))
    user_name: str = field(default_factory=lambda: _get("USER_NAME", "King"))
    # audio
    audio_in_sample_rate: int = field(default_factory=lambda: _int("AUDIO_IN_SAMPLE_RATE", 16000))
    audio_out_sample_rate: int = field(default_factory=lambda: _int("AUDIO_OUT_SAMPLE_RATE", 16000))
    audio_input_device: int | str | None = field(default_factory=lambda: _device("AUDIO_INPUT_DEVICE"))
    audio_output_device: int | str | None = field(default_factory=lambda: _device("AUDIO_OUTPUT_DEVICE"))
    # VAD (Silero on the Pi)
    vad_confidence: float = field(default_factory=lambda: _float("VAD_CONFIDENCE", 0.7))
    vad_start_secs: float = field(default_factory=lambda: _float("VAD_START_SECS", 0.2))
    vad_stop_secs: float = field(default_factory=lambda: _float("VAD_STOP_SECS", 0.2))
    vad_min_volume: float = field(default_factory=lambda: _float("VAD_MIN_VOLUME", 0.6))
    # turn taking
    turn_start: str = field(default_factory=lambda: _get("TURN_START_STRATEGY", "vad"))  # vad | asr
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

    @property
    def python(self) -> str:
        return sys.executable

    def validate(self) -> list[str]:
        problems = []
        if not self.deepgram_api_key:
            problems.append("DEEPGRAM_API_KEY is missing (ASR and TTS servers need it)")
        if not self.groq_api_key:
            problems.append("GROQ_API_KEY is missing (LLM)")
        if self.turn_start not in ("vad", "asr"):
            problems.append("TURN_START_STRATEGY must be 'vad' or 'asr'")
        return problems


def load() -> Config:
    DATA_DIR.mkdir(exist_ok=True)
    return Config()
