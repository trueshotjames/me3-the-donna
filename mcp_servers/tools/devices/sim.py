"""Simulated devices: the house exists in SQLite and in the UI widgets.

Each device keeps a small JSON state (persisted through `persist`, so a restart finds the
lights as they were) and advances time-based behaviour lazily in `tick()`:

    lights       switch and fade at once (transition_ms tells the UI how long to animate)
    thermostat   the room temperature moves toward the target at RATE_C_PER_S while heating/cooling
    media        a track plays from `started_at`; when it ends the next one starts
    phone        a call rings for RING_SECS and then connects; a message is delivered after DELIVER_SECS

Nothing here touches hardware; see devices/hardware.py for the real backends.
"""

from __future__ import annotations

import random
import time
from typing import Any, Callable

from mcp_servers.tools.devices.base import DeviceError, LightDevice, MediaPlayer, PhoneDevice, ThermostatDevice

Persist = Callable[[str, dict[str, Any]], None]
Load = Callable[[str], dict[str, Any] | None]


def _now() -> float:
    return time.time()


# ------------------------------------------------------------------------------------ lights

COLORS = {
    # name -> hex. Warm white is the "on" default; the UI paints the bulb with this.
    "warm": "#ffd9a0", "warm white": "#ffd9a0", "white": "#ffffff", "cool white": "#dbe9ff", "daylight": "#dbe9ff",
    "red": "#ff5a5a", "orange": "#ffa040", "yellow": "#ffe866", "green": "#5ad67d", "teal": "#3fd1c4",
    "cyan": "#5ee0ff", "blue": "#5a8dff", "purple": "#a876ff", "violet": "#a876ff", "pink": "#ff7ad9",
    "magenta": "#ff5ad6", "lavender": "#c9b6ff", "amber": "#ffbf40", "gold": "#ffd24d",
}
DEFAULT_ROOMS = ["living room", "bedroom", "kitchen"]
LIGHT_TRANSITION_MS = 450


def parse_color(text: str | None) -> tuple[str, str] | None:
    """'warm white' -> ('warm white', '#ffd9a0'); '#ff8800' -> ('#ff8800', '#ff8800'); None for nothing."""
    if not text:
        return None
    t = text.strip().lower()
    if t.startswith("#") and len(t) in (4, 7) and all(c in "0123456789abcdef" for c in t[1:]):
        if len(t) == 4:
            t = "#" + "".join(c * 2 for c in t[1:])
        return (t, t)
    if t in COLORS:
        return (t, COLORS[t])
    for name, hexv in COLORS.items():  # "light blue" -> blue
        if name in t.split() or t.endswith(name):
            return (name, hexv)
    raise DeviceError(f"unknown color {text!r}; try warm white, white, red, orange, yellow, green, blue, purple or pink")


class SimLights(LightDevice):
    backend = "sim"

    def __init__(self, rooms: list[str] | None = None, *, load: Load | None = None, persist: Persist | None = None):
        self._rooms = [r.strip().lower() for r in (rooms or DEFAULT_ROOMS) if r.strip()]
        self._persist = persist or (lambda name, state: None)
        saved = (load(self.name) if load else None) or {}
        self._lights: dict[str, dict[str, Any]] = {}
        for room in self._rooms:
            prev = saved.get(room) or {}
            self._lights[room] = {
                "room": room,
                "on": bool(prev.get("on", False)),
                "brightness": int(prev.get("brightness", 100)),
                "color": str(prev.get("color", "#ffd9a0")),
                "color_name": str(prev.get("color_name", "warm white")),
            }

    def rooms(self) -> list[str]:
        return list(self._rooms)

    def _match(self, room: str | None) -> list[str]:
        r = (room or "all").strip().lower()
        if r in ("", "all", "every", "everywhere", "the house", "house", "all lights", "all the lights", "lights"):
            return list(self._rooms)
        for name in self._rooms:  # "the kitchen light" -> kitchen
            if name == r or name in r or r in name:
                return [name]
        raise DeviceError(f"no light called {room!r}; the rooms are {', '.join(self._rooms)}")

    def set(self, room: str, *, on: bool | None = None, brightness: int | None = None, color: str | None = None) -> dict[str, Any]:
        if on is None and brightness is None and color is None:
            raise DeviceError("say what to change: on or off, a brightness percent, or a color")
        col = parse_color(color)
        changed = []
        for name in self._match(room):
            light = self._lights[name]
            if brightness is not None:
                light["brightness"] = max(0, min(100, int(brightness)))
                light["on"] = light["brightness"] > 0 if on is None else bool(on)
            if col is not None:
                light["color_name"], light["color"] = col
                if on is None and brightness is None:
                    light["on"] = True  # "make the bedroom blue" turns it on
            if on is not None:
                light["on"] = bool(on)
                if on and light["brightness"] == 0:
                    light["brightness"] = 100
            changed.append(dict(light))
        self._persist(self.name, self._lights)
        return {"lights": changed, "transition_ms": LIGHT_TRANSITION_MS}

    def status(self) -> dict[str, Any]:
        return {"backend": self.backend, "lights": [dict(l) for l in self._lights.values()]}


