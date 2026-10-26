"""
Stub event backend for the UI, used until assistant/ publishes real events.

Serves a WebSocket on ws://127.0.0.1:8765 using only the standard library,
plays a scripted conversation on a loop, and honours the UI commands
(alarm_stop, alarm_delete, reminder_done, reminder_delete, mute, interrupt,
and the house widgets' light_set, thermostat_set, media, phone, timer_*).
Message shape: one JSON object per frame, see app/UI_EVENTS.md.

The house widgets use the real simulated devices (mcp_servers/tools/devices/sim.py) when the
project is present next to app/, so the stub shows exactly what the pipeline would; with only
app/ on disk they are skipped.

Run:  python app/stub_events.py [port]
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import random
import socket
import struct
import sys
import threading
import time
from datetime import datetime, timedelta

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
try:  # the real simulated devices, in memory (no SQLite)
    from mcp_servers.tools.devices.sim import SimLights, SimMediaPlayer, SimPhone, SimThermostat

    house = {"lights": SimLights(), "thermostat": SimThermostat(), "media": SimMediaPlayer(), "phone": SimPhone()}
except Exception as e:  # noqa: BLE001  app/ alone on disk, or a missing module
    print(f"[stub-events] house widgets off ({e!r})", flush=True)
    house = None

clients: list[socket.socket] = []
lock = threading.Lock()

state = {
    "reminders": [
        {"id": "r1", "text": "Submit ME3 report", "due_at": (datetime.now() + timedelta(hours=3)).isoformat(timespec="minutes"), "done": False},
    ],
    "alarms": [
        {"id": "a1", "label": "Morning", "time": "07:00", "enabled": True},
    ],
    "muted": False,
    "interrupt": False,
    "chat": [],
    "talking": None,  # "user" / "bot" while the script speaks (drives the level meter)
    "timers": [],
    "weather": None,
    "home_version": 0,
}

FAKE_WEATHER = {
    "city": "Berlin, Germany", "unit": "C",
    "now": {"temperature": 11.4, "feels_like": 9.1, "humidity": 71, "wind_kmh": 14, "conditions": "partly cloudy", "icon": "cloud-sun", "is_day": True},
    "days": [
        {"date": "2026-10-26", "label": "today", "high": 13, "low": 7, "rain_chance": 20, "conditions": "partly cloudy", "icon": "cloud-sun"},
        {"date": "2026-10-27", "label": "tomorrow", "high": 10, "low": 5, "rain_chance": 70, "conditions": "rain", "icon": "rain"},
        {"date": "2026-10-28", "label": "day after tomorrow", "high": 9, "low": 3, "rain_chance": 10, "conditions": "clear sky", "icon": "sun"},
    ],
    "spoken": "Berlin now: partly cloudy, eleven degrees, feels like nine.",
}


def home_snapshot() -> dict | None:
    if house is None:
        return None
    for d in house.values():
        d.tick()
    return {
        "type": "home", "version": state["home_version"],
        "lights": house["lights"].status()["lights"], "rooms": house["lights"].rooms(),
        "thermostat": house["thermostat"].status(), "media": house["media"].status(), "phone": house["phone"].status(),
        "backends": {k: "sim (stub)" for k in house}, "weather": state["weather"],
    }


def push_home() -> None:
    snap = home_snapshot()
    if snap:
        state["home_version"] += 1
        broadcast(snap)


def timers_snapshot() -> list[dict]:
    out = []
    for t in state["timers"]:
        if t["state"] == "running" and time.time() >= t["ends_at_s"]:
            t["state"] = "ringing"
            broadcast({"type": "timer_fired", "id": t["id"], "label": t["label"], "duration_s": t["duration_s"]})
        out.append({**{k: v for k, v in t.items() if k != "ends_at_s"}, "ringing": t["state"] == "ringing",
                    "remaining_s": max(0, int(t["ends_at_s"] - time.time())) if t["state"] == "running" else 0})
    return out


def house_command(cmd: dict) -> None:
    """The widgets' own buttons, mapped to the simulated devices like the scheduler maps them to the tools."""
    t = cmd.get("type")
    try:
        if t == "light_set":
            house["lights"].set(cmd.get("room", "all"), on=cmd.get("on"), brightness=cmd.get("brightness"), color=cmd.get("color"))
        elif t == "thermostat_set":
            house["thermostat"].set(target=cmd.get("target"), mode=cmd.get("mode"))
        elif t == "media":
            m, a, v = house["media"], cmd.get("action", ""), cmd.get("value")
            {"play": lambda: m.play(cmd.get("query", "")), "pause": m.pause, "resume": m.resume, "stop": m.stop, "next": m.next, "previous": m.previous,
             "set_volume": lambda: m.set_volume(int(v or 0)), "mute": lambda: m.set_mute(True), "unmute": lambda: m.set_mute(False),
             "shuffle_on": lambda: m.set_shuffle(True), "shuffle_off": lambda: m.set_shuffle(False),
             "repeat_off": lambda: m.set_repeat("off"), "repeat_one": lambda: m.set_repeat("one"), "repeat_all": lambda: m.set_repeat("all")}.get(a, m.status)()
        elif t == "phone":
            a = cmd.get("action")
            if a == "call":
                house["phone"].call(cmd.get("contact", ""))
            elif a == "hang_up":
                house["phone"].hang_up()
            elif a == "message":
                house["phone"].send_message(cmd.get("contact", ""), cmd.get("text", ""))
    except Exception as e:  # noqa: BLE001  DeviceError: say it like the scheduler does
        broadcast({"type": "warning", "message": str(e), "kind": "device"})
    push_home()


