"""v8 (SPEAKING_RATE): "Speak slower" / "talk faster" really changes how fast she speaks.

Up to v7 she could only say "I can't change my speaking speed" (scenario 16). Deepgram Aura-2 takes a speed
parameter (0.7-1.5) on the websocket, so the TTS MCP server got a tts_set_speed tool, and the LLM gets
set_speaking_rate. The confirmation is spoken from a template, without a second LLM call, like fast_confirm.

No CPU cost on the Pi: the speed is applied by Aura in the cloud; the TTS server reopens its socket once.
"""

from __future__ import annotations

import json

from loguru import logger
from pipecat.adapters.schemas.function_schema import FunctionSchema

STEP = 0.15
SLOWEST = 0.7
FASTEST = 1.3

SCHEMA = FunctionSchema(
    name="set_speaking_rate",
    description=(
        "Change how fast you speak, when the user asks you to speak slower, faster, or at normal speed again."
    ),
    properties={"rate": {"type": "string", "enum": ["slower", "faster", "normal"]}},
    required=["rate"],
)


def next_speed(current: float, rate: str, base: float = 1.0) -> float:
    """v10: "normal" returns to ``base`` (TTS_SPEED), not always 1.0."""
    rate = (rate or "").strip().lower()
    if rate == "slower":
        return round(max(SLOWEST, current - STEP), 2)
    if rate == "faster":
        return round(min(FASTEST, current + STEP), 2)
    return round(float(base), 2)


def sentence(old: float, new: float, rate: str) -> str:
    rate = (rate or "").strip().lower()
    if rate == "normal":
        return "Okay, back to my normal speed." if old != new else "This is my normal speed."
    if new == old:
        return "This is as slow as I can go." if rate == "slower" else "This is as fast as I can go."
    return "Okay, I'll speak more slowly." if rate == "slower" else "Okay, I'll speak faster."


class SpeakingRate:
    def __init__(self, tts, base: float = 1.0) -> None:
        self._tts = tts  # MCPTTSService: its MCP bridge to the TTS server
        self.base = round(min(1.5, max(0.7, float(base))), 2)  # v10: TTS_SPEED, the speed the server starts at
        self.speed = self.base

    def register(self, llm) -> list[FunctionSchema]:
        llm.register_function("set_speaking_rate", self._tool)
        return [SCHEMA]

    async def _tool(self, params) -> None:
        from pipecat.frames.frames import (
            FunctionCallResultProperties,
            LLMFullResponseEndFrame,
            LLMFullResponseStartFrame,
            LLMTextFrame,
        )

        rate = str((params.arguments or {}).get("rate") or "normal")
        old = self.speed
        new = next_speed(old, rate, self.base)
        try:
            res = await self._tts._bridge.call_json("tts_set_speed", {"speed": new}, timeout=10)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"set_speaking_rate({rate}) failed: {e!r}")
            await params.result_callback(json.dumps({"error": "could not change the speaking rate"}))
            return
        self.speed = float((res or {}).get("speed", new)) if isinstance(res, dict) else new
        say = sentence(old, self.speed, rate)
        logger.info(f"speaking rate {old:g} -> {self.speed:g} ({rate}): {say!r}")
        await params.result_callback(
            json.dumps({"speed": self.speed}), properties=FunctionCallResultProperties(run_llm=False)
        )
        await params.llm.push_frame(LLMFullResponseStartFrame())
        await params.llm.push_frame(LLMTextFrame(say))
        await params.llm.push_frame(LLMFullResponseEndFrame())
