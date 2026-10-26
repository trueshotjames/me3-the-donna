"""Level meter, chat log and network monitor (UI extras); no pipeline, no internet."""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import numpy as np
from pipecat.frames.frames import InputAudioRawFrame, OutputAudioRawFrame

from assistant.chat_log import ChatLog
from assistant.levels import FLOOR_DB, LevelMeter, dbfs
from assistant.netstatus import NetMonitor, Target


class Bus:
    def __init__(self, clients=1):
        self.clients = clients
        self.sent = []

    def emit(self, type_, **fields):
        self.sent.append({"type": type_, **fields})


def pcm(amplitude: float, n: int = 320) -> bytes:
    t = np.arange(n) / 16000
    return (np.sin(2 * np.pi * 440 * t) * amplitude * 32767).astype(np.int16).tobytes()


def test_dbfs():
    assert dbfs(b"") == (FLOOR_DB, FLOOR_DB)
    assert dbfs(b"\x00" * 640) == (FLOOR_DB, FLOOR_DB)
    rms, peak = dbfs(pcm(1.0))
    assert -3.5 < rms < -2.5  # full-scale sine: -3 dBFS RMS
    assert -0.5 < peak <= 0.0
    rms_half, _ = dbfs(pcm(0.5))
    assert abs((rms - rms_half) - 6.0) < 0.3


async def test_level_meter_keeps_loudest_and_ignores_tts_bursts():
    bus = Bus()
    meter = LevelMeter(bus)
    from pipecat.transports.base_output import BaseOutputTransport

    out_transport = BaseOutputTransport.__new__(BaseOutputTransport)
    for amp in (0.1, 0.5, 0.2):
        await meter.on_push_frame(SimpleNamespace(frame=InputAudioRawFrame(pcm(amp), 16000, 1), source=None))
    # from the TTS (not yet played): ignored; from the output transport (played): counted
    await meter.on_push_frame(SimpleNamespace(frame=OutputAudioRawFrame(pcm(1.0), 16000, 1), source=object()))
    m = meter.take()
    assert m["out_db"] == FLOOR_DB
    assert abs(m["mic_db"] - dbfs(pcm(0.5))[0]) < 0.11
    await meter.on_push_frame(SimpleNamespace(frame=OutputAudioRawFrame(pcm(0.25), 16000, 1), source=out_transport))
    m = meter.take()
    assert m["mic_db"] == FLOOR_DB and m["out_db"] > -20


async def test_level_meter_idle_without_clients():
    bus = Bus(clients=0)
    meter = LevelMeter(bus)
    await meter.on_push_frame(SimpleNamespace(frame=InputAudioRawFrame(pcm(0.5), 16000, 1), source=None))
    assert meter.take()["mic_db"] == FLOOR_DB


def test_chat_log_groups_a_reply():
    bus = Bus()
    chat = ChatLog(bus, gap_secs=0.05)
    chat.user("what time is it")
    chat.assistant("It is five.")
    chat.assistant("Anything else?")
    chat.tool("get_current_time", True)
    chat.user("set an alarm")
    chat.assistant("Setting it")
    chat.interrupted()
    chat.assistant("Okay.")
    items = chat.items
    assert [i["role"] for i in items] == ["user", "assistant", "tool", "user", "assistant", "assistant"]
    assert items[1]["text"] == "It is five. Anything else?"
    assert items[4]["interrupted"] is True and items[5]["interrupted"] is False
    # an update re-sends the same id
    ids = [e["item"]["id"] for e in bus.sent if e["type"] == "chat"]
    assert ids.count(items[1]["id"]) == 2


def test_chat_log_new_bubble_after_a_pause():
    chat = ChatLog(Bus(), gap_secs=0.01)
    chat.assistant("Reply one.")
    chat.bot_stopped()
    time.sleep(0.03)
    chat.assistant("Reminder: water the plants.")
    assert len(chat.items) == 2


