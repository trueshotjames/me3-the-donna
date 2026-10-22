"""
Voice Assistant launcher.

Double-clicking the desktop icon runs this file. It:
  1. starts every process listed in app/launch.json (MCP servers, pipeline),
     skipping any whose entry point does not exist yet;
  2. serves app/ui over a local HTTP port;
  3. opens the UI in a pywebview window (falls back to the default browser);
  4. stops all child processes when the window closes.

Run from anywhere:  python app/launcher.py  [--browser] [--stub]
  --browser  open the system browser instead of a pywebview window
  --stub     run the stub event backend even if the real pipeline exists
"""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
ROOT = APP_DIR.parent
UI_DIR = APP_DIR / "ui"
LOG_DIR = APP_DIR / "logs"
CONFIG = APP_DIR / "launch.json"

IS_WINDOWS = os.name == "nt"


def log(msg: str) -> None:
    print(f"[launcher] {msg}", flush=True)


def port_open(host: str, port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.2)
        return s.connect_ex((host, port)) == 0


def free_port(preferred: int) -> int:
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", preferred))
            return preferred
        except OSError:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]


class Procs:
    """Child processes started from launch.json."""

    def __init__(self, entries: list[dict], force_stub: bool):
        self.entries = entries
        self.force_stub = force_stub
        self.procs: list[tuple[str, subprocess.Popen]] = []

    def _entry_exists(self, entry: dict) -> bool:
        check = entry.get("exists")
        return True if not check else (ROOT / check).exists()

    def start(self) -> None:
        LOG_DIR.mkdir(exist_ok=True)
        real_pipeline_started = False
        for entry in self.entries:
            name = entry["name"]
            if entry.get("stub") and real_pipeline_started and not self.force_stub:
                log(f"skip {name}: real backend is running")
                continue
            if not self._entry_exists(entry):
                log(f"skip {name}: {entry['exists']} not found yet")
                continue
            if entry.get("port") and port_open("127.0.0.1", entry["port"]):
                log(f"skip {name}: port {entry['port']} already in use")
                continue
            cmd = [sys.executable if c == "{python}" else c for c in entry["cmd"]]
            out = open(LOG_DIR / f"{name}.log", "a", encoding="utf-8")
            kwargs = {}
            if IS_WINDOWS:
                kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                kwargs["start_new_session"] = True
            p = subprocess.Popen(cmd, cwd=ROOT, stdout=out, stderr=subprocess.STDOUT, **kwargs)
            self.procs.append((name, p))
            log(f"started {name} (pid {p.pid}): {' '.join(cmd)}")
            if entry.get("publishes_events") and not entry.get("stub"):
                real_pipeline_started = True
            time.sleep(entry.get("delay_s", 0.3))

    def stop(self) -> None:
        for name, p in reversed(self.procs):
            if p.poll() is not None:
                continue
            log(f"stopping {name}")
            try:
                if IS_WINDOWS:
                    p.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    os.killpg(p.pid, signal.SIGTERM)
                p.wait(timeout=3)
            except Exception:
                p.kill()


def serve_ui(port: int) -> ThreadingHTTPServer:
    handler = partial(SimpleHTTPRequestHandler, directory=str(UI_DIR))
    handler.log_message = lambda *a, **k: None  # quiet
    httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def open_window(url: str, use_browser: bool) -> None:
    if not use_browser:
        try:
            import webview  # pywebview

            webview.create_window(
                "Voice Assistant", url, width=1100, height=720, min_size=(800, 560),
                background_color="#0f1420",
            )
            webview.start()
            return
        except ImportError:
            log("pywebview not installed, opening in browser (pip install pywebview)")
        except Exception as e:  # GUI backend missing on the Pi, etc.
            log(f"pywebview failed ({e}), opening in browser")
    webbrowser.open(url)
    log("UI is open in your browser. Press Ctrl+C here to stop everything.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass


def main() -> None:
    use_browser = "--browser" in sys.argv
    force_stub = "--stub" in sys.argv
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    ws_port = cfg.get("events_port", 8765)
    ui_port = free_port(cfg.get("ui_port", 8780))

    procs = Procs(cfg["processes"], force_stub)
    procs.start()
    httpd = serve_ui(ui_port)
    url = f"http://127.0.0.1:{ui_port}/?ws={ws_port}"
    log(f"UI at {url}")
    try:
        open_window(url, use_browser)
    finally:
        httpd.shutdown()
        procs.stop()
        log("bye")


if __name__ == "__main__":
    main()
