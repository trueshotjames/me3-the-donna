"""Internet status for the UI, and the network share of each turn for data/turns.jsonl.

Probe: every ``every_secs`` (5 s while a host is down) each cloud host gets DNS, a TCP connect
and a TLS handshake, timed separately, then the socket is closed. No request is sent, so it
costs one small handshake per host, which the Pi does not notice. TCP connect time is one
network round trip (RTT), measured to the millisecond.

Handshakes: the ASR and TTS MCP servers append each Deepgram connect (and failed connect) to
data/net_events.jsonl (mcp_servers/common.py ``log_net_event``). Those times are exact.

Per turn (``turn_block``), what can and cannot be known:
  measured  provider connects that overlapped the turn (Flux, Aura), and the latest RTTs
  estimated network time already inside the streaming stages: the providers do not report how
            much of their time-to-first-byte was their own work. Each stage on the reply path
            needs about one round trip: ASR (your last audio up, end of turn down), each LLM
            call kept for the reply (request up, first token down), TTS (sentence up, first
            audio down). An LLM call that comes more than ``LLM_KEEPALIVE_S`` after the
            previous one opens a new HTTPS connection (httpx drops idle connections after 5 s),
            so it also pays DNS + TCP + TLS, estimated from the last probe of that host.
"""

from __future__ import annotations

import asyncio
import json
import os
import ssl
import statistics
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from loguru import logger

if TYPE_CHECKING:
    from assistant.ui_bus import UIBus

RTT_GOOD_MS = 150.0
RTT_FAIR_MS = 400.0
LLM_KEEPALIVE_S = 5.0  # httpx default keepalive_expiry (openai SDK client)


@dataclass
class Target:
    name: str  # short key: "deepgram", "llm"
    label: str  # shown in the UI
    host: str
    port: int = 443


def targets_from(cfg) -> list[Target]:
    """Deepgram (ASR + TTS) and the fast LLM endpoint, from the same settings the services use."""
    dg = urlparse(os.getenv("DEEPGRAM_FLUX_URL") or "wss://api.deepgram.com/v2/listen")
    llm = urlparse(cfg.llm_base_url)
    out = [Target("deepgram", "Deepgram (ASR + TTS)", dg.hostname or "api.deepgram.com", dg.port or 443)]
    if llm.hostname:
        out.append(Target("llm", f"LLM ({llm.hostname})", llm.hostname, llm.port or (443 if llm.scheme != "http" else 80)))
    return out


def _ms(a: float, b: float) -> float:
    return round((b - a) * 1000, 1)


