"""Shared helpers for the three MCP servers (ASR, TTS, tools).

Every server runs on the Pi (or the dev laptop) and is written so that
nothing is ever printed to stdout: stdout is the MCP stdio transport.
All logging goes to stderr through loguru.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from loguru import logger

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"


def setup(server_name: str) -> None:
    """Load .env from the project root and route logs to stderr."""
    load_dotenv(ROOT / ".env")
    DATA_DIR.mkdir(exist_ok=True)
    logger.remove()
    logger.add(
        sys.stderr,
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="<green>{time:HH:mm:ss.SSS}</green> | <level>{level: <7}</level> | "
        + server_name
        + " | {message}",
    )


def env(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    return value if value not in (None, "") else default


def env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return default


def env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default
