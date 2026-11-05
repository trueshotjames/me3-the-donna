"""Donna as a Spotify speaker through Spotify's official Web Playback SDK (SPOTIFY_WEBPLAYER=1).

Why not go-librespot: Spotify refuses its audio keys for newer accounts (King's included), so it plays silence.
The Web Playback SDK is Spotify's own player for web pages: a small page (below), opened in a hidden browser
window, registers a Spotify Connect device called "Donna" that plays through the system's default speaker. The
Web API backend (spotify.py) then always plays on it, with the Spotify app closed on every phone and laptop.

    tools server --serves--> http://127.0.0.1:8767/        the player page
                             http://127.0.0.1:8767/token   a fresh access token (local only; scope "streaming")
                 --starts--> Chrome / Edge (laptop) or Chromium (Pi) in app mode, off-screen, own profile

Needs: Spotify Premium; the login with the streaming permission (spotify_login); a browser with Widevine DRM
(Chrome and Edge have it; on the Pi: sudo apt install chromium libwidevinecdm0). Cost on the Pi: one Chromium
renderer, roughly 250-350 MB RAM and a few % CPU while playing.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

try:
    from loguru import logger
except ImportError:  # pragma: no cover
    import logging

    logger = logging.getLogger(__name__)  # type: ignore[assignment]

from mcp_servers.tools.devices.base import DeviceError

PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Donna speaker</title></head>
<body style="font-family:sans-serif;background:#111;color:#ccc">
<p id="s">Starting the Donna speaker...</p>
<script src="https://sdk.scdn.co/spotify-player.js"></script>
<script>
const say = (t) => { document.getElementById("s").textContent = t; };
const report = (kind, data) => fetch("/state", {method: "POST", headers: {"Content-Type": "application/json"},
  body: JSON.stringify({kind, data: data || {}})}).catch(() => {});
window.onSpotifyWebPlaybackSDKReady = () => {
  const player = new Spotify.Player({
    name: "__NAME__",
    volume: __VOLUME__,
    getOAuthToken: (cb) => fetch("/token").then((r) => r.json()).then((j) => cb(j.access_token)),
  });
  player.addListener("ready", ({device_id}) => { say("Donna speaker ready"); report("ready", {device_id}); });
  player.addListener("not_ready", ({device_id}) => { say("offline"); report("not_ready", {device_id}); });
  for (const e of ["initialization_error", "authentication_error", "account_error", "playback_error"]) {
    player.addListener(e, ({message}) => { say(e + ": " + message); report(e, {message}); });
  }
  player.addListener("autoplay_failed", () => report("autoplay_failed"));
  player.addListener("player_state_changed", (s) => report("state", s ? {paused: s.paused} : {}));
  player.connect().then((ok) => report(ok ? "connecting" : "connect_failed"));
  setInterval(() => player.activateElement && player.activateElement(), 60000);
};
</script></body></html>
"""


