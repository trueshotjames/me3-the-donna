"""Tools MCP server: the assistant's real-world tools (reminders, time, alarms, timers, weather, the house).

Backed by SQLite at data/assistant.db. Runs on the Pi.

LLM-facing tools (registered with the language model):
    get_current_time, create_reminder, list_reminders, complete_reminder, delete_reminder,
    create_alarm, list_alarms, stop_alarm, delete_alarm,
    create_timer, cancel_timer, get_weather,
    control_music, set_light, set_thermostat, home_status, send_message, make_call, end_call
Internal tools (used by the assistant's scheduler and UI bridge, hidden from the LLM):
    poll_due, snapshot, home_snapshot, stop_timer

The household devices (lights, thermostat, music, phone) are simulated by default and live
behind the interfaces in mcp_servers/tools/devices/ (see its README for real hardware).

Transport: streamable HTTP on 127.0.0.1:8766 by default (so the assistant and the
launcher can both reach it), or stdio with --stdio.

    python -m mcp_servers.tools.server            # HTTP on $TOOLS_MCP_HOST:$TOOLS_MCP_PORT
    python -m mcp_servers.tools.server --stdio
"""

from __future__ import annotations

import argparse
import functools
import inspect
import json
import re
import time
from datetime import datetime, timedelta
from typing import Any

from loguru import logger
from mcp.server.mcpserver import MCPServer

from mcp_servers.common import DATA_DIR, env, env_int, setup
from mcp_servers.tools.db import Store, iso, now, parse_iso
from mcp_servers.tools.devices import DeviceError, build_devices
from mcp_servers.tools.weather import WeatherClient

setup("tools")

DB_PATH = DATA_DIR / env("ASSISTANT_DB", "assistant.db")
HOST = env("TOOLS_MCP_HOST", "127.0.0.1")
PORT = env_int("TOOLS_MCP_PORT", 8766)

from mcp_servers.tools.catalog import LLM_TOOLS  # noqa: E402,F401

mcp = MCPServer(
    "tools",
    log_level="WARNING",
    instructions="Reminders, alarms, timers, the time, the weather and the house (lights, thermostat, music, phone) for the ME3 voice assistant.",
)
store = Store(DB_PATH)
devices = build_devices(store.get_state, store.set_state)
weather = WeatherClient(default_city=env("WEATHER_DEFAULT_CITY", "") or "")
EXEC_LOG = DATA_DIR / "tool_exec.jsonl"  # read by the assistant's turn record (assistant/turn_metrics.py)
_register = mcp.tool


def _log_exec(name: str, t0: float) -> None:
    t1 = time.time()
    try:
        with open(EXEC_LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"tool": name, "t_start": t0, "t_end": t1, "exec_ms": round((t1 - t0) * 1000, 2)}) + "\n")
    except OSError:
        pass


def _timed_tool(*args, **kwargs):
    """Like @mcp.tool(), but also records how long each call spends inside this server (sync or async tools)."""
    decorator = _register(*args, **kwargs)

    def wrap(fn):
        if inspect.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def timed_async(*a, **k):
                t0 = time.time()
                try:
                    return await fn(*a, **k)
                finally:
                    _log_exec(fn.__name__, t0)

            return decorator(timed_async)

        @functools.wraps(fn)
        def timed(*a, **k):
            t0 = time.time()
            try:
                return fn(*a, **k)
            finally:
                _log_exec(fn.__name__, t0)

        return decorator(timed)

    return wrap


mcp.tool = _timed_tool  # type: ignore[method-assign]

try:  # keep the timing log small
    if EXEC_LOG.exists() and EXEC_LOG.stat().st_size > 1_000_000:
        EXEC_LOG.write_text("\n".join(EXEC_LOG.read_text(encoding="utf-8").splitlines()[-1000:]) + "\n", encoding="utf-8")
except OSError:
    pass


def _j(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False)