# -------------------------------------------------------------------------------- thermostat

MODES = ("heat", "cool", "auto", "off")
RATE_C_PER_S = 0.05  # 3 degrees a minute: fast enough to watch in a demo
AMBIENT_C = 24.0  # where the room drifts when the thermostat is off (a warm country)
AMBIENT_RATE_C_PER_S = 0.01


class SimThermostat(ThermostatDevice):
    backend = "sim"

    def __init__(self, *, unit: str = "C", load: Load | None = None, persist: Persist | None = None):
        self._persist = persist or (lambda name, state: None)
        saved = (load(self.name) if load else None) or {}
        self.unit = "F" if str(unit).upper().startswith("F") else "C"
        self._s: dict[str, Any] = {
            "target": float(saved.get("target", 22.0)),
            "current": float(saved.get("current", 24.0)),
            "mode": str(saved.get("mode", "auto")),
            "min": 10.0,
            "max": 32.0,
            "t": float(saved.get("t", _now())),
        }
        if self._s["mode"] not in MODES:
            self._s["mode"] = "auto"

    def _activity(self) -> str:
        s = self._s
        if s["mode"] == "off":
            return "off"
        diff = s["target"] - s["current"]
        if abs(diff) < 0.15:
            return "idle"
        if diff > 0 and s["mode"] in ("heat", "auto"):
            return "heating"
        if diff < 0 and s["mode"] in ("cool", "auto"):
            return "cooling"
        return "idle"

    def tick(self) -> bool:
        s = self._s
        now = _now()
        dt = max(0.0, min(60.0, now - s["t"]))
        s["t"] = now
        if dt <= 0:
            return False
        before = round(s["current"], 1)
        act = self._activity()
        if act == "heating":
            s["current"] = min(s["target"], s["current"] + RATE_C_PER_S * dt)
        elif act == "cooling":
            s["current"] = max(s["target"], s["current"] - RATE_C_PER_S * dt)
        else:  # idle or off: drift slowly toward the ambient temperature
            step = AMBIENT_RATE_C_PER_S * dt
            if s["current"] < AMBIENT_C:
                s["current"] = min(AMBIENT_C, s["current"] + step)
            elif s["current"] > AMBIENT_C:
                s["current"] = max(AMBIENT_C, s["current"] - step)
        changed = round(s["current"], 1) != before
        if changed:
            self._persist(self.name, s)
        return changed

    def set(self, *, target: float | None = None, mode: str | None = None) -> dict[str, Any]:
        self.tick()
        s = self._s
        if target is None and mode is None:
            raise DeviceError("say a target temperature or a mode (heat, cool, auto, off)")
        if mode is not None:
            m = mode.strip().lower()
            if m in ("heating",):
                m = "heat"
            if m in ("cooling", "ac", "air conditioning"):
                m = "cool"
            if m not in MODES:
                raise DeviceError(f"unknown mode {mode!r}; use heat, cool, auto or off")
            s["mode"] = m
        if target is not None:
            t = float(target)
            if self.unit == "F":
                t = (t - 32) * 5 / 9
            if not (s["min"] <= t <= s["max"]):
                lo, hi = self._out(s["min"]), self._out(s["max"])
                raise DeviceError(f"target must be between {lo} and {hi} degrees")
            s["target"] = round(t, 1)
            if s["mode"] == "off":
                s["mode"] = "auto"
        self._persist(self.name, s)
        return self.status()

    def _out(self, c: float) -> float:
        return round(c * 9 / 5 + 32 if self.unit == "F" else c, 1)

    def status(self) -> dict[str, Any]:
        s = self._s
        return {
            "backend": self.backend,
            "unit": self.unit,
            "target": self._out(s["target"]),
            "current": self._out(s["current"]),
            "mode": s["mode"],
            "activity": self._activity(),
            "min": self._out(s["min"]),
            "max": self._out(s["max"]),
        }