async def test_chat_log_snapshot_and_clear():
    bus = Bus()
    chat = ChatLog(bus, max_items=3)
    for i in range(5):
        chat.user(f"u{i}")
    snap = await chat.snapshot()
    assert [i["text"] for i in snap[0]["items"]] == ["u2", "u3", "u4"]
    await chat.handle_command({"type": "chat_clear"})
    assert chat.items == [] and bus.sent[-1] == {"type": "chat_history", "items": []}


async def test_net_probe_local_and_down():
    server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    bus = Bus()
    mon = NetMonitor(bus, [Target("llm", "local", "127.0.0.1", 80), Target("deepgram", "closed", "127.0.0.1", 1)], timeout_secs=1)
    mon.targets[0].port = port
    # port != 80 would try TLS against a plain server: keep the TCP part only
    mon._ssl = None
    msg = await mon.check_now()
    server.close()
    rows = {r["name"]: r for r in msg["targets"]}
    assert rows["llm"]["tcp_ms"] is not None and rows["llm"]["state"] in ("good", "fair", "poor")
    assert rows["deepgram"]["state"] == "down" and rows["deepgram"]["error"]
    assert msg["online"] is True
    assert bus.sent[-1]["type"] == "net"


def test_net_turn_block(tmp_path):
    events = tmp_path / "net_events.jsonl"
    events.write_text("")
    bus = Bus()
    mon = NetMonitor(bus, [Target("deepgram", "Deepgram", "x"), Target("llm", "LLM", "y")], events_path=events)
    now = time.time()
    for name, tcp in (("deepgram", 100.0), ("llm", 50.0)):
        mon.history[name].append({"t": now, "ok": True, "dns_ms": 5.0, "tcp_ms": tcp, "tls_ms": 120.0, "error": None})
    with open(events, "a") as fh:
        fh.write(json.dumps({"t": now - 1.0, "provider": "Flux", "ms": 6800.0, "ok": True, "reconnect": True}) + "\n")
        fh.write('{"t": ')  # half-written line: left for later
    turn = SimpleNamespace(
        speech_start=now - 5, created=now - 6, bot_started=now, eot=now - 1.5, tts=[{}],
        llm_calls=[{"t_req": now - 1.4, "t_end": now - 0.5}, {"t_req": now - 3, "t_end": now - 2.9}],
    )
    record = {"llm": {"calls": [{"kind": "normal"}, {"kind": "early-discarded"}]}, "end_to_end": {"total_ms": 9000.0}}
    block = mon.turn_block(turn, record)
    assert block["handshakes"][0]["ms"] == 6800.0 and block["handshake_ms"] == 6800.0
    est = block["estimate_ms"]
    assert est["asr_ms"] == 100.0 and est["tts_ms"] == 100.0 and est["llm_ms"] == 50.0
    # the kept call came 1.5 s after the discarded one: connection still alive
    assert est["llm_new_connection_ms"] is None
    assert block["total_ms"] == 7050.0
    assert block["share_of_e2e_pct"] == 78.3
    assert "network" in NetMonitor.format_line(block)
    assert any(e["type"] == "warning" and e["kind"] == "slow_connect" for e in bus.sent)


def test_net_turn_block_counts_a_new_llm_connection():
    mon = NetMonitor(None, [Target("deepgram", "Deepgram", "x"), Target("llm", "LLM", "y")])
    now = time.time()
    mon.history["llm"].append({"t": now, "ok": True, "dns_ms": 10.0, "tcp_ms": 60.0, "tls_ms": 130.0, "error": None})
    mon._last_llm_end = now - 30  # previous reply long ago: idle connection was dropped
    turn = SimpleNamespace(speech_start=now - 3, created=now - 3, bot_started=now, eot=now - 1, tts=[],
                           llm_calls=[{"t_req": now - 0.9, "t_end": now - 0.2}])
    block = mon.turn_block(turn, {"llm": {"calls": [{"kind": "normal"}]}})
    assert block["estimate_ms"]["llm_new_connection_ms"] == 200.0
    assert block["estimate_ms"]["asr_ms"] is None  # no Deepgram probe yet
