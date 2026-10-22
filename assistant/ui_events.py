"""Observer that turns pipeline frames into UI events on the WebSocket bus."""

from __future__ import annotations

import json
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


def _parse(result: Any) -> Any:
    if isinstance(result, str):
        try:
            return json.loads(result)
        except json.JSONDecodeError:
            return result
    return result
