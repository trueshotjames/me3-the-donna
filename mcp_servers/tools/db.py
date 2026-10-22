"""SQLite store for reminders and alarms (shared by the tools MCP server)."""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL,
    due_at TEXT,              -- ISO 8601 local time, or NULL for an undated reminder
    done INTEGER NOT NULL DEFAULT 0,
    notified INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS alarms (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    label TEXT NOT NULL DEFAULT '',
    time TEXT NOT NULL,       -- "HH:MM" 24-hour
    next_fire TEXT,           -- ISO 8601 local time of the next ring, NULL when disabled
    repeat_daily INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1,
    ringing INTEGER NOT NULL DEFAULT 0,
    fired_at TEXT,
    created_at TEXT NOT NULL
);
"""


def now() -> datetime:
    return datetime.now().replace(microsecond=0)


def iso(dt: datetime | None) -> str | None:
    return dt.isoformat(timespec="seconds") if dt else None


def parse_iso(text: str) -> datetime:
    return datetime.fromisoformat(text)


def next_occurrence(hhmm: str, base: datetime | None = None) -> datetime:
    base = base or now()
    hour, minute = (int(x) for x in hhmm.split(":"))
    candidate = base.replace(hour=hour, minute=minute, second=0)
    if candidate <= base:
        candidate += timedelta(days=1)
    return candidate


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    # ---------- reminders ----------
    def create_reminder(self, text: str, due_at: datetime | None) -> dict[str, Any]:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO reminders(text, due_at, created_at) VALUES (?, ?, ?)",
                (text, iso(due_at), iso(now())),
            )
            self._conn.commit()
            return self.get_reminder(cur.lastrowid)

    def get_reminder(self, rid: int) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM reminders WHERE id=?", (rid,)).fetchone()
        return self._reminder(row) if row else None

    def list_reminders(self, include_done: bool = False) -> list[dict[str, Any]]:
        q = "SELECT * FROM reminders" + ("" if include_done else " WHERE done=0")
        q += " ORDER BY (due_at IS NULL), due_at, id"
        return [self._reminder(r) for r in self._conn.execute(q).fetchall()]

    def complete_reminder(self, rid: int) -> dict[str, Any] | None:
        with self._lock:
            self._conn.execute("UPDATE reminders SET done=1 WHERE id=?", (rid,))
            self._conn.commit()
        return self.get_reminder(rid)

    def delete_reminder(self, rid: int) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM reminders WHERE id=?", (rid,))
            self._conn.commit()
            return cur.rowcount > 0

    def due_reminders(self) -> list[dict[str, Any]]:
        """Reminders whose time has come and that were not announced yet; marks them notified."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM reminders WHERE done=0 AND notified=0 AND due_at IS NOT NULL AND due_at<=?",
                (iso(now()),),
            ).fetchall()
            ids = [r["id"] for r in rows]
            if ids:
                self._conn.executemany("UPDATE reminders SET notified=1 WHERE id=?", [(i,) for i in ids])
                self._conn.commit()
            return [self._reminder(r) for r in rows]

    @staticmethod
    def _reminder(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "text": row["text"],
            "due_at": row["due_at"],
            "done": bool(row["done"]),
            "notified": bool(row["notified"]),
            "created_at": row["created_at"],
        }

    # ---------- alarms ----------
    def create_alarm(self, hhmm: str, label: str, repeat_daily: bool) -> dict[str, Any]:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO alarms(label, time, next_fire, repeat_daily, created_at) VALUES (?, ?, ?, ?, ?)",
                (label, hhmm, iso(next_occurrence(hhmm)), int(repeat_daily), iso(now())),
            )
            self._conn.commit()
            return self.get_alarm(cur.lastrowid)

    def get_alarm(self, aid: int) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM alarms WHERE id=?", (aid,)).fetchone()
        return self._alarm(row) if row else None

    def list_alarms(self) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM alarms ORDER BY time, id").fetchall()
        return [self._alarm(r) for r in rows]

    def ringing_alarms(self) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM alarms WHERE ringing=1").fetchall()
        return [self._alarm(r) for r in rows]

    def stop_alarm(self, aid: int | None) -> list[dict[str, Any]]:
        """Silence one ringing alarm (or all when aid is None). Returns the alarms stopped."""
        with self._lock:
            if aid is None:
                rows = self._conn.execute("SELECT * FROM alarms WHERE ringing=1").fetchall()
            else:
                rows = self._conn.execute("SELECT * FROM alarms WHERE id=?", (aid,)).fetchall()
            stopped = []
            for r in rows:
                if r["repeat_daily"]:
                    self._conn.execute(
                        "UPDATE alarms SET ringing=0, next_fire=? WHERE id=?",
                        (iso(next_occurrence(r["time"])), r["id"]),
                    )
                else:
                    self._conn.execute(
                        "UPDATE alarms SET ringing=0, enabled=0, next_fire=NULL WHERE id=?", (r["id"],)
                    )
                stopped.append(r["id"])
            self._conn.commit()
        return [a for a in (self.get_alarm(i) for i in stopped) if a]

    def delete_alarm(self, aid: int) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM alarms WHERE id=?", (aid,))
            self._conn.commit()
            return cur.rowcount > 0

    def due_alarms(self) -> list[dict[str, Any]]:
        """Alarms whose next_fire has passed; marks them ringing."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM alarms WHERE enabled=1 AND ringing=0 AND next_fire IS NOT NULL AND next_fire<=?",
                (iso(now()),),
            ).fetchall()
            ids = [r["id"] for r in rows]
            if ids:
                self._conn.executemany(
                    "UPDATE alarms SET ringing=1, fired_at=? WHERE id=?", [(iso(now()), i) for i in ids]
                )
                self._conn.commit()
            return [a for a in (self.get_alarm(i) for i in ids) if a]

    @staticmethod
    def _alarm(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "label": row["label"],
            "time": row["time"],
            "next_fire": row["next_fire"],
            "repeat_daily": bool(row["repeat_daily"]),
            "enabled": bool(row["enabled"]),
            "ringing": bool(row["ringing"]),
            "fired_at": row["fired_at"],
            "created_at": row["created_at"],
        }
