"""Bot-initiated speech: alarms, timers and timed reminders; the house widgets.

Polls the tools MCP server's `poll_due` once a second. A due reminder is spoken
once, after the user's current query has been answered; a due alarm or timer rings (UI sound)
and is announced by voice, un-interruptibly, as soon as the user is not mid-sentence,
and repeats until the user or the UI stops it. All speech goes through the Announcer
(assistant/announcer.py). Also serves the UI's commands and keeps the UI's
reminder/alarm/timer snapshots fresh.

The house (lights, thermostat, music, phone; mcp_servers/tools/devices/) is pushed to the UI as
one `home` snapshot: right after a device tool call, and whenever `poll_due` reports a new
`home_version` (a track ended, a call connected, the room warmed up). The widgets' own buttons
arrive as UI commands (`light_set`, `thermostat_set`, `media`, `phone`) and call the same tools.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import Any

from loguru import logger
from pipecat.frames.frames import InterruptionFrame, STTMuteFrame, TTSSpeakFrame
from pipecat.pipeline.worker import PipelineWorker

from assistant.announcer import PRIORITY_ALARM, PRIORITY_REMINDER, PRIORITY_URGENT, Announcer
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


def spoken_duration(seconds: int | None) -> str:
    m, s = divmod(int(seconds or 0), 60)
    h, m = divmod(m, 60)
    parts = []
    if h:
        parts.append(f"{h} hour" + ("s" if h != 1 else ""))
    if m:
        parts.append(f"{m} minute" + ("s" if m != 1 else ""))
    if s or not parts:
        parts.append(f"{s} second" + ("s" if s != 1 else ""))
    return " ".join(parts)


# Tools whose result changes what the house widgets show (a `home` snapshot is pushed after them).
HOME_TOOLS = {"control_music", "set_light", "set_thermostat", "send_message", "make_call", "end_call"}
# Internal tools the widgets' own buttons map to: UI command -> (tool, argument names)
WIDGET_COMMANDS = {
    "light_set": ("set_light", ("room", "on", "brightness", "color")),
    "thermostat_set": ("set_thermostat", ("target", "mode")),
    "media": ("control_music", ("action", "query", "value")),
}


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
        self._announcer: Announcer | None = None
        self._poll_secs = poll_secs
        self._reannounce_secs = reannounce_secs
        self._runner: asyncio.Task | None = None
        self._last_announce: dict[int, float] = {}
        self._last_timer_announce: dict[int, float] = {}
        self._home_version: int | None = None  # last home_version seen in poll_due
        self._muted = False
        self._settings = None  # SettingsOps: summarize / VAD calibration / config (assistant/settings_ops.py)

    def set_settings(self, settings) -> None:
        self._settings = settings

    def attach(self, task: PipelineWorker, announcer: Announcer | None = None) -> None:
        self._task = task
        self._announcer = announcer

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
            await self.say(f"Reminder: {rem['text']}.", source="reminder", priority=PRIORITY_REMINDER, ttl=600)
        for alarm in due.get("alarms", []):
            changed = True
            self._bus.emit("alarm_fired", id=alarm["id"], label=alarm.get("label", ""), time=alarm["time"])
            await self._announce_alarm(alarm)
        # Re-announce alarms that keep ringing (the UI plays the sound continuously).
        for alarm in due.get("ringing", []):
            last = self._last_announce.get(alarm["id"], 0.0)
            if time.time() - last >= self._reannounce_secs:
                await self._announce_alarm(alarm)
        # Timers: ring like an alarm until stopped, re-announced on the same schedule.
        for timer in due.get("timers", []):
            changed = True
            self._bus.emit("timer_fired", id=timer["id"], label=timer.get("label", ""), duration_s=timer.get("duration_s"))
            await self._announce_timer(timer)
        for timer in due.get("ringing_timers", []):
            last = self._last_timer_announce.get(timer["id"], 0.0)
            if time.time() - last >= self._reannounce_secs:
                await self._announce_timer(timer)
        if changed:
            await self.push_snapshots()
        # The house changed on its own (a track ended, a call connected, the room warmed up).
        version = due.get("home_version")
        if version is not None and version != self._home_version:
            self._home_version = version
            await self.push_home()

    async def _announce_alarm(self, alarm: dict[str, Any]) -> None:
        self._last_announce[alarm["id"]] = time.time()
        label = alarm.get("label") or ""
        text = f"Alarm. {label}. It's {spoken_time(alarm.get('fired_at'))}." if label else (
            f"Alarm. It's {spoken_time(alarm.get('fired_at'))}. Say stop to turn it off."
        )
        # Cuts into her reply if it has to, but not into his sentence; repeats anyway in 45 s.
        await self.say(text, source="alarm", interruptible=False, wait="speech", priority=PRIORITY_ALARM, ttl=30)

    async def _announce_timer(self, timer: dict[str, Any]) -> None:
        self._last_timer_announce[timer["id"]] = time.time()
        label = timer.get("label") or ""
        length = spoken_duration(timer.get("duration_s"))
        text = f"Your {label} timer is done." if label else f"Your {length} timer is done. Say stop to turn it off."
        await self.say(text, source="timer", interruptible=False, wait="speech", priority=PRIORITY_ALARM, ttl=30)

    # ------------------------------------------------------------------- speech

    async def say(
        self,
        text: str,
        *,
        source: str = "scheduler",
        interruptible: bool = True,
        append_to_context: bool = True,
        wait: str = "turn",
        priority: int = PRIORITY_REMINDER,
        ttl: float | None = None,
    ) -> None:
        """Make the bot speak on its own (no user turn), at the right moment (see Announcer)."""
        if not self._task or self._muted:
            return
        if self._announcer is not None:
            self._announcer.say(
                text, source, interruptible=interruptible, append_to_context=append_to_context,
                wait=wait, priority=priority, ttl=ttl,
            )
            return
        frame = TTSSpeakFrame(text=text, append_to_context=append_to_context)
        frame.interruptible = interruptible
        await self._task.queue_frame(frame)

    async def say_now(self, text: str, *, source: str = "urgent") -> None:
        """Speak at once, without waiting for a quiet moment ("One moment" while the LLM is stuck)."""
        await self.say(text, source=source, append_to_context=False, wait="none", priority=PRIORITY_URGENT, ttl=5)

    # ----------------------------------------------------------------- UI state

    async def snapshot_messages(self) -> list[dict[str, Any]]:
        if not self._tools.connected:
            return []
        snap = await self._tools.call_json("snapshot", timeout=5)
        ts = int(time.time() * 1000)
        msgs = [
            {"type": "reminders", "ts": ts, "items": snap.get("reminders", [])},
            {"type": "alarms", "ts": ts, "items": snap.get("alarms", [])},
            {"type": "timers", "ts": ts, "items": snap.get("timers", [])},
        ]
        if self._settings is not None:
            msgs.append({"type": "config", "ts": ts, **self._settings.config()})
        home = await self.home_message()
        if home:
            msgs.append(home)
        return msgs

    async def home_message(self) -> dict[str, Any] | None:
        """The `home` snapshot message (None when the tools server has no house, e.g. an old server)."""
        try:
            snap = await self._tools.call_json("home_snapshot", timeout=5)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"home_snapshot unavailable: {e!r}")
            return None
        if not isinstance(snap, dict) or snap.get("error"):
            return None
        self._home_version = snap.get("version", self._home_version)
        return {"type": "home", "ts": int(time.time() * 1000), **snap}

    async def push_snapshots(self) -> None:
        for msg in await self.snapshot_messages():
            self._bus.emit(msg["type"], **{k: v for k, v in msg.items() if k not in ("type", "ts")})

    async def push_home(self) -> None:
        msg = await self.home_message()
        if msg:
            self._bus.emit("home", **{k: v for k, v in msg.items() if k not in ("type", "ts")})

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
            for timer in result.get("timers", []):
                self._bus.emit("timer_stopped", id=timer["id"])
        elif name == "get_current_time":
            self._bus.emit("time_told", iso=result.get("iso"), spoken=result.get("spoken"))
        elif name == "complete_reminder":
            self._bus.emit("reminder_done", id=result.get("id"))
        elif name in ("delete_reminder", "delete_alarm"):
            self._bus.emit(f"{result.get('deleted', 'item')}_deleted", id=result.get("id"))
        elif name == "create_timer":
            self._bus.emit(
                "timer_created", id=result.get("id"), label=result.get("label", ""), duration_s=result.get("duration_s"), ends_at=result.get("ends_at")
            )
        elif name == "cancel_timer":
            for timer in result.get("timers", []):
                self._bus.emit("timer_cancelled", id=timer["id"])
        elif name == "get_weather":
            self._bus.emit("weather", **{k: v for k, v in result.items() if k != "error"})
        if name in (
            "create_reminder",
            "create_alarm",
            "stop_alarm",
            "complete_reminder",
            "delete_reminder",
            "delete_alarm",
            "create_timer",
            "cancel_timer",
        ):
            await self.push_snapshots()
        if name in HOME_TOOLS:
            await self.push_home()

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
        elif kind == "timer_stop":
            result = await self._tools.call_json("stop_timer", {"id": msg.get("id")}, timeout=5)
            for timer in result.get("timers", []):
                self._bus.emit("timer_stopped", id=timer["id"])
            await self.push_snapshots()
        elif kind == "timer_cancel":
            result = await self._tools.call_json("cancel_timer", {"id": msg.get("id")}, timeout=5)
            for timer in result.get("timers", []) if isinstance(result, dict) else []:
                self._bus.emit("timer_cancelled", id=timer["id"])
            await self.push_snapshots()
        elif kind in WIDGET_COMMANDS:
            # A widget button: same tool the LLM would call, so the result shape and the UI update are identical.
            tool, names = WIDGET_COMMANDS[kind]
            args = {k: msg[k] for k in names if msg.get(k) is not None}
            result = await self._tools.call_json(tool, args, timeout=10)
            if isinstance(result, dict) and result.get("error"):
                self._bus.emit("warning", message=str(result["error"]), kind="device")
            await self.push_home()
        elif kind == "phone":
            action = str(msg.get("action", ""))
            if action == "hang_up":
                result = await self._tools.call_json("end_call", {}, timeout=5)
            elif action == "call":
                result = await self._tools.call_json("make_call", {"contact": str(msg.get("contact", ""))}, timeout=5)
            elif action == "message":
                result = await self._tools.call_json("send_message", {"contact": str(msg.get("contact", "")), "text": str(msg.get("text", ""))}, timeout=5)
            else:
                result = {"error": f"unknown phone action {action!r}"}
            if isinstance(result, dict) and result.get("error"):
                self._bus.emit("warning", message=str(result["error"]), kind="device")
            await self.push_home()
        elif kind == "home_refresh":
            await self.push_home()
        elif kind == "mute":
            self._muted = bool(msg.get("muted", True))
            if self._announcer is not None:
                self._announcer.set_muted(self._muted)
            if self._task:
                await self._task.queue_frame(STTMuteFrame(mute=self._muted))
            self._bus.emit("muted", muted=self._muted)
        elif kind == "interrupt":
            if self._task:
                await self._task.queue_frame(InterruptionFrame())
        elif kind == "say":
            # Debug helper for the UI: make the bot speak arbitrary text.
            await self.say_now(str(msg.get("text", "")), source="ui")
        elif kind == "refresh":
            await self.push_snapshots()
        elif self._settings is not None and await self._settings.handle(msg):
            pass
        else:
            logger.debug(f"unknown UI command: {kind}")