def _kill_with_us(proc: subprocess.Popen) -> Any:
    """Windows: put the browser in a job object that dies with this process (KILL_ON_JOB_CLOSE), so closing or killing
    Donna closes the hidden window too. Its own child processes join the job. Returns the job handle (keep it)."""
    try:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)

        class IO_COUNTERS(ctypes.Structure):  # noqa: N801
            _fields_ = [(n, ctypes.c_ulonglong) for n in ("r", "w", "o", "rb", "wb", "ob")]

        class BASIC(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class EXTENDED(ctypes.Structure):
            _fields_ = [("Basic", BASIC), ("Io", IO_COUNTERS), ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        k32.CreateJobObjectW.restype = wintypes.HANDLE
        job = k32.CreateJobObjectW(None, None)
        info = EXTENDED()
        info.Basic.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k32.SetInformationJobObject(wintypes.HANDLE(job), 9, ctypes.byref(info), ctypes.sizeof(info)):
            raise OSError(ctypes.get_last_error())
        if not k32.AssignProcessToJobObject(wintypes.HANDLE(job), wintypes.HANDLE(int(proc._handle))):
            raise OSError(ctypes.get_last_error())
        return job
    except Exception as e:  # noqa: BLE001
        logger.warning(f"webplayer: could not tie the browser to Donna ({e!r}); it may stay open after a crash")
        return None


def find_browser() -> str | None:
    env = os.getenv("SPOTIFY_WEBPLAYER_BROWSER")
    if env:
        return env
    if sys.platform.startswith("win"):
        for p in (r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                  r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                  r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                  r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"):
            if Path(p).is_file():
                return p
        return None
    for name in ("chromium", "chromium-browser", "google-chrome", "microsoft-edge"):
        found = shutil.which(name)
        if found:
            return found
    return None


class WebPlayer:
    def __init__(self, token: Callable[[], str], *, name: str | None = None, port: int | None = None,
                 profile_dir: Path | None = None, volume: int = 60):
        self.name = name or os.getenv("SPOTIFY_SPEAKER_NAME", "Donna") or "Donna"
        self.port = int(port or os.getenv("SPOTIFY_WEBPLAYER_PORT") or 8767)
        self._token = token
        if profile_dir is None:
            from mcp_servers.common import DATA_DIR

            profile_dir = DATA_DIR / "webplayer-profile"
        self.profile_dir = Path(profile_dir)
        self.volume = volume
        self.state: dict[str, Any] = {"kind": "starting"}
        self.ready = threading.Event()
        self._server: ThreadingHTTPServer | None = None
        self._proc: subprocess.Popen | None = None
        self._job: Any = None  # Windows job object: the browser dies with this process

    # -------------------------------------------------------------- local server
    def _handler(self):
        wp = self

        class H(BaseHTTPRequestHandler):
            def _send(self, code: int, body: bytes, ctype: str) -> None:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):  # noqa: N802
                if self.path in ("/", "/index.html"):
                    page = PAGE.replace("__NAME__", wp.name).replace("__VOLUME__", str(round(wp.volume / 100, 2)))
                    self._send(200, page.encode(), "text/html; charset=utf-8")
                elif self.path == "/token":
                    try:
                        self._send(200, json.dumps({"access_token": wp._token()}).encode(), "application/json")
                    except Exception as e:  # noqa: BLE001
                        logger.warning(f"webplayer: no token: {e}")
                        self._send(503, b"{}", "application/json")
                else:
                    self._send(404, b"", "text/plain")

            def do_POST(self):  # noqa: N802
                n = int(self.headers.get("Content-Length") or 0)
                try:
                    msg = json.loads(self.rfile.read(n) or b"{}")
                except ValueError:
                    msg = {}
                if self.path == "/state":
                    wp._on_state(msg.get("kind", ""), msg.get("data") or {})
                self._send(204, b"", "text/plain")

            def log_message(self, *a):
                pass

        return H

    def _on_state(self, kind: str, data: dict[str, Any]) -> None:
        if kind == "state":
            self.state["paused"] = data.get("paused")
            return
        self.state = {"kind": kind, **{k: v for k, v in data.items() if k != "device_id"}}
        if kind == "ready":
            self.ready.set()
            logger.info(f"webplayer: {self.name} speaker ready")
        elif kind == "not_ready":
            self.ready.clear()
            logger.warning("webplayer: speaker went offline")
        elif kind not in ("connecting",):
            logger.warning(f"webplayer: {kind} {data.get('message', '')}")

    # ------------------------------------------------------------------- browser
    def ensure_widevine(self) -> Path | None:
        """Pi: point Donna's own Chromium profile at the installed Widevine (DRM), or Spotify's player fails with
        "initialization_error: Failed to initialize player".

        Chromium only loads a system Widevine (/opt/WidevineCdm, from libwidevinecdm0) when the profile has
        WidevineCdm/latest-component-updated-widevine-cdm saying where it is. A normal desktop profile has it,
        Donna's separate profile (data/webplayer-profile) did not (found and fixed on the Pi, 5 Nov 2026). An
        existing pointer is left alone: Chromium may have rewritten it to its own copy. Chrome/Edge (laptop) bring
        their own Widevine, so this does nothing on Windows.
        """
        if sys.platform.startswith("win"):
            return None
        pointer = self.profile_dir / "WidevineCdm" / "latest-component-updated-widevine-cdm"
        if pointer.exists():
            return pointer
        for d in (os.getenv("WIDEVINE_CDM_DIR"), "/opt/WidevineCdm", "/usr/lib/chromium/WidevineCdm"):
            if d and (Path(d) / "manifest.json").exists():
                pointer.parent.mkdir(parents=True, exist_ok=True)
                pointer.write_text(json.dumps({"Path": d}, separators=(",", ":")), encoding="utf-8")
                logger.info(f"webplayer: Widevine pointer written ({d})")
                return pointer
        logger.warning("webplayer: no Widevine found (sudo apt install libwidevinecdm0); Spotify will not play")
        return None

    def start(self, wait_secs: float = 20.0) -> None:
        browser = find_browser()
        if not browser:
            raise DeviceError("no Chrome, Edge or Chromium found for the Spotify speaker page")
        self._server = ThreadingHTTPServer(("127.0.0.1", self.port), self._handler())
        threading.Thread(target=self._server.serve_forever, name="webplayer-http", daemon=True).start()
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self.ensure_widevine()
        args = [browser, f"--app=http://127.0.0.1:{self.port}/", f"--user-data-dir={self.profile_dir}",
                "--autoplay-policy=no-user-gesture-required", "--no-first-run", "--no-default-browser-check",
                "--window-position=-2400,-2400", "--window-size=320,240", "--disable-background-timer-throttling",
                "--disable-renderer-backgrounding", "--disable-backgrounding-occluded-windows"]
        if not sys.platform.startswith("win"):
            # Pi: a fresh Chromium profile otherwise opens a "choose a keyring password" dialog that nobody answers
            args.append("--password-store=basic")
        args += os.getenv("SPOTIFY_WEBPLAYER_ARGS", "").split()  # extra browser flags, e.g. --ozone-platform=x11
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if sys.platform.startswith("win"):
            self._proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                          stderr=subprocess.DEVNULL, creationflags=flags)
            self._job = _kill_with_us(self._proc)
        else:
            # v9 (King): the browser must close with Donna even when the app is force-closed (no atexit then). A tiny
            # shell watchdog closes it as soon as this tools server's process is gone.
            script = ('"$@" & B=$!; trap \'kill $B 2>/dev/null; exit 0\' TERM INT; '
                      f'while kill -0 {os.getpid()} 2>/dev/null && kill -0 $B 2>/dev/null; do sleep 2; done; '
                      'kill $B 2>/dev/null')
            self._proc = subprocess.Popen(["sh", "-c", script, "webplayer", *args], stdin=subprocess.DEVNULL,
                                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not self.ready.wait(wait_secs):
            raise DeviceError(f"the Spotify speaker page did not get ready ({self.state.get('kind')}: "
                              f"{self.state.get('message', 'no answer')})")

    def stop(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(3)
            except Exception:  # noqa: BLE001
                self._proc.kill()
        self._proc = None
        if self._server is not None:
            self._server.shutdown()
            self._server = None

    # the speaker interface spotify.py uses (volume and pause go through the Web API for this one)
    def set_volume(self, percent: int) -> None:
        raise DeviceError("volume goes through the Web API")

    def playing(self) -> bool:
        return False

    def command(self, name: str) -> bool:
        return False