def _parse_hhmm(text: str) -> str:
    """Accept '7:30', '07:30', '7 am', '7:30 pm', '19h30'; return 'HH:MM'."""
    t = text.strip().lower().replace(".", "")
    m = re.match(r"^(\d{1,2})(?::|h)?(\d{2})?\s*(am|pm)?$", t)
    if not m:
        raise ValueError(f"cannot parse time {text!r}; use HH:MM (24-hour) or '7:30 pm'")
    hour = int(m.group(1))
    minute = int(m.group(2) or 0)
    ampm = m.group(3)
    if ampm == "pm" and hour < 12:
        hour += 12
    if ampm == "am" and hour == 12:
        hour = 0
    if not (0 <= hour < 24 and 0 <= minute < 60):
        raise ValueError(f"time out of range: {text!r}")
    return f"{hour:02d}:{minute:02d}"


def _speakable(dt: datetime) -> str:
    return dt.strftime("%A, %B %d at %I:%M %p").replace(" 0", " ").replace("at 0", "at ")


# ---------------------------------------------------------------- LLM tools


@mcp.tool()
def get_current_time() -> str:
    """Get the current local date and time. Call this before computing any reminder time."""
    dt = now()
    return _j(
        {
            "iso": iso(dt),
            "date": dt.strftime("%Y-%m-%d"),
            "time": dt.strftime("%H:%M"),
            "weekday": dt.strftime("%A"),
            "spoken": dt.strftime("%I:%M %p on %A, %B %d").lstrip("0").replace(" 0", " "),
            "timezone": datetime.now().astimezone().tzname(),
        }
    )


@mcp.tool()
def create_reminder(text: str, due_at: str | None = None, due_in_minutes: int | None = None) -> str:
    """Create a reminder. `text` is what to remind about. Give either `due_at` (ISO 8601 local time,
    e.g. 2026-10-22T18:30:00) or `due_in_minutes` (relative to now); omit both for an undated reminder."""
    due: datetime | None = None
    if due_in_minutes is not None:
        due = now() + timedelta(minutes=int(due_in_minutes))
    elif due_at:
        try:
            due = parse_iso(due_at)
        except ValueError:
            return _j({"error": f"due_at must be ISO 8601, got {due_at!r}. Call get_current_time first."})
    rem = store.create_reminder(text.strip(), due)
    rem["spoken_due"] = _speakable(due) if due else None
    logger.info(f"reminder created: {rem}")
    return _j({"created": "reminder", **rem})


@mcp.tool()
def list_reminders(include_done: bool = False) -> str:
    """List reminders (pending by default)."""
    return _j({"reminders": store.list_reminders(include_done)})


@mcp.tool()
def complete_reminder(id: int) -> str:
    """Mark a reminder as done."""
    rem = store.complete_reminder(int(id))
    return _j({"completed": "reminder", **rem} if rem else {"error": f"no reminder with id {id}"})


@mcp.tool()
def delete_reminder(id: int) -> str:
    """Delete a reminder permanently. Ask the user to confirm before calling this."""
    ok = store.delete_reminder(int(id))
    return _j({"deleted": "reminder", "id": int(id)} if ok else {"error": f"no reminder with id {id}"})


@mcp.tool()
def create_alarm(time: str, label: str = "", repeat_daily: bool = False) -> str:
    """Set an alarm. `time` is a clock time such as "07:30" or "7:30 pm"; it rings at the next
    occurrence of that time. `label` is optional ("wake up"). `repeat_daily` makes it ring every day."""
    try:
        hhmm = _parse_hhmm(time)
    except ValueError as e:
        return _j({"error": str(e)})
    alarm = store.create_alarm(hhmm, label.strip(), bool(repeat_daily))
    alarm["spoken_next"] = _speakable(parse_iso(alarm["next_fire"])) if alarm["next_fire"] else None
    logger.info(f"alarm created: {alarm}")
    return _j({"created": "alarm", **alarm})


@mcp.tool()
def list_alarms() -> str:
    """List all alarms with their next ring time and whether one is ringing now."""
    return _j({"alarms": store.list_alarms()})


@mcp.tool()
def stop_alarm(id: int | None = None) -> str:
    """Silence a ringing alarm or timer ("stop", "okay", "turn it off"). Omit `id` to stop everything that is ringing."""
    stopped = store.stop_alarm(int(id) if id is not None else None)
    timers = store.stop_timer(None) if id is None else []
    return _j({"stopped": "alarm", "alarms": stopped, "timers": timers, "count": len(stopped) + len(timers)})