class NetMonitor:
    def __init__(
        self,
        bus: UIBus | None,
        targets: list[Target],
        *,
        every_secs: float = 15.0,
        retry_secs: float = 5.0,
        timeout_secs: float = 5.0,
        events_path: Path | None = None,
        history: int = 20,
    ):
        self._bus = bus
        self.targets = targets
        self._every = every_secs
        self._retry = retry_secs
        self._timeout = timeout_secs
        self._events_path = events_path
        self._events_pos = 0
        self.history: dict[str, deque[dict]] = {t.name: deque(maxlen=history) for t in targets}
        self.handshakes: deque[dict] = deque(maxlen=100)
        self._task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._ssl = ssl.create_default_context()
        self._last_llm_end: float | None = None
        if events_path is not None and events_path.exists():
            self._events_pos = events_path.stat().st_size  # only this session's connects

    # ------------------------------------------------------------------ probing

    async def probe(self, t: Target) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        r: dict[str, Any] = {"t": time.time(), "ok": False, "dns_ms": None, "tcp_ms": None, "tls_ms": None, "error": None}
        transport = None
        try:
            t0 = time.perf_counter()
            infos = await asyncio.wait_for(loop.getaddrinfo(t.host, t.port, type=0, proto=6), self._timeout)
            t1 = time.perf_counter()
            r["dns_ms"] = round((t1 - t0) * 1000, 1)
            family, _, _, _, addr = infos[0]
            transport, protocol = await asyncio.wait_for(
                loop.create_connection(asyncio.Protocol, addr[0], addr[1], family=family), self._timeout
            )
            t2 = time.perf_counter()
            r["tcp_ms"] = round((t2 - t1) * 1000, 1)
            r["ok"] = True  # reachable; TLS below is extra information
            if t.port != 80:
                try:
                    transport = await asyncio.wait_for(
                        loop.start_tls(transport, protocol, self._ssl, server_hostname=t.host), self._timeout
                    )
                    r["tls_ms"] = round((time.perf_counter() - t2) * 1000, 1)
                except Exception as e:  # noqa: BLE001
                    r["error"] = f"TLS: {e.__class__.__name__}"
        except asyncio.TimeoutError:
            r["error"] = f"timeout after {self._timeout:.0f} s"
        except OSError as e:
            r["error"] = e.strerror or e.__class__.__name__
        except Exception as e:  # noqa: BLE001
            r["error"] = repr(e)
        finally:
            if transport is not None:
                transport.abort()
        return r

    async def check_now(self) -> dict[str, Any]:
        results = await asyncio.gather(*(self.probe(t) for t in self.targets))
        for t, r in zip(self.targets, results):
            self.history[t.name].append(r)
            if not r["ok"]:
                logger.warning(f"network: {t.host} unreachable ({r['error']})")
        self._read_events()
        msg = self.status()
        if self._bus is not None:
            self._bus.emit("net", **msg)
        return msg

    def rtt(self, name: str) -> float | None:
        """Median TCP connect time of the last three good probes of ``name``."""
        vals = [r["tcp_ms"] for r in list(self.history.get(name, ()))[-3:] if r["ok"] and r["tcp_ms"] is not None]
        return round(statistics.median(vals), 1) if vals else None

    def _connect_cost(self, name: str) -> float | None:
        """DNS + TCP + TLS of the last good probe: what a fresh HTTPS connection to that host costs."""
        for r in reversed(self.history.get(name, ())):
            if r["ok"]:
                return round(sum(r[k] or 0.0 for k in ("dns_ms", "tcp_ms", "tls_ms")), 1)
        return None

    def status(self) -> dict[str, Any]:
        now = time.time()
        rows = []
        for t in self.targets:
            last = self.history[t.name][-1] if self.history[t.name] else None
            rtt = self.rtt(t.name)
            if last is None:
                state = "unknown"
            elif not last["ok"]:
                state = "down"
            elif rtt is None or rtt < RTT_GOOD_MS:
                state = "good"
            elif rtt < RTT_FAIR_MS:
                state = "fair"
            else:
                state = "poor"
            rows.append(
                {
                    "name": t.name,
                    "label": t.label,
                    "host": t.host,
                    "state": state,
                    "rtt_ms": rtt,
                    "dns_ms": last and last["dns_ms"],
                    "tcp_ms": last and last["tcp_ms"],
                    "tls_ms": last and last["tls_ms"],
                    "error": last and last["error"],
                    "age_s": round(now - last["t"], 1) if last else None,
                }
            )
        recent = [h for h in self.handshakes if now - h.get("t", 0) < 600]
        return {
            "online": any(r["state"] in ("good", "fair", "poor") for r in rows),
            "targets": rows,
            "handshakes": recent[-5:],
        }

    # ------------------------------------------------------- provider connects

    def _read_events(self) -> None:
        """New lines of data/net_events.jsonl (written by the ASR/TTS MCP servers)."""
        p = self._events_path
        if p is None:
            return
        try:
            size = p.stat().st_size
        except OSError:
            return
        if size < self._events_pos:
            self._events_pos = 0  # file was replaced
        if size == self._events_pos:
            return
        try:
            with open(p, "rb") as fh:
                fh.seek(self._events_pos)
                chunk = fh.read()
        except OSError:
            return
        end = chunk.rfind(b"\n") + 1  # keep a half-written last line for next time
        self._events_pos += end
        for line in chunk[:end].splitlines():
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if isinstance(ev, dict):
                self.handshakes.append(ev)
                if ev.get("ms") and ev["ms"] >= 2000 and self._bus is not None:
                    what = "failed after" if not ev.get("ok", True) else "took"
                    self._bus.emit("warning", message=f"Slow network: {ev.get('provider')} connect {what} {ev['ms'] / 1000:.1f} s", kind="slow_connect")

    # ------------------------------------------------------------- per turn

    def turn_block(self, turn, record: dict) -> dict | None:
        """The ``net`` block of a turn record (see the module docstring)."""
        self._read_events()
        t0 = turn.speech_start or turn.created
        t1 = turn.bot_started or time.time()
        handshakes = []
        for h in self.handshakes:
            end = h.get("t")
            if end is None:
                continue
            start = end - (h.get("ms") or 0) / 1000
            if start <= t1 and end >= t0:
                handshakes.append(
                    {"provider": h.get("provider"), "ms": h.get("ms"), "ok": h.get("ok", True), "reconnect": h.get("reconnect"),
                     "before_reply_audio": end <= t1}
                )
        dg, llm = self.rtt("deepgram"), self.rtt("llm")

        kinds = [c.get("kind") for c in (record.get("llm") or {}).get("calls", [])]
        llm_rt = 0.0
        llm_connects = 0
        llm_connect_ms = 0.0
        prev_end = self._last_llm_end
        for c, kind in sorted(zip(turn.llm_calls, kinds), key=lambda ck: ck[0]["t_req"]):
            if kind == "early-discarded" or c["t_req"] > t1:
                if c.get("t_end"):
                    prev_end = c["t_end"]
                continue
            if llm is not None:
                llm_rt += llm
            if prev_end is None or c["t_req"] - prev_end > LLM_KEEPALIVE_S:
                cost = self._connect_cost("llm")
                if cost is not None:
                    llm_connects += 1
                    llm_connect_ms += cost
            prev_end = c.get("t_end") or c["t_req"]
        if turn.llm_calls:
            ends = [c.get("t_end") for c in turn.llm_calls if c.get("t_end")]
            self._last_llm_end = max(ends) if ends else self._last_llm_end

        est = {
            "asr_ms": dg if (dg is not None and turn.eot is not None) else None,
            "llm_ms": round(llm_rt, 1) if llm_rt else None,
            "llm_new_connection_ms": round(llm_connect_ms, 1) if llm_connects else None,
            "tts_ms": dg if (dg is not None and turn.tts) else None,
        }
        measured = round(sum(h["ms"] or 0 for h in handshakes if h["before_reply_audio"]), 1)
        estimated = round(sum(v for v in est.values() if v), 1)
        total = round(estimated + measured, 1)
        e2e = (record.get("end_to_end") or {}).get("total_ms")
        if dg is None and llm is None and not handshakes:
            return None
        return {
            "rtt_ms": {"deepgram": dg, "llm": llm},
            "handshakes": handshakes,
            "handshake_ms": measured,
            "estimate_ms": est,
            "total_ms": total,
            "share_of_e2e_pct": round(100 * total / e2e, 1) if e2e else None,
            "method": "handshakes measured; other stages = RTT x round trips (estimate)",
        }

    @staticmethod
    def format_line(block: dict) -> str:
        est = block["estimate_ms"]
        parts = [f"{k[:-3]} ~{v:.0f}" for k, v in est.items() if v]
        hs = ", ".join(f"{h['provider']} {h['ms']:.0f} ms" for h in block["handshakes"]) or "none"
        share = f" ({block['share_of_e2e_pct']:.0f}% of end-to-end)" if block.get("share_of_e2e_pct") is not None else ""
        rtt = block["rtt_ms"]
        return (
            f"  network  ~{block['total_ms']:.0f} ms{share} = {' + '.join(parts) or '0'} ms est."
            f" + handshakes {block['handshake_ms']:.0f} ms measured [{hs}]"
            f" | RTT deepgram {rtt['deepgram'] if rtt['deepgram'] is not None else '?'} ms,"
            f" llm {rtt['llm'] if rtt['llm'] is not None else '?'} ms"
        )

    # -------------------------------------------------------------- lifecycle

    async def snapshot(self) -> list[dict[str, Any]]:
        if not any(self.history.values()):
            return []
        return [{"type": "net", "ts": int(time.time() * 1000), **self.status()}]

    async def handle_command(self, msg: dict[str, Any]) -> None:
        if msg.get("type") == "net_check":
            self._wake.set()

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run(self) -> None:
        while True:
            self._wake.clear()
            try:
                msg = await self.check_now()
                down = any(r["state"] == "down" for r in msg["targets"])
            except Exception as e:  # noqa: BLE001
                logger.debug(f"network check failed: {e!r}")
                down = True
            try:
                await asyncio.wait_for(self._wake.wait(), self._retry if down else self._every)
            except asyncio.TimeoutError:
                pass