# ------------------------------------------------------------------------------------- media

# A small made-up library (no real songs). `art` is a gradient pair for the UI.
LIBRARY: list[dict[str, Any]] = [
    {"id": 1, "title": "Morning Light", "artist": "Sim Quartet", "genre": "jazz", "duration_s": 212, "playlists": ["favorites", "morning"], "art": ["#f59e0b", "#ef4444"]},
    {"id": 2, "title": "Blue Hour", "artist": "Sim Quartet", "genre": "jazz", "duration_s": 188, "playlists": ["evening"], "art": ["#3b82f6", "#1e3a8a"]},
    {"id": 3, "title": "Rainy Window", "artist": "Lo-Fi Lab", "genre": "lo-fi", "duration_s": 165, "playlists": ["focus", "study", "chill"], "art": ["#8b5cf6", "#ec4899"]},
    {"id": 4, "title": "Tape Hiss Dreams", "artist": "Lo-Fi Lab", "genre": "lo-fi", "duration_s": 174, "playlists": ["focus", "study", "chill"], "art": ["#14b8a6", "#8b5cf6"]},
    {"id": 5, "title": "Sonata in Sim Minor", "artist": "Ada Virtuoso", "genre": "classical", "duration_s": 254, "playlists": ["focus", "calm"], "art": ["#e5e7eb", "#6b7280"]},
    {"id": 6, "title": "Nocturne for Pi", "artist": "Ada Virtuoso", "genre": "classical", "duration_s": 231, "playlists": ["calm", "evening", "sleep"], "art": ["#64748b", "#0f172a"]},
    {"id": 7, "title": "Garage Door", "artist": "The Resistors", "genre": "rock", "duration_s": 197, "playlists": ["workout", "party"], "art": ["#ef4444", "#111827"]},
    {"id": 8, "title": "Overclocked", "artist": "The Resistors", "genre": "rock", "duration_s": 183, "playlists": ["workout"], "art": ["#f97316", "#7c2d12"]},
    {"id": 9, "title": "Neon Weekend", "artist": "Pixel Pop", "genre": "pop", "duration_s": 201, "playlists": ["party", "favorites", "morning"], "art": ["#ec4899", "#f59e0b"]},
    {"id": 10, "title": "Sunrise Bakery", "artist": "Pixel Pop", "genre": "pop", "duration_s": 178, "playlists": ["morning", "favorites"], "art": ["#fbbf24", "#f472b6"]},
    {"id": 11, "title": "Deep Field", "artist": "Orbit", "genre": "ambient", "duration_s": 263, "playlists": ["sleep", "calm", "focus"], "art": ["#0ea5e9", "#312e81"]},
    {"id": 12, "title": "Slow Tide", "artist": "Orbit", "genre": "ambient", "duration_s": 240, "playlists": ["sleep", "calm"], "art": ["#22d3ee", "#0f766e"]},
]
GENRE_ALIASES = {"jazzy": "jazz", "lofi": "lo-fi", "lo fi": "lo-fi", "chillhop": "lo-fi", "classic": "classical", "piano": "classical", "orchestral": "classical", "metal": "rock", "guitar": "rock", "dance": "pop", "electronic": "ambient", "relaxing": "ambient", "relax": "calm", "relaxation": "calm", "sleeping": "sleep", "bedtime": "sleep", "work": "focus", "studying": "study", "exercise": "workout", "gym": "workout", "running": "workout"}
REPEAT_MODES = ("off", "one", "all")


