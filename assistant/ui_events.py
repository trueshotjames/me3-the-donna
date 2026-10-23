"""Observer that turns pipeline frames into UI events on the WebSocket bus."""

from __future__ import annotations

import json
import re
import time
from typing import Any

from loguru import logger
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    ErrorFrame,
    FunctionCallInProgressFrame,
    FunctionCallResultFrame,
    InterimTranscriptionFrame,
    InterruptionFrame,
    LLMFullResponseStartFrame,
    MetricsFrame,
    TranscriptionFrame,
    TTSTextFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.metrics.metrics import ProcessingMetricsData, TTFBMetricsData
from pipecat.observers.base_observer import BaseObserver, FramePushed

from assistant.ui_bus import UIBus


class UIEventObserver(BaseObserver):
    """Maps frames to the events documented in assistant/EVENTS.md."""

    def __init__(self, bus: UIBus, *, on_tool_result=None, **kwargs):
        super().__init__(observe_every_push=False, **kwargs)
        self._bus = bus
        self._on_tool_result = on_tool_result  # async callback(name, args, result_dict)
        self._bot_speaking = False
        self._user_speaking = False
        self._pending_args: dict[str, Any] = {}
        self._seen_calls: set[str] = set()
        self._seen_results: set[str] = set()
        self._last_interrupt = 0.0
        self._tools_this_reply = 0  # tool calls since the user's turn ended
        self._claim_warned = False

    async def on_push_frame(self, data: FramePushed):
        frame = data.frame
        bus = self._bus
        if isinstance(frame, UserStartedSpeakingFrame):
            if not self._user_speaking:  # broadcast frames arrive twice
                self._user_speaking = True
                logger.info("user turn started")
                bus.emit("state", state="listening")
        elif isinstance(frame, UserStoppedSpeakingFrame):
            if self._user_speaking:
                self._user_speaking = False
                logger.info("user turn stopped; running LLM")
                self._tools_this_reply = 0
                self._claim_warned = False
                bus.emit("state", state="thinking")
        elif isinstance(frame, LLMFullResponseStartFrame):
            if not self._bot_speaking:
                bus.emit("state", state="thinking")
        elif isinstance(frame, BotStartedSpeakingFrame):
            self._bot_speaking = True
            bus.emit("state", state="speaking")
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._bot_speaking = False
            bus.emit("state", state="idle")
        elif isinstance(frame, InterimTranscriptionFrame):
            bus.emit("transcript", text=frame.text, final=False)
        elif isinstance(frame, TranscriptionFrame):
            bus.emit("transcript", text=frame.text, final=True)
        elif isinstance(frame, TTSTextFrame):
            if frame.text.strip():
                logger.info(f"bot says: {frame.text}")
                bus.emit("assistant_text", text=frame.text, final=True)
                self._check_claim(frame.text)
        elif isinstance(frame, InterruptionFrame):
            # Broadcast frames reach the observer twice (upstream and downstream copies).
            if self._bot_speaking and time.monotonic() - self._last_interrupt > 0.2:
                self._last_interrupt = time.monotonic()
                bus.emit("interrupted")
        elif isinstance(frame, FunctionCallInProgressFrame):
            if frame.tool_call_id in self._seen_calls:
                return
            self._seen_calls.add(frame.tool_call_id)
            logger.info(f"tool call: {frame.function_name}({frame.arguments})")
            self._pending_args[frame.tool_call_id] = frame.arguments
            bus.emit("tool_call", name=frame.function_name, args=frame.arguments, id=frame.tool_call_id)
        elif isinstance(frame, FunctionCallResultFrame):
            if frame.tool_call_id in self._seen_results:
                return
            self._seen_results.add(frame.tool_call_id)
            self._seen_calls.discard(frame.tool_call_id)
            result = _parse(frame.result)
            args = self._pending_args.pop(frame.tool_call_id, frame.arguments)
            self._tools_this_reply += 1
            logger.info(f"tool result: {frame.function_name} -> {str(result)[:160]}")
            bus.emit("tool_result", name=frame.function_name, result=result, id=frame.tool_call_id)
            if self._on_tool_result:
                try:
                    await self._on_tool_result(frame.function_name, args, result)
                except Exception as e:  # noqa: BLE001
                    logger.error(f"tool result hook failed: {e!r}")
        elif isinstance(frame, MetricsFrame):
            for m in frame.data:
                if isinstance(m, TTFBMetricsData):
                    bus.emit("latency", stage=f"{m.processor}:ttfb", ms=round(m.value * 1000, 1))
                elif isinstance(m, ProcessingMetricsData):
                    bus.emit("latency", stage=f"{m.processor}:processing", ms=round(m.value * 1000, 1))
        elif isinstance(frame, ErrorFrame):
            logger.error(f"pipeline error: {frame.error}")
            bus.emit("error", message=str(frame.error), fatal=bool(getattr(frame, "fatal", False)))


    # The LLM sometimes answers "Okay, alarm set" without calling create_alarm (seen with Qwen on
    # 2026-10-23: two of three alarm requests). Nothing was saved, so tell the user instead of
    # letting the spoken confirmation stand.
    _CLAIM = re.compile(r"\b(alarm|reminder)\b.{0,40}\b(set|created|added|saved|done|scheduled)\b", re.I)
    _CLAIM_REV = re.compile(r"\b(set|created|added|saved|scheduled)\b.{0,30}\b(alarm|reminder)\b", re.I)

    # Or the opposite: "I'm sorry, I can't set an alarm for you." (2026-10-23, after "Sat on alarm").
    _REFUSAL = re.compile(r"\b(can't|cannot|can not|unable to|not able to)\b.{0,20}\b(set|create|add|make)\b.{0,30}\b(alarm|reminder)s?\b", re.I)

    def _check_claim(self, text: str) -> None:
        if self._tools_this_reply or self._claim_warned:
            return
        if self._REFUSAL.search(text):
            self._claim_warned = True
            logger.warning(f"LLM claimed a tool action is impossible: [{text}]")
            self._bus.emit(
                "warning",
                message="She said she can't do that, but alarms and reminders do work. Ask again, e.g. \"set an alarm for 6 AM\".",
                kind="refused_tool",
            )
            return
        if self._CLAIM.search(text) or self._CLAIM_REV.search(text):
            self._claim_warned = True
            logger.warning(f"LLM claimed a tool action without calling a tool: [{text}]")
            self._bus.emit(
                "warning",
                message="She said it is set, but no tool was called: nothing was saved. Ask again.",
                kind="no_tool_call",
            )


def _parse(result: Any) -> Any:
    if isinstance(result, str):
        try:
            return json.loads(result)
        except json.JSONDecodeError:
            return result
    return result
