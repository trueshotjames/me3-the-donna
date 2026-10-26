"""Timers, the simulated house and the scheduler's widget events (no network, no API keys).

    .venv\\Scripts\\python -m pytest tests -q
"""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from mcp_servers.tools.db import Store
from mcp_servers.tools.devices import Devices, build_devices
from mcp_servers.tools.devices.base import DeviceError
from mcp_servers.tools.devices import sim
from mcp_servers.tools.devices.sim import SimLights, SimMediaPlayer, SimPhone, SimThermostat, parse_color


class Bus:
    def __init__(self):
        self.sent = []

    def emit(self, type_, **fields):
        self.sent.append({"type": type_, **fields})

    def types(self):
        return [m["type"] for m in self.sent]


# ------------------------------------------------------------------ store: timers and device state


def test_timers_ring_then_stop(tmp_path):
    store = Store(tmp_path / "t.db")
    t = store.create_timer(1, "tea")
    assert t["state"] == "running" and t["remaining_s"] in (0, 1) and t["label"] == "tea"
    assert store.due_timers() == []
    time.sleep(1.1)
    due = store.due_timers()
    assert [d["id"] for d in due] == [t["id"]] and due[0]["ringing"] is True
    assert store.due_timers() == []  # only reported once
    assert [r["id"] for r in store.ringing_timers()] == [t["id"]]
    stopped = store.stop_timer(None)
    assert stopped[0]["state"] == "done" and store.list_timers() == []
    assert store.list_timers(include_finished=True)[0]["state"] == "done"


def test_cancel_timer(tmp_path):
    store = Store(tmp_path / "t.db")
    a, b = store.create_timer(600, ""), store.create_timer(60, "")
    assert [x["id"] for x in store.list_timers()] == [b["id"], a["id"]]  # soonest first
    assert [x["id"] for x in store.cancel_timer(a["id"])] == [a["id"]]
    assert store.cancel_timer(a["id"]) == []
    assert [x["id"] for x in store.list_timers()] == [b["id"]]


def test_device_state_round_trip(tmp_path):
    store = Store(tmp_path / "t.db")
    assert store.get_state("lights") is None
    store.set_state("lights", {"kitchen": {"on": True}})
    store.set_state("lights", {"kitchen": {"on": False, "brightness": 40}})
    assert store.get_state("lights") == {"kitchen": {"on": False, "brightness": 40}}


# ------------------------------------------------------------------ simulated devices


def test_lights_match_rooms_and_persist():
    saved = {}
    lights = SimLights(["living room", "bedroom"], persist=lambda n, s: saved.update({n: json.loads(json.dumps(s))}))
    out = lights.set("the bedroom light", on=True)
    assert out["lights"][0]["room"] == "bedroom" and out["lights"][0]["on"] and out["transition_ms"] > 0
    out = lights.set("all", brightness=25, color="blue")
    assert [l["brightness"] for l in out["lights"]] == [25, 25] and out["lights"][0]["color_name"] == "blue"
    out = lights.set("living", color="warm white")  # a colour alone turns the light on
    assert out["lights"][0]["on"] is True
    with pytest.raises(DeviceError):
        lights.set("garage", on=True)
    with pytest.raises(DeviceError):
        lights.set("bedroom")
    with pytest.raises(DeviceError):
        lights.set("bedroom", color="plaid")
    # a restart finds the house as it was
    again = SimLights(["living room", "bedroom"], load=lambda n: saved.get(n))
    assert again.status()["lights"][1]["brightness"] == 25 and again.status()["lights"][1]["color_name"] == "blue"


def test_parse_color():
    assert parse_color("#F80") == ("#ff8800", "#ff8800")
    assert parse_color("light blue")[0] == "blue"
    assert parse_color(None) is None


