"""Donna's own Spotify Connect speaker (SPOTIFY_SPEAKER=1): go-librespot, started and stopped with the tools server.

Spotify music then plays from Donna herself, through the same speakerphone as her voice (so its echo canceller
removes it), and works with the Spotify app closed on every phone and laptop. The Web API backend (spotify.py)
targets this speaker by name; ducking uses its local REST API (localhost, a few ms) instead of the cloud.

    tools/go-librespot/go-librespot(.exe)   the program (Windows: plus its decoder DLLs; Pi: linux_arm64 build)
    data/go-librespot/                      config.yml (written here) and state.json (the login: keep private)

One-time login (Spotify shows a code to enter at spotify.com/pair; works on a Pi without a screen):
    python -m mcp_servers.tools.devices.speaker --login

Settings (.env): SPOTIFY_SPEAKER_NAME (Donna), SPOTIFY_SPEAKER_BACKEND (wasapi on Windows, alsa or pulseaudio on
the Pi), SPOTIFY_SPEAKER_DEVICE (default = the system's default output: make the speakerphone the default),
SPOTIFY_SPEAKER_PORT (3678). Cost on the Pi: one Go process, ~20-30 MB RAM, a few % CPU while playing.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

try:
    from loguru import logger
except ImportError:  # pragma: no cover
    import logging

    logger = logging.getLogger(__name__)  # type: ignore[assignment]

from mcp_servers.tools.devices.base import DeviceError

ROOT = Path(__file__).resolve().parents[3]


def _default_bin() -> Path:
    exe = "go-librespot.exe" if sys.platform.startswith("win") else "go-librespot"
    return ROOT / "tools" / "go-librespot" / exe


def config_yaml(name: str, backend: str, device: str, port: int, volume: int = 60) -> str:
    return "\n".join([
        "# Donna's Spotify speaker (written by mcp_servers/tools/devices/speaker.py; change .env, not this file)",
        f"device_name: {name}",
        "device_type: speaker",
        f"audio_backend: {backend}",
        f"audio_device: {device}",
        "credentials:",
        "  type: device_auth",
        "server:",
        "  enabled: true",
        "  address: 127.0.0.1",
        f"  port: {port}",
        "bitrate: 160",
        f"initial_volume: {volume}",
        "log_level: info",
        "",
    ])


class Speaker:
    def __init__(self, *, name: str | None = None, binary: str | Path | None = None, config_dir: Path | None = None,
                 backend: str | None = None, device: str | None = None, port: int | None = None, http: Any = None):
        self.name = name or os.getenv("SPOTIFY_SPEAKER_NAME", "Donna") or "Donna"
        self.binary = Path(binary or os.getenv("SPOTIFY_SPEAKER_BIN") or _default_bin())
        if config_dir is None:
            from mcp_servers.common import DATA_DIR

            config_dir = DATA_DIR / "go-librespot"
        self.config_dir = Path(config_dir)
        self.backend = backend or os.getenv("SPOTIFY_SPEAKER_BACKEND") or ("wasapi" if sys.platform.startswith("win") else "alsa")
        self.device = device or os.getenv("SPOTIFY_SPEAKER_DEVICE") or "default"
        self.port = int(port or os.getenv("SPOTIFY_SPEAKER_PORT") or 3678)
        self.url = f"http://127.0.0.1:{self.port}"  # not "localhost": on Windows it tries IPv6 first (2 s stall)
        if http is None:
            import httpx

            http = httpx.Client(timeout=2.0)
        self._http = http
        self._proc: subprocess.Popen | None = None

    # ------------------------------------------------------------ process
    def write_config(self) -> None:
        self.config_dir.mkdir(parents=True, exist_ok=True)
        (self.config_dir / "config.yml").write_text(config_yaml(self.name, self.backend, self.device, self.port), encoding="utf-8")

    def alive(self) -> bool:
        try:
            return self._http.get(self.url + "/").status_code < 500
        except Exception:  # noqa: BLE001
            return False

    def start(self, wait_secs: float = 8.0) -> None:
        if self.alive():
            logger.info(f"speaker: {self.name} already running on port {self.port}")
            return
        if not self.binary.is_file():
            raise DeviceError(f"the Spotify speaker program is missing ({self.binary}); see devices/README.md")
        self.write_config()
        log = open(self.config_dir / "speaker.log", "ab")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self._proc = subprocess.Popen([str(self.binary), "--config_dir", str(self.config_dir)], stdout=log, stderr=log,
                                      stdin=subprocess.DEVNULL, cwd=str(self.binary.parent), creationflags=flags)
        deadline = time.time() + wait_secs
        while time.time() < deadline:
            if self._proc.poll() is not None:
                code = self._proc.returncode
                hint = " (a DLL is missing next to go-librespot.exe)" if code in (-1073741515, 3221225781) else ""
                raise DeviceError(f"the Spotify speaker stopped at start-up, exit code {code}{hint}; see data/go-librespot/speaker.log")
            if self.alive():
                logger.info(f"speaker: {self.name} started (pid {self._proc.pid}, {self.backend}:{self.device})")
                return
            time.sleep(0.2)
        raise DeviceError("the Spotify speaker did not answer in time; see data/go-librespot/speaker.log")

    def stop(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(3)
            except Exception:  # noqa: BLE001
                self._proc.kill()
        self._proc = None

    # ------------------------------------------------------------ local API
    def logged_in(self) -> bool:
        try:
            r = self._http.get(self.url + "/status")
            return r.status_code == 200 and bool((r.json() or {}).get("username"))
        except Exception:  # noqa: BLE001
            return False

    def auth_code(self) -> dict[str, Any] | None:
        try:
            r = self._http.get(self.url + "/auth/code")
            return r.json() if r.status_code == 200 and r.content else None
        except Exception:  # noqa: BLE001
            return None

    def set_volume(self, percent: int) -> None:
        """Instant, local: used for ducking while someone talks (volume_steps is 100, so steps = percent)."""
        try:
            r = self._http.post(self.url + "/player/volume", json={"volume": max(0, min(100, int(percent)))})
        except Exception as e:  # noqa: BLE001
            raise DeviceError("the Spotify speaker is not answering") from e
        if r.status_code >= 400:
            raise DeviceError(f"the Spotify speaker refused the volume ({r.status_code})")


    def playing(self) -> bool:
        try:
            s = self._http.get(self.url + "/status").json() or {}
            return not s.get("stopped", True) and not s.get("paused", True)
        except Exception:  # noqa: BLE001
            return False

    def command(self, name: str) -> bool:
        """pause / resume / next / prev on this speaker, locally (instant). False if it did not take."""
        try:
            return self._http.post(self.url + f"/player/{name}", json={}).status_code < 400
        except Exception:  # noqa: BLE001
            return False


def _login() -> int:
    from mcp_servers.common import setup

    setup("speaker-login")
    sp = Speaker()
    try:
        sp.start()
    except DeviceError as e:
        print(f"Could not start the speaker: {e}")
        return 1
    try:
        if sp.logged_in():
            print(f"The {sp.name} speaker is already logged in. Nothing to do.")
            return 0
        print("Waiting for Spotify's pairing code...")
        shown = False
        deadline = time.time() + 600
        while time.time() < deadline:
            if sp.logged_in():
                print(f"Logged in. The {sp.name} speaker now shows up in Spotify and stays logged in.")
                return 0
            code = sp.auth_code()
            if code and not shown:
                link = code.get("url") or "https://spotify.com/pair"
                print(f"\nOn any phone or computer, open: {link}")
                if code.get("code"):
                    print(f"(if asked, enter the code: {code['code']})")
                print("Approve it with the Spotify Premium account. Waiting...\n")
                shown = True
            time.sleep(1.0)
        print("Timed out waiting for the approval; run this again.")
        return 1
    finally:
        sp.stop()


if __name__ == "__main__":
    if "--login" in sys.argv:
        raise SystemExit(_login())
    print(__doc__)