@mcp.tool()
def delete_alarm(id: int) -> str:
    """Delete an alarm permanently. Ask the user to confirm before calling this."""
    ok = store.delete_alarm(int(id))
    return _j({"deleted": "alarm", "id": int(id)} if ok else {"error": f"no alarm with id {id}"})


# ------------------------------------------------------------------- timers


def _spoken_duration(seconds: int) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    parts = []
    if h:
        parts.append(f"{h} hour" + ("s" if h != 1 else ""))
    if m:
        parts.append(f"{m} minute" + ("s" if m != 1 else ""))
    if s or not parts:
        parts.append(f"{s} second" + ("s" if s != 1 else ""))
    return " ".join(parts)


@mcp.tool()
def create_timer(minutes: float = 0, seconds: float = 0, label: str = "") -> str:
    """Start a countdown timer ("set a timer for 5 minutes", "a 90 second timer"). Give minutes and/or seconds;
    `label` is optional ("pasta"). It rings when done; stop_alarm silences it."""
    total = int(round(float(minutes or 0) * 60 + float(seconds or 0)))
    if total <= 0:
        return _j({"error": "a timer needs a duration, e.g. minutes=5"})
    if total > 24 * 3600:
        return _j({"error": "a timer can be at most 24 hours; use create_alarm or create_reminder for later"})
    timer = store.create_timer(total, label.strip())
    timer["spoken_duration"] = _spoken_duration(total)
    logger.info(f"timer created: {timer}")
    return _j({"created": "timer", **timer})


@mcp.tool()
def cancel_timer(id: int | None = None) -> str:
    """Cancel a running timer before it rings. Omit `id` to cancel all timers."""
    cancelled = store.cancel_timer(int(id) if id is not None else None)
    if not cancelled:
        return _j({"error": "no running timer" + (f" with id {id}" if id is not None else "")})
    return _j({"cancelled": "timer", "timers": cancelled, "count": len(cancelled)})


# ------------------------------------------------------------------ weather


@mcp.tool()
async def get_weather(city: str = "", day: str = "today") -> str:
    """Current weather and a 3-day outlook from Open-Meteo. `city` such as "Berlin" (empty = the home city);
    `day` is "today", "tomorrow" or "day after tomorrow". Use for any weather or forecast question."""
    return _j(await weather.get(city, day))


# ------------------------------------------------------------------- house


def _device(fn, *args, **kwargs) -> str:
    """Run a device command; a refused command becomes {"error": ...} and every change bumps the UI version."""
    try:
        result = fn(*args, **kwargs)
    except DeviceError as e:
        return _j({"error": str(e)})
    devices.bump()
    return _j(result)


@mcp.tool()
def control_music(action: str, query: str = "", value: int | None = None) -> str:
    """Music and media control. `action`: play (with `query`: a song, artist, genre like jazz or lo-fi, or a playlist
    like focus, sleep, workout; empty = favorites or resume), pause, resume, stop, next, previous, volume_up,
    volume_down, set_volume (`value` 0-100), mute, unmute, shuffle_on, shuffle_off, repeat_off, repeat_one, repeat_all, status."""
    a = (action or "").strip().lower().replace(" ", "_").replace("-", "_")
    a = {"skip": "next", "forward": "next", "back": "previous", "prev": "previous", "unpause": "resume", "continue": "resume",
         "louder": "volume_up", "quieter": "volume_down", "softer": "volume_down", "volume": "set_volume", "now_playing": "status",
         "what_is_playing": "status", "shuffle": "shuffle_on", "repeat": "repeat_all"}.get(a, a)
    m = devices.media
    step = int(value) if value is not None else 10
    table = {
        "play": lambda: m.play(query),
        "pause": m.pause,
        "resume": m.resume,
        "stop": m.stop,
        "next": m.next,
        "previous": m.previous,
        "volume_up": lambda: m.volume_step(abs(step)),
        "volume_down": lambda: m.volume_step(-abs(step)),
        "set_volume": lambda: m.set_volume(int(value if value is not None else 50)),
        "mute": lambda: m.set_mute(True),
        "unmute": lambda: m.set_mute(False),
        "shuffle_on": lambda: m.set_shuffle(True),
        "shuffle_off": lambda: m.set_shuffle(False),
        "repeat_off": lambda: m.set_repeat("off"),
        "repeat_one": lambda: m.set_repeat("one"),
        "repeat_all": lambda: m.set_repeat("all"),
        "status": m.status,
    }
    if a not in table:
        return _j({"error": f"unknown action {action!r}; use play, pause, resume, stop, next, previous, volume_up, volume_down, set_volume, mute, unmute, shuffle_on/off, repeat_off/one/all or status"})
    out = _device(table[a])
    if a == "status":
        devices.version -= 1  # reading changes nothing
    return out


