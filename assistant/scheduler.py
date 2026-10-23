"""Bot-initiated speech: alarms and timed reminders.

Polls the tools MCP server's `poll_due` once a second. A due reminder is spoken
once; a due alarm rings (UI sound) and is announced by voice, un-interruptibly,
until the user or the UI stops it. Also serves the UI's commands and keeps the
UI's reminder/alarm snapshots fresh.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import Any

from loguru import logger
from pipecat.frames.frames import InterruptionFrame, STTMuteFrame, TTSSpeakFrame
from pipecat.pipeline.worker import PipelineWorker

from assistant.mcp_bridge import MCPBridge
from assistant.ui_bus import UIBus


def spoken_time(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    return dt.strftime("%I:%M %p").lstrip("0").replace(" 0", " ")


class Scheduler:
    def __init__(
        self,
        tools: MCPBridge,
        bus: UIBus,
        *,
        poll_secs: float = 1.0,
        reannounce_secs: float = 45.0,
    ):
        self._tools = tools
        self._bus = bus
        self._task: PipelineWorker | None = None
        self._poll_secs = poll_secs
        self._reannounce_secs = reannounce_secs
        self._runner: asyncio.Task | None = None
        self._last_announce: dict[int, float] = {}
        self._muted = False
        self._settings = None  # SettingsOps: summarize / VAD calibration / config (assistant/settings_ops.py)

    def set_settings(self, settings) -> None:
        self._settings = settings

    def attach(self, task: PipelineWorker) -> None:
        self._task = task

    async def start(self) -> None:
        self._bus.on_command(self.handle_command)
        self._bus.snapshot_provider(self.snapshot_messages)
        self._runner = asyncio.create_task(self._loop(), name="scheduler")

    async def stop(self) -> None:
        if self._runner:
            self._runner.cancel()
            self._runner = None

    # ------------------------------------------------------------------ polling

    async def _loop(self) -> None:
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.warning(f"scheduler tick failed: {e!r}")
            await asyncio.sleep(self._poll_secs)

    async def _tick(self) -> None:
        if not self._tools.connected:
            return
        due = await self._tools.call_json("poll_due", timeout=5)
        changed = False
        for rem in due.get("reminders", []):
            changed = True
            self._bus.emit("reminder_due", id=rem["id"], text=rem["text"], due_at=rem.get("due_at"))
            await self.say(f"Reminder: {rem['text']}.", interruptible=True)
        for alarm in due.get("alarms", []):
            changed = True
            self._bus.emit("alarm_fired", id=alarm["id"], label=alarm.get("label", ""), time=alarm["time"])
            await self._announce_alarm(alarm)
        # Re-announce alarms that keep ringing (the UI plays the sound continuously).
        for alarm in due.get("ringing", []):
            last = self._last_announce.get(alarm["id"], 0.0)
            if time.time() - last >= self._reannounce_secs:
                await self._announce_alarm(alarm)
        if changed:
            await self.push_snapshots()

    async def _announce_alarm(self, alarm: dict[str, Any]) -> None:
        self._last_announce[alarm["id"]] = time.time()
        label = alarm.get("label") or ""
        text = f"Alarm. {label}. It's {spoken_time(alarm.get('fired_at'))}." if label else (
            f"Alarm. It's {spoken_time(alarm.get('fired_at'))}. Say stop to turn it off."
        )
        await self.say(text, interruptible=False)

    # ------------------------------------------------------------------- speech

    async def say(self, text: str, *, interruptible: bool = True, append_to_context: bool = True) -> None:
        """Make the bot speak on its own (no user turn)."""
        if not self._task or self._muted:
            return
        frame = TTSSpeakFrame(text=text, append_to_context=append_to_context)
        frame.interruptible = interruptible
        await self._task.queue_frame(frame)

    # ----------------------------------------------------------------- UI state

    async def snapshot_messages(self) -> list[dict[str, Any]]:
        if not self._tools.connected:
            return []
        snap = await self._tools.call_json("snapshot", timeout=5)
        ts = int(time.time() * 1000)
        msgs = [
            {"type": "reminders", "ts": ts, "items": snap.get("reminders", [])},
            {"type": "alarms", "ts": ts, "items": snap.get("alarms", [])},
        ]
        if self._settings is not None:
            msgs.append({"type": "config", "ts": ts, **self._settings.config()})
        return msgs

    async def push_snapshots(self) -> None:
        for msg in await self.snapshot_messages():
            self._bus.emit(msg["type"], **{k: v for k, v in msg.items() if k not in ("type", "ts")})

    async def on_tool_result(self, name: str, args: dict[str, Any] | None, result: Any) -> None:
        """Called by the UI observer after every LLM tool call; turns results into widget events."""
        if not isinstance(result, dict) or result.get("error"):
            return
        if name == "create_reminder":
            self._bus.emit("reminder_created", id=result.get("id"), text=result.get("text"), due_at=result.get("due_at"))
        elif name == "create_alarm":
            self._bus.emit(
                "alarm_created",
                id=result.get("id"),
                label=result.get("label", ""),
                time=result.get("time"),
                next_fire=result.get("next_fire"),
                repeat_daily=result.get("repeat_daily", False),
            )
        elif name == "stop_alarm":
            for alarm in result.get("alarms", []):
                self._bus.emit("alarm_stopped", id=alarm["id"])
        elif name == "get_current_time":
            self._bus.emit("time_told", iso=result.get("iso"), spoken=result.get("spoken"))
        elif name == "complete_reminder":
            self._bus.emit("reminder_done", id=result.get("id"))
        elif name in ("delete_reminder", "delete_alarm"):
            self._bus.emit(f"{result.get('deleted', 'item')}_deleted", id=result.get("id"))
        if name in (
            "create_reminder",
            "create_alarm",
            "stop_alarm",
            "complete_reminder",
            "delete_reminder",
            "delete_alarm",
        ):
            await self.push_snapshots()

    # --------------------------------------------------------------- UI commands

    async def handle_command(self, msg: dict[str, Any]) -> None:
        kind = msg.get("type")
        if kind == "alarm_stop":
            result = await self._tools.call_json("stop_alarm", {"id": msg.get("id")}, timeout=5)
            for alarm in result.get("alarms", []):
                self._bus.emit("alarm_stopped", id=alarm["id"])
            await self.push_snapshots()
        elif kind == "alarm_delete":
            await self._tools.call_json("delete_alarm", {"id": int(msg["id"])}, timeout=5)
            await self.push_snapshots()
        elif kind == "reminder_done":
            await self._tools.call_json("complete_reminder", {"id": int(msg["id"])}, timeout=5)
            self._bus.emit("reminder_done", id=int(msg["id"]))
            await self.push_snapshots()
        elif kind == "reminder_delete":
            await self._tools.call_json("delete_reminder", {"id": int(msg["id"])}, timeout=5)
            await self.push_snapshots()
        elif kind == "mute":
            self._muted = bool(msg.get("muted", True))
            if self._task:
                await self._task.queue_frame(STTMuteFrame(mute=self._muted))
            self._bus.emit("muted", muted=self._muted)
        elif kind == "interrupt":
            if self._task:
                await self._task.queue_frame(InterruptionFrame())
        elif kind == "say":
            # Debug helper for the UI: make the bot speak arbitrary text.
            await self.say(str(msg.get("text", "")), interruptible=True, append_to_context=False)
        elif kind == "refresh":
            await self.push_snapshots()
        elif self._settings is not None and await self._settings.handle(msg):
            pass
        else:
            logger.debug(f"unknown UI command: {kind}")