def test_thermostat_moves_toward_target(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(sim, "_now", lambda: clock[0])
    th = SimThermostat()
    assert th.status()["current"] == 24.0
    st = th.set(target=20)
    assert st["activity"] == "cooling" and st["mode"] == "auto"
    clock[0] += 20
    assert th.tick() is True
    assert th.status()["current"] == 23.0  # 0.05 degrees a second
    clock[0] += 600
    th.tick()
    st = th.status()
    assert st["current"] == 20.0 and st["activity"] == "idle"
    st = th.set(mode="off")
    assert st["activity"] == "off"
    with pytest.raises(DeviceError):
        th.set(target=50)
    with pytest.raises(DeviceError):
        th.set(mode="party")
    f = SimThermostat(unit="F")
    assert f.set(target=68)["target"] == 68.0 and f._s["target"] == 20.0


def test_media_player_plays_queue(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(sim, "_now", lambda: clock[0])
    m = SimMediaPlayer()
    st = m.play("some jazz music")
    assert st["state"] == "playing" and st["track"]["genre"] == "jazz" and st["source"] == "jazz" and st["queue_length"] == 2
    clock[0] += 30
    assert m.status()["position_s"] == 30.0
    st = m.pause()
    clock[0] += 30
    assert m.status()["position_s"] == 30.0 and st["state"] == "paused"
    assert m.play()["state"] == "playing"  # bare "play" resumes
    clock[0] += 1000  # past the end of the first track: the next one starts on its own
    assert m.tick() is True and m.status()["queue_index"] == 1
    clock[0] += 1000
    m.tick()
    assert m.status()["state"] == "stopped"  # end of the queue, repeat off
    m.set_repeat("all")
    m.resume()
    clock[0] += 1000
    m.tick()
    assert m.status()["state"] == "playing"
    assert m.volume_step(10)["volume"] == 70 and m.set_volume(150)["volume"] == 100
    assert m.set_mute(True)["muted"] is True and m.set_volume(40)["muted"] is False
    assert m.play("focus playlist")["source"] == "focus playlist"
    assert m.play("Pixel Pop")["track"]["artist"] == "Pixel Pop"
    with pytest.raises(DeviceError):
        m.play("polka")
    with pytest.raises(DeviceError):
        m.set_repeat("sometimes")


def test_phone_call_and_message(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(sim, "_now", lambda: clock[0])
    ph = SimPhone()
    st = ph.call("my mom")
    assert st["call"]["contact"] == "Mom" and st["call"]["state"] == "ringing"
    with pytest.raises(DeviceError):
        ph.call("Dad")  # already on a call
    clock[0] += sim.RING_SECS
    assert ph.tick() is True and ph.status()["call"]["state"] == "connected"
    clock[0] += 12
    assert ph.status()["call"]["duration_s"] == 12
    assert ph.hang_up()["call"]["duration_s"] == 12
    with pytest.raises(DeviceError):
        ph.hang_up()
    msg = ph.send_message("alex", "running late")["message"]
    assert msg["contact"] == "Alex" and msg["state"] == "sent"
    clock[0] += sim.DELIVER_SECS
    assert ph.tick() is True and ph.status()["messages"][-1]["state"] == "delivered"
    with pytest.raises(DeviceError):
        ph.send_message("nobody", "hi")
    with pytest.raises(DeviceError):
        ph.send_message("Sam", "  ")


def test_build_devices_defaults_to_simulation(monkeypatch):
    monkeypatch.setenv("LIGHT_BACKEND", "gpio")  # no gpiozero here: falls back with a warning
    monkeypatch.setenv("LIGHT_ROOMS", "studio, hall")
    monkeypatch.setenv("PHONE_CONTACTS", "Ana,Ben")
    devices = build_devices(lambda n: None, lambda n, s: None)
    assert isinstance(devices, Devices)
    st = devices.status()
    assert st["backends"] == {"lights": "sim", "thermostat": "sim", "media": "sim", "phone": "sim"}
    assert st["rooms"] == ["studio", "hall"] and st["phone"]["contacts"] == ["Ana", "Ben"]
    v = devices.version
    devices.lights.set("hall", on=True)
    devices.bump()
    assert devices.version == v + 1


# ------------------------------------------------------------------ the tools server


def test_server_tools_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setenv("ASSISTANT_DB", "test_home.db")
    import importlib

    import mcp_servers.common as common

    monkeypatch.setattr(common, "DATA_DIR", tmp_path)
    import mcp_servers.tools.server as server

    server = importlib.reload(server)
    j = lambda s: json.loads(s)  # noqa: E731
    assert j(server.control_music("play", "lo-fi"))["track"]["genre"] == "lo-fi"
    assert j(server.control_music("louder"))["volume"] == 70
    assert j(server.control_music("dance"))["error"]
    assert j(server.set_light("kitchen", on=True, brightness=30))["lights"][0]["brightness"] == 30
    assert j(server.set_light("attic", on=True))["error"]
    assert j(server.set_thermostat(target=21, mode="heat"))["activity"] in ("idle", "heating", "cooling")
    assert j(server.make_call("Dr. Reyes"))["call"]["state"] == "ringing"
    assert j(server.end_call())["call"]["state"] == "ended"
    assert j(server.send_message("work", "on my way"))["message"]["contact"] == "Work"
    assert j(server.create_timer(minutes=0))["error"]
    t = j(server.create_timer(seconds=1, label="egg"))
    assert t["created"] == "timer" and t["spoken_duration"] == "1 second"
    time.sleep(1.1)
    due = j(server.poll_due())
    assert [x["id"] for x in due["timers"]] == [t["id"]] and due["home_version"] > 1
    stopped = j(server.stop_alarm())
    assert [x["id"] for x in stopped["timers"]] == [t["id"]]
    status = j(server.home_status())
    assert status["media"]["state"] == "playing" and status["timers"] == []
    snap = j(server.home_snapshot())
    assert snap["weather"] is None and snap["lights"][2]["on"] is True
    assert (tmp_path / "tool_exec.jsonl").exists()
    assert set(server.LLM_TOOLS) >= {"control_music", "set_light", "set_thermostat", "home_status", "send_message", "make_call", "end_call", "create_timer", "cancel_timer", "get_weather"}


# ------------------------------------------------------------------ scheduler: widget events


class FakeTools:
    """Stands in for the MCP bridge: answers the tools the scheduler calls."""

    connected = True

    def __init__(self):
        self.calls = []
        self.version = 1
        self.due = {"reminders": [], "alarms": [], "ringing": [], "timers": [], "ringing_timers": [], "home_version": 1}

    async def call_json(self, tool, args=None, *, timeout=30):
        self.calls.append((tool, args or {}))
        if tool == "poll_due":
            return dict(self.due, home_version=self.version)
        if tool == "snapshot":
            return {"reminders": [], "alarms": [], "timers": [{"id": 3, "state": "running"}]}
        if tool == "home_snapshot":
            return {"version": self.version, "lights": [], "thermostat": {}, "media": {}, "phone": {}, "backends": {}, "weather": None}
        if tool == "stop_timer":
            return {"timers": [{"id": args.get("id")}]}
        if tool == "set_light":
            return {"error": "no light called 'attic'"} if args.get("room") == "attic" else {"lights": []}
        if tool == "control_music":
            return {"state": "playing"}
        if tool == "end_call":
            return {"call": {"state": "ended"}}
        return {}


def make_scheduler(tools):
    from assistant.scheduler import Scheduler

    bus = Bus()
    sch = Scheduler(tools, bus)
    spoken = []

    async def say(text, **kw):
        spoken.append((text, kw))

    sch.say = say
    return sch, bus, spoken


async def test_scheduler_rings_timers_and_pushes_home():
    tools = FakeTools()
    sch, bus, spoken = make_scheduler(tools)
    await sch._tick()
    assert bus.types() == ["home"]  # nothing due; the house version is new
    tools.due["timers"] = [{"id": 3, "label": "", "duration_s": 300}]
    await sch._tick()
    assert "timer_fired" in bus.types()
    assert spoken[-1][0] == "Your 5 minutes timer is done. Say stop to turn it off." and spoken[-1][1]["interruptible"] is False
    bus.sent.clear()
    tools.due["timers"] = []
    await sch._tick()
    assert "home" not in bus.types()  # unchanged version: nothing pushed
    tools.version = 2
    await sch._tick()
    assert bus.types() == ["home"]


async def test_scheduler_tool_results_become_events():
    tools = FakeTools()
    sch, bus, _ = make_scheduler(tools)
    await sch.on_tool_result("create_timer", {}, {"created": "timer", "id": 3, "duration_s": 60, "ends_at": "x"})
    assert bus.types()[0] == "timer_created" and "timers" in bus.types()
    bus.sent.clear()
    await sch.on_tool_result("set_light", {"room": "kitchen"}, {"lights": []})
    assert bus.types() == ["home"]
    bus.sent.clear()
    await sch.on_tool_result("get_weather", {}, {"city": "Berlin, Germany", "now": {}, "days": [], "spoken": "..."})
    assert bus.sent[0]["type"] == "weather" and bus.sent[0]["city"] == "Berlin, Germany"
    bus.sent.clear()
    await sch.on_tool_result("stop_alarm", {}, {"alarms": [], "timers": [{"id": 3}]})
    assert bus.types()[0] == "timer_stopped"
    bus.sent.clear()
    await sch.on_tool_result("set_light", {}, {"error": "no light called 'attic'"})
    assert bus.sent == []  # errors are shown through tool_result, not as a widget change


async def test_scheduler_widget_commands():
    tools = FakeTools()
    sch, bus, _ = make_scheduler(tools)
    await sch.handle_command({"type": "light_set", "room": "kitchen", "on": True})
    assert tools.calls[-2] == ("set_light", {"room": "kitchen", "on": True}) and bus.types()[-1] == "home"
    await sch.handle_command({"type": "light_set", "room": "attic", "on": True})
    assert any(m["type"] == "warning" and m["kind"] == "device" for m in bus.sent)
    await sch.handle_command({"type": "media", "action": "set_volume", "value": 30})
    assert tools.calls[-2] == ("control_music", {"action": "set_volume", "value": 30})
    await sch.handle_command({"type": "phone", "action": "hang_up"})
    assert tools.calls[-2][0] == "end_call"
    bus.sent.clear()
    await sch.handle_command({"type": "timer_stop", "id": 3})
    assert bus.types()[0] == "timer_stopped" and tools.calls[-3] == ("stop_timer", {"id": 3})


def test_prompt_mentions_the_house_tools():
    from assistant.prompts import system_prompt

    p = system_prompt("Pi", "King", 8, delegate=True)
    for name in ("control_music", "set_light", "set_thermostat", "home_status", "create_timer", "get_weather", "send_message", "make_call", "end_call"):
        assert name in p
    assert "Never delegate the time, the weather" in p