@mcp.tool()
def set_light(room: str = "all", on: bool | None = None, brightness: int | None = None, color: str | None = None) -> str:
    """Control the lights. `room` is a room name or "all". Give `on` (true/false), `brightness` (0-100 percent,
    "dim" = 30) and/or `color` ("warm white", "blue", "#ff8800"). "Dim the lights to 20 percent" = brightness=20."""
    return _device(devices.lights.set, room, on=on, brightness=brightness, color=color)


@mcp.tool()
def set_thermostat(target: float | None = None, mode: str | None = None) -> str:
    """Set the thermostat: `target` temperature in degrees (the house unit) and/or `mode` heat, cool, auto or off.
    For "warmer" or "cooler" read home_status first and move the target by 1 degree."""
    return _device(devices.thermostat.set, target=target, mode=mode)


@mcp.tool()
def home_status() -> str:
    """Current state of the house: every light, the thermostat (current and target temperature), what is playing,
    the phone (call, recent messages) and running timers. Call it when asked what is on, playing or set."""
    devices.tick()
    status = devices.status()
    status["timers"] = store.list_timers()
    return _j(status)


@mcp.tool()
def send_message(contact: str, text: str) -> str:
    """Send a text message to a contact ("text Mom that I'm running late"). `text` is the message as it should be sent."""
    return _device(devices.phone.send_message, contact, text)


@mcp.tool()
def make_call(contact: str) -> str:
    """Call a contact by name ("call Mom"). The call rings and then connects; end_call hangs up."""
    return _device(devices.phone.call, contact)


@mcp.tool()
def end_call() -> str:
    """Hang up the current call ("hang up", "end the call")."""
    return _device(devices.phone.hang_up)


# ------------------------------------------------------------ internal tools


@mcp.tool()
def poll_due() -> str:
    """Internal: reminders, alarms and timers that just became due (marks them notified/ringing), plus the
    house version (changes whenever a device changed, by a command or on its own)."""
    devices.tick()
    return _j(
        {
            "reminders": store.due_reminders(),
            "alarms": store.due_alarms(),
            "ringing": store.ringing_alarms(),
            "timers": store.due_timers(),
            "ringing_timers": store.ringing_timers(),
            "home_version": devices.version,
        }
    )


@mcp.tool()
def stop_timer(id: int | None = None) -> str:
    """Internal (UI Stop button): silence a ringing timer; omit `id` for all."""
    stopped = store.stop_timer(int(id) if id is not None else None)
    return _j({"stopped": "timer", "timers": stopped, "count": len(stopped)})


@mcp.tool()
def snapshot() -> str:
    """Internal: full state for the UI (pending reminders, all alarms, running timers)."""
    return _j({"reminders": store.list_reminders(False), "alarms": store.list_alarms(), "timers": store.list_timers(), "now": iso(now())})


@mcp.tool()
def home_snapshot() -> str:
    """Internal: the house for the UI widgets (lights, thermostat, media, phone) and the last weather answer."""
    devices.tick()
    return _j({**devices.status(), "weather": weather.last})


def main() -> None:
    parser = argparse.ArgumentParser(description="ME3 tools MCP server")
    parser.add_argument("--stdio", action="store_true", help="serve over stdio instead of HTTP")
    parser.add_argument("--host", default=HOST)
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args()
    logger.info(f"database: {DB_PATH}")
    if args.stdio:
        mcp.run(transport="stdio")
    else:
        logger.info(f"serving streamable HTTP on http://{args.host}:{args.port}/mcp")
        mcp.run(transport="streamable-http", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