class SimMediaPlayer(MediaPlayer):
    backend = "sim"

    def __init__(self, *, load: Load | None = None, persist: Persist | None = None, library: list[dict[str, Any]] | None = None):
        self._persist = persist or (lambda name, state: None)
        self._lib = library or LIBRARY
        self._by_id = {t["id"]: t for t in self._lib}
        saved = (load(self.name) if load else None) or {}
        self._s: dict[str, Any] = {
            "state": saved.get("state", "stopped"),  # playing | paused | stopped
            "queue": [i for i in saved.get("queue", []) if i in self._by_id],
            "index": int(saved.get("index", 0)),
            "position_at": float(saved.get("position_at", 0.0)),  # seconds into the track when the state last changed
            "started_at": float(saved.get("started_at", 0.0)),  # wall clock of that change (while playing)
            "volume": int(saved.get("volume", 60)),
            "muted": bool(saved.get("muted", False)),
            "shuffle": bool(saved.get("shuffle", False)),
            "repeat": saved.get("repeat", "off"),
            "source": saved.get("source", ""),  # what the user asked for ("jazz", "focus playlist")
        }
        if self._s["state"] == "playing" and not self._s["queue"]:
            self._s["state"] = "stopped"
        self.tick()

    # ---- helpers
    def _track(self) -> dict[str, Any] | None:
        q, i = self._s["queue"], self._s["index"]
        return self._by_id.get(q[i]) if q and 0 <= i < len(q) else None

    def _position(self) -> float:
        s = self._s
        pos = s["position_at"]
        if s["state"] == "playing":
            pos += _now() - s["started_at"]
        tr = self._track()
        return min(pos, tr["duration_s"]) if tr else 0.0

    def _save(self) -> None:
        self._persist(self.name, self._s)

    def _start(self, index: int, position: float = 0.0) -> None:
        s = self._s
        s["index"], s["position_at"], s["started_at"], s["state"] = index, position, _now(), "playing"

    def tick(self) -> bool:
        s = self._s
        tr = self._track()
        if s["state"] != "playing" or tr is None:
            return False
        if _now() - s["started_at"] + s["position_at"] < tr["duration_s"]:
            return False
        # the track ended
        if s["repeat"] == "one":
            self._start(s["index"])
        elif s["index"] + 1 < len(s["queue"]):
            self._start(s["index"] + 1)
        elif s["repeat"] == "all":
            self._start(0)
        else:
            s["state"], s["position_at"] = "stopped", 0.0
        self._save()
        return True

    def _search(self, query: str) -> tuple[list[int], str]:
        q = query.strip().lower()
        for prefix in ("some ", "me some ", "me ", "a ", "the "):
            if q.startswith(prefix):
                q = q[len(prefix):]
        for suffix in (" music", " songs", " song", " playlist", " tracks", " please"):
            if q.endswith(suffix):
                q = q[: -len(suffix)]
        q = GENRE_ALIASES.get(q, q)
        if not q or q in ("something", "anything", "music", "favorites", "my favorites", "my music"):
            ids = [t["id"] for t in self._lib if "favorites" in t["playlists"]]
            return ids or [t["id"] for t in self._lib], "favorites"
        title = [t["id"] for t in self._lib if q == t["title"].lower()]
        if title:
            return title, self._by_id[title[0]]["title"]
        artist = [t["id"] for t in self._lib if q == t["artist"].lower() or q in t["artist"].lower()]
        if artist:
            return artist, self._by_id[artist[0]]["artist"]
        genre = [t["id"] for t in self._lib if q == t["genre"] or q in t["genre"]]
        if genre:
            return genre, self._by_id[genre[0]]["genre"]
        pl = [t["id"] for t in self._lib if any(q == p or q in p for p in t["playlists"])]
        if pl:
            return pl, f"{q} playlist"
        loose = [t["id"] for t in self._lib if q in t["title"].lower()]
        if loose:
            return loose, self._by_id[loose[0]]["title"]
        raise DeviceError(f"nothing in the library matches {query!r}; try a genre like jazz, lo-fi, classical, rock, pop or ambient, or a playlist like focus, sleep or workout")

    # ---- commands
    def play(self, query: str = "") -> dict[str, Any]:
        self.tick()
        s = self._s
        if not query.strip() and s["state"] == "paused" and self._track():
            return self.resume()
        ids, source = self._search(query)
        if s["shuffle"]:
            ids = random.sample(ids, len(ids))
        s["queue"], s["source"] = ids, source
        self._start(0)
        self._save()
        return self.status()

    def pause(self) -> dict[str, Any]:
        self.tick()
        s = self._s
        if s["state"] == "playing":
            s["position_at"], s["state"] = self._position(), "paused"
            self._save()
        return self.status()

    def resume(self) -> dict[str, Any]:
        self.tick()
        s = self._s
        if s["state"] == "paused" and self._track():
            s["started_at"], s["state"] = _now(), "playing"
        elif s["state"] == "stopped":
            if not s["queue"]:
                return self.play("")
            self._start(s["index"] if 0 <= s["index"] < len(s["queue"]) else 0)
        self._save()
        return self.status()

    def stop(self) -> dict[str, Any]:
        s = self._s
        s["state"], s["position_at"] = "stopped", 0.0
        self._save()
        return self.status()

    def next(self) -> dict[str, Any]:
        self.tick()
        s = self._s
        if not s["queue"]:
            raise DeviceError("nothing is queued; say what to play first")
        if s["index"] + 1 < len(s["queue"]):
            self._start(s["index"] + 1)
        else:  # a skip at the end wraps around to the start of the queue
            self._start(0)
        self._save()
        return self.status()

    def previous(self) -> dict[str, Any]:
        self.tick()
        s = self._s
        if not s["queue"]:
            raise DeviceError("nothing is queued; say what to play first")
        if self._position() > 4.0 or s["index"] == 0:
            self._start(s["index"])  # restart the current track, like a car stereo
        else:
            self._start(s["index"] - 1)
        self._save()
        return self.status()

    def set_volume(self, level: int) -> dict[str, Any]:
        s = self._s
        s["volume"] = max(0, min(100, int(level)))
        s["muted"] = False
        self._save()
        return self.status()

    def volume_step(self, delta: int) -> dict[str, Any]:
        return self.set_volume(self._s["volume"] + int(delta))

    def set_mute(self, muted: bool) -> dict[str, Any]:
        self._s["muted"] = bool(muted)
        self._save()
        return self.status()

    def set_shuffle(self, on: bool) -> dict[str, Any]:
        s = self._s
        s["shuffle"] = bool(on)
        if on and len(s["queue"]) > 1:
            cur = s["queue"][s["index"]] if 0 <= s["index"] < len(s["queue"]) else None
            rest = [i for i in s["queue"] if i != cur]
            random.shuffle(rest)
            s["queue"] = ([cur] if cur is not None else []) + rest
            s["index"] = 0
        self._save()
        return self.status()

    def set_repeat(self, mode: str) -> dict[str, Any]:
        m = (mode or "off").strip().lower()
        m = {"track": "one", "song": "one", "this": "one", "on": "all", "queue": "all", "playlist": "all"}.get(m, m)
        if m not in REPEAT_MODES:
            raise DeviceError("repeat can be off, one or all")
        self._s["repeat"] = m
        self._save()
        return self.status()

    def status(self) -> dict[str, Any]:
        self.tick()
        s = self._s
        tr = self._track()
        pos = self._position()
        up_next = [self._by_id[i] for i in s["queue"][s["index"] + 1 : s["index"] + 4]] if s["queue"] else []
        return {
            "backend": self.backend,
            "state": s["state"],
            "track": {"title": tr["title"], "artist": tr["artist"], "genre": tr["genre"], "duration_s": tr["duration_s"], "art": tr["art"]} if tr else None,
            "position_s": round(pos, 1),
            "started_at": s["started_at"] if s["state"] == "playing" else None,  # the UI advances the bar from this
            "volume": s["volume"],
            "muted": s["muted"],
            "shuffle": s["shuffle"],
            "repeat": s["repeat"],
            "source": s["source"],
            "queue_length": len(s["queue"]),
            "queue_index": s["index"],
            "up_next": [{"title": t["title"], "artist": t["artist"]} for t in up_next],
        }


