"""Tools MCP server: the assistant's real-world tools (reminders, time, alarms).

Backed by SQLite at data/assistant.db. Runs on the Pi.

LLM-facing tools (registered with the language model):
    get_current_time, create_reminder, list_reminders, complete_reminder, delete_reminder,
    create_alarm, list_alarms, stop_alarm, delete_alarm
Internal tools (used by the assistant's scheduler and UI bridge, hidden from the LLM):
    poll_due, snapshot

Transport: streamable HTTP on 127.0.0.1:8766 by default (so the assistant and the
launcher can both reach it), or stdio with --stdio.

    python -m mcp_servers.tools.server            # HTTP on $TOOLS_MCP_HOST:$TOOLS_MCP_PORT
    python -m mcp_servers.tools.server --stdio
"""

from __future__ import annotations

import argparse
import functools
import json
import re
import time
from datetime import datetime, timedelta
from typing import Any

from loguru import logger
from mcp.server.mcpserver import MCPServer

from mcp_servers.common import DATA_DIR, env, env_int, setup
from mcp_servers.tools.db import Store, iso, now, parse_iso

setup("tools")

DB_PATH = DATA_DIR / env("ASSISTANT_DB", "assistant.db")
HOST = env("TOOLS_MCP_HOST", "127.0.0.1")
PORT = env_int("TOOLS_MCP_PORT", 8766)

from mcp_servers.tools.catalog import LLM_TOOLS  # noqa: E402,F401

mcp = MCPServer("tools", log_level="WARNING", instructions="Reminders, alarms and the current time for the ME3 voice assistant.")
store = Store(DB_PATH)
EXEC_LOG = DATA_DIR / "tool_exec.jsonl"  # read by the assistant's turn record (assistant/turn_metrics.py)
_register = mcp.tool


def _timed_tool(*args, **kwargs):
    """Like @mcp.tool(), but also records how long each call spends inside this server."""
    decorator = _register(*args, **kwargs)

    def wrap(fn):
        @functools.wraps(fn)
        def timed(*a, **k):
            t0 = time.time()
            try:
                return fn(*a, **k)
            finally:
                t1 = time.time()
                try:
                    with open(EXEC_LOG, "a", encoding="utf-8") as fh:
                        fh.write(json.dumps({"tool": fn.__name__, "t_start": t0, "t_end": t1, "exec_ms": round((t1 - t0) * 1000, 2)}) + "\n")
                except OSError:
                    pass

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
    """Silence a ringing alarm. Omit `id` to stop every alarm that is ringing."""
    stopped = store.stop_alarm(int(id) if id is not None else None)
    return _j({"stopped": "alarm", "alarms": stopped, "count": len(stopped)})


@mcp.tool()
def delete_alarm(id: int) -> str:
    """Delete an alarm permanently. Ask the user to confirm before calling this."""
    ok = store.delete_alarm(int(id))
    return _j({"deleted": "alarm", "id": int(id)} if ok else {"error": f"no alarm with id {id}"})


# ------------------------------------------------------------ internal tools


@mcp.tool()
def poll_due() -> str:
    """Internal: return reminders and alarms that just became due (marks them notified/ringing)."""
    return _j({"reminders": store.due_reminders(), "alarms": store.due_alarms(), "ringing": store.ringing_alarms()})


@mcp.tool()
def snapshot() -> str:
    """Internal: full state for the UI (pending reminders and all alarms)."""
    return _j({"reminders": store.list_reminders(False), "alarms": store.list_alarms(), "now": iso(now())})


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