def chat(role: str, text: str, item_id: int | None = None, **extra) -> int:
    """Add or update one chat entry (same shape as assistant/chat_log.py)."""
    for it in state["chat"]:
        if it["id"] == item_id:
            it.update(text=text, **extra)
            broadcast({"type": "chat", "item": it})
            return item_id
    it = {"id": len(state["chat"]) + 1, "role": role, "text": text, "t": time.time(), **extra}
    state["chat"].append(it)
    broadcast({"type": "chat", "item": it})
    return it["id"]


# ---- minimal RFC 6455 server -------------------------------------------------
def handshake(conn: socket.socket) -> bool:
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = conn.recv(4096)
        if not chunk:
            return False
        data += chunk
    headers = {}
    for line in data.split(b"\r\n")[1:]:
        if b":" in line:
            k, v = line.split(b":", 1)
            headers[k.strip().lower()] = v.strip()
    key = headers.get(b"sec-websocket-key")
    if not key:
        conn.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\n")
        return False
    accept = base64.b64encode(hashlib.sha1(key + GUID.encode()).digest())
    conn.sendall(
        b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
        b"Connection: Upgrade\r\nSec-WebSocket-Accept: " + accept + b"\r\n\r\n"
    )
    return True


def recv_exact(conn: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            raise ConnectionError
        buf += chunk
    return buf


def read_frame(conn: socket.socket):
    b1, b2 = recv_exact(conn, 2)
    opcode = b1 & 0x0F
    masked = b2 & 0x80
    length = b2 & 0x7F
    if length == 126:
        length = struct.unpack(">H", recv_exact(conn, 2))[0]
    elif length == 127:
        length = struct.unpack(">Q", recv_exact(conn, 8))[0]
    mask = recv_exact(conn, 4) if masked else b""
    payload = recv_exact(conn, length)
    if masked:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return opcode, payload


def frame(payload: bytes, opcode: int = 0x1) -> bytes:
    n = len(payload)
    if n < 126:
        head = struct.pack("!BB", 0x80 | opcode, n)
    elif n < 65536:
        head = struct.pack("!BBH", 0x80 | opcode, 126, n)
    else:
        head = struct.pack("!BBQ", 0x80 | opcode, 127, n)
    return head + payload


def send(conn: socket.socket, msg: dict) -> None:
    msg.setdefault("ts", int(time.time() * 1000))
    try:
        conn.sendall(frame(json.dumps(msg).encode()))
    except OSError:
        pass


def broadcast(msg: dict) -> None:
    with lock:
        for c in list(clients):
            send(c, dict(msg))


# ---- state helpers -----------------------------------------------------------
def snapshot(conn=None):
    for msg in ({"type": "info", "stub": True, "backend": "app/stub_events.py"},
                {"type": "reminders", "items": state["reminders"]},
                {"type": "alarms", "items": state["alarms"]},
                {"type": "timers", "items": timers_snapshot()},
                {"type": "chat_history", "items": state["chat"]},
                {"type": "state", "state": "idle"},
                *([home_snapshot()] if house else [])):
        (send(conn, msg) if conn else broadcast(msg))


def handle_command(cmd: dict) -> None:
    t = cmd.get("type")
    if t == "alarm_stop":
        broadcast({"type": "alarm_stopped", "id": cmd.get("id")})
    elif t == "alarm_delete":
        state["alarms"] = [a for a in state["alarms"] if a["id"] != cmd.get("id")]
        broadcast({"type": "alarms", "items": state["alarms"]})
    elif t == "alarm_toggle":
        for a in state["alarms"]:
            if a["id"] == cmd.get("id"):
                a["enabled"] = bool(cmd.get("enabled", not a["enabled"]))
        broadcast({"type": "alarms", "items": state["alarms"]})
    elif t == "reminder_done":
        for r in state["reminders"]:
            if r["id"] == cmd.get("id"):
                r["done"] = True
        broadcast({"type": "reminders", "items": state["reminders"]})
    elif t == "reminder_delete":
        state["reminders"] = [r for r in state["reminders"] if r["id"] != cmd.get("id")]
        broadcast({"type": "reminders", "items": state["reminders"]})
    elif t == "refresh":
        snapshot()
    elif t == "mute":
        state["muted"] = bool(cmd.get("muted"))
    elif t == "chat_clear":
        state["chat"] = []
        broadcast({"type": "chat_history", "items": []})
    elif t == "net_check":
        broadcast(fake_net())
    elif t == "interrupt":
        state["interrupt"] = True
        broadcast({"type": "state", "state": "listening"})
    elif t in ("timer_stop", "timer_cancel"):
        for tm in state["timers"]:
            if tm["id"] == cmd.get("id"):
                tm["state"] = "done" if t == "timer_stop" else "cancelled"
                broadcast({"type": "timer_stopped" if t == "timer_stop" else "timer_cancelled", "id": tm["id"]})
        broadcast({"type": "timers", "items": timers_snapshot()})
    elif house and t in ("light_set", "thermostat_set", "media", "phone"):
        house_command(cmd)
    elif t == "home_refresh":
        push_home()


def client_loop(conn: socket.socket) -> None:
    if not handshake(conn):
        conn.close()
        return
    with lock:
        clients.append(conn)
    snapshot(conn)
    try:
        while True:
            opcode, payload = read_frame(conn)
            if opcode == 0x8:
                break
            if opcode == 0x9:
                conn.sendall(frame(payload, 0xA))
            elif opcode == 0x1:
                try:
                    handle_command(json.loads(payload.decode()))
                except json.JSONDecodeError:
                    pass
    except (ConnectionError, OSError):
        pass
    finally:
        with lock:
            if conn in clients:
                clients.remove(conn)
        conn.close()


# ---- scripted conversation ---------------------------------------------------
def say(text: str, seconds: float) -> None:
    """Stream assistant text word by word, stop early on interrupt."""
    state["interrupt"] = False
    broadcast({"type": "state", "state": "speaking"})
    state["talking"] = "bot"
    words = text.split()
    cid = None
    for i in range(1, len(words) + 1):
        if state["interrupt"]:
            broadcast({"type": "assistant_text", "text": " ".join(words[:i]) + " —", "final": True})
            chat("assistant", " ".join(words[:i]), cid, interrupted=True)
            state["talking"] = None
            return
        broadcast({"type": "assistant_text", "text": " ".join(words[:i]), "final": i == len(words)})
        cid = chat("assistant", " ".join(words[:i]), cid, interrupted=False)
        time.sleep(seconds / len(words))
    state["talking"] = None
    broadcast({"type": "state", "state": "idle"})


def hear(text: str) -> None:
    broadcast({"type": "state", "state": "listening"})
    state["talking"] = "user"
    words = text.split()
    for i in range(1, len(words) + 1):
        broadcast({"type": "transcript", "text": " ".join(words[:i]), "final": i == len(words)})
        time.sleep(0.18)
    state["talking"] = None
    chat("user", text)
    broadcast({"type": "state", "state": "thinking"})
    time.sleep(0.6)


def fake_net() -> dict:
    dg, llm = random.gauss(180, 30), random.gauss(90, 20)
    return {"type": "net", "online": True, "handshakes": [], "targets": [
        {"name": "deepgram", "label": "Deepgram (ASR + TTS)", "host": "api.deepgram.com", "state": "fair" if dg > 150 else "good",
         "rtt_ms": round(dg, 1), "dns_ms": 4.0, "tcp_ms": round(dg, 1), "tls_ms": round(dg * 1.4, 1), "error": None, "age_s": 0},
        {"name": "llm", "label": "LLM (openrouter.ai)", "host": "openrouter.ai", "state": "good",
         "rtt_ms": round(llm, 1), "dns_ms": 3.0, "tcp_ms": round(llm, 1), "tls_ms": round(llm * 1.3, 1), "error": None, "age_s": 0}]}


def meters() -> None:
    """Fake mic / speaker levels at 10 Hz, louder while someone talks in the script."""
    n = 0
    while True:
        time.sleep(0.1)
        n += 1
        mic = random.gauss(-22, 4) if state["talking"] == "user" else random.gauss(-58, 2)
        out = random.gauss(-16, 4) if state["talking"] == "bot" else -90.0
        broadcast({"type": "levels", "mic_db": round(mic, 1), "mic_peak_db": round(mic + 8, 1), "out_db": round(out, 1)})
        if n % 150 == 1:
            broadcast(fake_net())
        if n % 10 == 0:  # once a second, like the scheduler: timers that rang, devices that moved on their own
            before = [t["state"] for t in state["timers"]]
            items = timers_snapshot()
            if [t["state"] for t in state["timers"]] != before:
                broadcast({"type": "timers", "items": items})
            if house and any(d.tick() for d in house.values()):
                push_home()


def script() -> None:
    n = 1
    while True:
        time.sleep(8)
        # 1) time
        hear("what time is it")
        broadcast({"type": "tool_call", "name": "get_current_time", "args": {}})
        now = datetime.now()
        broadcast({"type": "tool_result", "name": "get_current_time", "result": now.isoformat(timespec="seconds")})
        broadcast({"type": "latency", "stage": "voice_to_voice", "ms": 840})
        say(f"It is {now.strftime('%I:%M %p').lstrip('0')}.", 1.5)
        time.sleep(6)
        # 2) reminder
        hear("remind me to call the lab in twenty minutes")
        rid = f"r{n + 1}"
        due = (datetime.now() + timedelta(minutes=20)).isoformat(timespec="minutes")
        broadcast({"type": "tool_call", "name": "create_reminder", "args": {"text": "Call the lab", "due_at": due}})
        state["reminders"].append({"id": rid, "text": "Call the lab", "due_at": due, "done": False})
        broadcast({"type": "reminder_created", "id": rid, "text": "Call the lab", "due_at": due})
        broadcast({"type": "reminders", "items": state["reminders"]})
        say("Okay, I will remind you to call the lab in twenty minutes.", 2.5)
        time.sleep(6)
        # 3) alarm
        hear("set an alarm for six thirty tomorrow")
        aid = f"a{n + 1}"
        broadcast({"type": "tool_call", "name": "create_alarm", "args": {"label": "Wake up", "time": "06:30"}})
        state["alarms"].append({"id": aid, "label": "Wake up", "time": "06:30", "enabled": True})
        broadcast({"type": "alarm_created", "id": aid, "label": "Wake up", "time": "06:30"})
        broadcast({"type": "alarms", "items": state["alarms"]})
        say("Alarm set for six thirty tomorrow morning.", 2.0)
        time.sleep(8)
        # 4) a reminder comes due, then an alarm fires (rings until Stop is pressed)
        broadcast({"type": "reminder_due", "id": rid, "text": "Call the lab"})
        say("Reminder: call the lab.", 1.5)
        time.sleep(6)
        broadcast({"type": "alarm_fired", "id": aid, "label": "Wake up"})
        n += 1
        time.sleep(15)
        if house:
            house_script(n)


def tool(name: str, args: dict, result: dict, secs: float = 0.5) -> None:
    """One tool call as the pipeline shows it: the widget animates "executing" until the result."""
    broadcast({"type": "tool_call", "name": name, "args": args, "id": f"c{int(time.time() * 1000)}"})
    time.sleep(secs)
    broadcast({"type": "tool_result", "name": name, "result": result, "id": f"c{int(time.time() * 1000)}"})


def house_script(n: int) -> None:
    """Music, lights, thermostat, a timer, a message, a call and the weather, through the widgets."""
    hear("play some jazz")
    tool("control_music", {"action": "play", "query": "jazz"}, house["media"].play("jazz")); push_home()
    st = house["media"].status()["track"]
    say(f"Playing jazz: {st['title']} by {st['artist']}.", 2.2)
    time.sleep(5)
    hear("turn on the living room light and make it blue")
    tool("set_light", {"room": "living room", "on": True, "color": "blue"}, house["lights"].set("living room", on=True, color="blue")); push_home()
    say("Living room light on, blue.", 1.5)
    time.sleep(4)
    hear("dim it to thirty percent")
    tool("set_light", {"room": "living room", "brightness": 30}, house["lights"].set("living room", brightness=30)); push_home()
    say("Dimmed to thirty percent.", 1.3)
    time.sleep(4)
    hear("set the temperature to twenty degrees")
    tool("set_thermostat", {"target": 20}, house["thermostat"].set(target=20)); push_home()
    say("Thermostat set to twenty, cooling.", 1.5)
    time.sleep(4)
    hear("set a timer for one minute")
    tid = f"t{n}"
    state["timers"].append({"id": tid, "label": "", "duration_s": 60, "ends_at": (datetime.now() + timedelta(seconds=60)).isoformat(timespec="seconds"), "ends_at_s": time.time() + 60, "state": "running", "fired_at": None})
    tool("create_timer", {"minutes": 1}, {"created": "timer", "id": tid, "duration_s": 60})
    broadcast({"type": "timer_created", "id": tid, "label": "", "duration_s": 60})
    broadcast({"type": "timers", "items": timers_snapshot()})
    say("One minute timer started.", 1.3)
    time.sleep(4)
    hear("text Alex that I'm running ten minutes late")
    tool("send_message", {"contact": "Alex", "text": "I'm running ten minutes late"}, house["phone"].send_message("Alex", "I'm running ten minutes late")); push_home()
    say("Sent to Alex.", 1.0)
    time.sleep(4)
    hear("what's the weather in Berlin")
    state["weather"] = FAKE_WEATHER
    tool("get_weather", {"city": "Berlin"}, FAKE_WEATHER, 0.9)
    broadcast({"type": "weather", **FAKE_WEATHER})
    say(FAKE_WEATHER["spoken"], 2.5)
    time.sleep(4)
    hear("call mom")
    tool("make_call", {"contact": "Mom"}, house["phone"].call("Mom")); push_home()
    say("Calling Mom.", 1.0)
    time.sleep(10)
    hear("hang up")
    tool("end_call", {}, house["phone"].hang_up()); push_home()
    say("Call ended.", 1.0)
    time.sleep(4)
    hear("pause the music")
    tool("control_music", {"action": "pause"}, house["media"].pause()); push_home()
    say("Paused.", 0.8)
    time.sleep(30)


def main() -> None:
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", PORT))
    srv.listen()
    print(f"[stub-events] ws://127.0.0.1:{PORT}", flush=True)
    threading.Thread(target=script, daemon=True).start()
    threading.Thread(target=meters, daemon=True).start()
    while True:
        conn, _ = srv.accept()
        threading.Thread(target=client_loop, args=(conn,), daemon=True).start()


if __name__ == "__main__":
    main()
