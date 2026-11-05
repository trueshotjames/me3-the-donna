"""Headless runner: keeps Donna running without the UI and says how she is doing with sound cues.

    python -m assistant.supervisor          (what the donna service runs on the Pi; pi/donna.service)

Loop:
  1. "starting" beep.
  2. Wait until Deepgram and the LLM host answer (DNS + TCP + TLS, same probe as the UI's internet panel).
     Still unreachable after ~10 s: "down" cue, repeated every CUE_DOWN_REPEAT_SECS while it lasts.
  3. Start ``python -m assistant.main`` (DONNA_SUPERVISED=1). It plays "live" when the ASR is connected,
     "down"/"back" when the internet or a provider drops out and returns, and exits with code 3 when the
     ASR cannot connect at all (instead of running on deaf).
  4. When it exits for any reason: stop its whole process group (tools server included, so nothing is left
     holding the speaker or the ports), play "down", wait (5 s, growing to 60 s if she keeps failing fast),
     back to 2. Her next "live" is played as "back".

The UI still works: the supervisor serves app/ui on 127.0.0.1:8780 (as app/launcher.py does) and
assistant.main serves its events on 8765, so the laptop's SSH tunnel (ssh -N donna, then
http://localhost:18780/?ws=18765) shows her page whenever you want. UI_PORT=0 turns the page server off.

SIGTERM/SIGINT (``donna stop``, systemctl stop): stop her cleanly and exit.
State goes to data/donna_status.json for ``donna status``.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import time

from loguru import logger

from assistant.config import DATA_DIR, ROOT, load
from assistant.cues import play_blocking, write_status

FAST_FAIL_SECS = 60.0
LOG_FILE = ROOT / "app" / "logs" / "donna.log"  # `donna logs` follows this (the Pi keeps no journal on disk)
LOG_MAX_BYTES = 20_000_000  # at supervisor start, a bigger log moves to donna.log.1  # a run shorter than this counts as a failed start (backoff grows)


def serve_ui(port: int):
    """app/ui over HTTP on 127.0.0.1 (same page as app/launcher.py; it connects to ws://127.0.0.1:8765)."""
    import threading
    from functools import partial
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

    handler = partial(SimpleHTTPRequestHandler, directory=str(ROOT / "app" / "ui"))
    handler.log_message = lambda *a, **k: None
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
    except OSError as e:
        logger.warning(f"UI page not served on port {port}: {e}")
        return None
    threading.Thread(target=httpd.serve_forever, daemon=True, name="ui-http").start()
    logger.info(f"UI page at http://127.0.0.1:{port}/ (laptop: ssh -N donna, then http://localhost:18780/?ws=18765)")
    return httpd


class Supervisor:
    def __init__(self, cfg):
        self.cfg = cfg
        self.proc: subprocess.Popen | None = None
        self.log = None  # open file handle for the child's output (set in main)
        self.stopping = False
        self.was_down = False
        self._last_down_cue = 0.0
        try:
            from assistant.main import resolve_device

            self.device = resolve_device(cfg.audio_output_device, want_input=False)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"output device for cues not resolved ({e!r}); using the default device")
            self.device = None

    # ------------------------------------------------------------------ cues

    def cue(self, kind: str) -> None:
        if self.cfg.cues_enabled:
            play_blocking(kind, device=self.device, sample_rate=self.cfg.audio_out_sample_rate, volume=self.cfg.cue_volume)

    def down(self, reason: str, *, repeat: bool = False) -> None:
        now = time.monotonic()
        write_status(DATA_DIR, "down", reason)
        if not self.was_down:
            logger.warning(f"down: {reason}")
        if not self.was_down or (repeat and self.cfg.cue_down_repeat_secs and now - self._last_down_cue >= self.cfg.cue_down_repeat_secs):
            self._last_down_cue = now
            self.cue("down")
        self.was_down = True

    # --------------------------------------------------------------- network

    async def wait_for_network(self) -> None:
        from assistant.netstatus import NetMonitor, targets_from

        mon = NetMonitor(None, targets_from(self.cfg))
        t0 = time.monotonic()
        while not self.stopping:
            results = await asyncio.gather(*(mon.probe(t) for t in mon.targets))
            bad = [f"{t.host} unreachable ({r['error']})" for t, r in zip(mon.targets, results) if not r["ok"]]
            if not bad:
                return
            if time.monotonic() - t0 >= 9:
                self.down("; ".join(bad), repeat=True)
            else:
                write_status(DATA_DIR, "waiting", "; ".join(bad))
            await asyncio.sleep(5)

    # ------------------------------------------------------------- the child

    def start_child(self) -> None:
        env = dict(os.environ, DONNA_SUPERVISED="1", DONNA_RECOVERED="1" if self.was_down else "0", PYTHONUNBUFFERED="1")
        kw: dict = {"cwd": str(ROOT), "env": env}
        if self.log is not None:
            kw.update(stdout=self.log, stderr=subprocess.STDOUT)
        if os.name == "posix":
            kw["start_new_session"] = True  # own process group: stop_child() takes the tools server with it
        else:
            kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        self.proc = subprocess.Popen([sys.executable, "-m", "assistant.main"], **kw)
        write_status(DATA_DIR, "starting", "connecting to Deepgram and the LLM", child_pid=self.proc.pid)
        logger.info(f"started assistant.main (pid {self.proc.pid})")

    def stop_child(self, timeout: float = 10.0) -> None:
        p = self.proc
        if p is None:
            return
        try:
            if os.name == "posix":
                try:
                    os.killpg(p.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            elif p.poll() is None:
                p.terminate()
            try:
                p.wait(timeout)
            except subprocess.TimeoutExpired:
                pass
            if os.name == "posix":
                try:
                    os.killpg(p.pid, signal.SIGKILL)  # whatever is left in the group (tools server, browser)
                except ProcessLookupError:
                    pass
            elif p.poll() is None:
                p.kill()
        finally:
            self.proc = None

    async def run(self) -> None:
        write_status(DATA_DIR, "starting", "supervisor started")
        self.cue("starting")
        backoff = 5.0
        while not self.stopping:
            await self.wait_for_network()
            if self.stopping:
                break
            self.start_child()
            t0 = time.monotonic()
            while not self.stopping and self.proc is not None and self.proc.poll() is None:
                await asyncio.sleep(0.5)
            if self.stopping:
                break
            code = self.proc.returncode if self.proc is not None else None
            ran = time.monotonic() - t0
            self.stop_child(timeout=5)  # the rest of her process group
            self.down(f"she stopped (exit code {code}) after {ran:.0f} s")
            backoff = 5.0 if ran >= FAST_FAIL_SECS else min(60.0, backoff * 2)
            logger.info(f"restarting in {backoff:.0f} s")
            end = time.monotonic() + backoff
            while not self.stopping and time.monotonic() < end:
                await asyncio.sleep(0.5)
        self.stop_child()
        write_status(DATA_DIR, "stopped", "stopped on request")
        logger.info("supervisor stopped")


def main() -> None:
    cfg = load()
    from assistant.main import setup_logging

    setup_logging(cfg.log_level)
    log = None
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > LOG_MAX_BYTES:
            os.replace(LOG_FILE, LOG_FILE.with_suffix(".log.1"))
        log = open(LOG_FILE, "a", buffering=1, encoding="utf-8")
        logger.add(log, level=cfg.log_level, format="{time:HH:mm:ss.SSS} | {level: <7} | [supervisor] {message}")
    except OSError as e:
        logger.warning(f"no log file ({e}); output goes to the terminal/journal only")
    sup = Supervisor(cfg)
    sup.log = log
    ui_port = int(os.getenv("UI_PORT", "8780") or 0)
    httpd = serve_ui(ui_port) if ui_port else None

    def on_signal(*_):
        sup.stopping = True

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    try:
        asyncio.run(sup.run())
    finally:
        if httpd is not None:
            httpd.shutdown()


if __name__ == "__main__":
    main()
