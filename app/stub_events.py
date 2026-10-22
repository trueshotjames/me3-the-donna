"""
Stub event backend for the UI, used until assistant/ publishes real events.

Serves a WebSocket on ws://127.0.0.1:8765 using only the standard library,
plays a scripted conversation on a loop, and honours the UI commands
(alarm_stop, alarm_delete, reminder_done, reminder_delete, mute, interrupt).
Message shape: one JSON object per frame, see app/UI_EVENTS.md.

Run:  python app/stub_events.py [port]
"""
from __future__ import annotations

import base64
import hashlib
import json
import socket
import struct
import sys
import threading
import time
from datetime import datetime, timedelta

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

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
}


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
                {"type": "state", "state": "idle"}):
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
    elif t == "interrupt":
        state["interrupt"] = True
        broadcast({"type": "state", "state": "listening"})


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
    words = text.split()
    for i in range(1, len(words) + 1):
        if state["interrupt"]:
            broadcast({"type": "assistant_text", "text": " ".join(words[:i]) + " —", "final": True})
            return
        broadcast({"type": "assistant_text", "text": " ".join(words[:i]), "final": i == len(words)})
        time.sleep(seconds / len(words))
    broadcast({"type": "state", "state": "idle"})


def hear(text: str) -> None:
    broadcast({"type": "state", "state": "listening"})
    words = text.split()
    for i in range(1, len(words) + 1):
        broadcast({"type": "transcript", "text": " ".join(words[:i]), "final": i == len(words)})
        time.sleep(0.18)
    broadcast({"type": "state", "state": "thinking"})
    time.sleep(0.6)


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


def main() -> None:
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", PORT))
    srv.listen()
    print(f"[stub-events] ws://127.0.0.1:{PORT}", flush=True)
    threading.Thread(target=script, daemon=True).start()
    while True:
        conn, _ = srv.accept()
        threading.Thread(target=client_loop, args=(conn,), daemon=True).start()


if __name__ == "__main__":
    main()