# ------------------------------------------------------------------------------------- phone

DEFAULT_CONTACTS = ["Mom", "Dad", "Alex", "Sam", "Dr. Reyes", "Work"]
RING_SECS = 4.0
DELIVER_SECS = 1.5
MAX_MESSAGES = 12


class SimPhone(PhoneDevice):
    backend = "sim"

    def __init__(self, contacts: list[str] | None = None, *, load: Load | None = None, persist: Persist | None = None):
        self._persist = persist or (lambda name, state: None)
        self._contacts = [c.strip() for c in (contacts or DEFAULT_CONTACTS) if c.strip()]
        saved = (load(self.name) if load else None) or {}
        self._s: dict[str, Any] = {
            "call": saved.get("call"),  # {contact, state: ringing|connected|ended, started_at, connected_at, ended_at, duration_s}
            "messages": list(saved.get("messages", []))[-MAX_MESSAGES:],
            "next_id": int(saved.get("next_id", 1)),
        }
        call = self._s["call"]
        if call and call.get("state") in ("ringing", "connected") and _now() - call.get("started_at", 0) > 3600:
            call["state"], call["ended_at"] = "ended", _now()  # a stale call from a previous run

    def contacts(self) -> list[str]:
        return list(self._contacts)

    def _resolve(self, contact: str) -> str:
        c = (contact or "").strip().lower()
        for prefix in ("my ", "to "):
            if c.startswith(prefix):
                c = c[len(prefix):]
        if not c:
            raise DeviceError("who should I contact? " + ", ".join(self._contacts))
        for name in self._contacts:
            if name.lower() == c:
                return name
        for name in self._contacts:
            if c in name.lower() or name.lower() in c:
                return name
        raise DeviceError(f"no contact called {contact!r}; the contacts are {', '.join(self._contacts)}")

    def tick(self) -> bool:
        changed = False
        now = _now()
        call = self._s["call"]
        if call and call["state"] == "ringing" and now - call["started_at"] >= RING_SECS:
            call["state"], call["connected_at"] = "connected", now
            changed = True
        for m in self._s["messages"]:
            if m["state"] == "sent" and now - m["t"] >= DELIVER_SECS:
                m["state"], m["delivered_at"] = "delivered", now
                changed = True
        if changed:
            self._persist(self.name, self._s)
        return changed

    def call(self, contact: str) -> dict[str, Any]:
        self.tick()
        name = self._resolve(contact)
        cur = self._s["call"]
        if cur and cur["state"] in ("ringing", "connected"):
            raise DeviceError(f"already on a call with {cur['contact']}; say hang up first")
        self._s["call"] = {"contact": name, "state": "ringing", "started_at": _now(), "connected_at": None, "ended_at": None, "duration_s": 0}
        self._persist(self.name, self._s)
        return {"call": dict(self._s["call"]), "note": "simulated call: it connects after a few seconds"}

    def hang_up(self) -> dict[str, Any]:
        self.tick()
        call = self._s["call"]
        if not call or call["state"] == "ended":
            raise DeviceError("there is no call to end")
        now = _now()
        call["state"], call["ended_at"] = "ended", now
        call["duration_s"] = int(now - call["connected_at"]) if call.get("connected_at") else 0
        self._persist(self.name, self._s)
        return {"call": dict(call)}

    def send_message(self, contact: str, text: str) -> dict[str, Any]:
        self.tick()
        name = self._resolve(contact)
        body = (text or "").strip()
        if not body:
            raise DeviceError("what should the message say?")
        msg = {"id": self._s["next_id"], "contact": name, "text": body, "state": "sent", "t": _now(), "delivered_at": None}
        self._s["next_id"] += 1
        self._s["messages"] = (self._s["messages"] + [msg])[-MAX_MESSAGES:]
        self._persist(self.name, self._s)
        return {"message": dict(msg), "note": "simulated: delivered in a moment"}

    def status(self) -> dict[str, Any]:
        self.tick()
        call = self._s["call"]
        if call and call["state"] == "connected":
            call = dict(call, duration_s=int(_now() - call["connected_at"]))
        return {"backend": self.backend, "contacts": self.contacts(), "call": call, "messages": list(self._s["messages"])}
