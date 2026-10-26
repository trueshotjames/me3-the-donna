"""Real-device backends: the same interfaces as devices/sim.py, on hardware or a real service.

None of these is used until its `*_BACKEND` setting in .env names it (devices/__init__.py),
and each imports its library lazily so the tools server starts without gpiozero, mpv or a
Telegram token installed. They are written against the libraries' documented APIs but were not
run on the Pi yet: treat them as the starting point for the hardware week, not as finished.

    LIGHT_BACKEND=gpio      GpioLights: one PWMLED per room (LIGHT_GPIO_PINS="living room:18,bedroom:23")
    MEDIA_BACKEND=mpv       MpvPlayer: mpv through its JSON IPC socket (pip install python-mpv-jsonipc), files in MUSIC_DIR
    PHONE_BACKEND=telegram  TelegramPhone: messages through a Telegram bot (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_IDS="Mom:12345,...")

The thermostat has no hardware backend: a real one would be a subclass of ThermostatDevice that
talks to the heater's relay or API and keeps the sim's `status()` shape.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from mcp_servers.tools.devices.base import DeviceError, LightDevice, MediaPlayer, PhoneDevice
from mcp_servers.tools.devices.sim import SimLights, SimMediaPlayer, SimPhone


class GpioLights(LightDevice):
    """LEDs or relay modules on GPIO pins, dimmed with software PWM (gpiozero.PWMLED).

    Colour is recorded for the UI but not shown by a single LED; an RGB LED would use gpiozero.RGBLED.
    Keeps the sim's state dict so the UI and the LLM see the same fields as in simulation.
    """

    backend = "gpio"

    def __init__(self, pins: dict[str, int], **sim_kwargs: Any):
        try:
            from gpiozero import PWMLED  # type: ignore
        except ImportError as e:  # pragma: no cover - hardware only
            raise DeviceError("gpiozero is not installed (sudo apt install python3-gpiozero on the Pi)") from e
        self._sim = SimLights(list(pins), **sim_kwargs)  # state keeping and room matching
        self._leds = {room: PWMLED(pin) for room, pin in pins.items()}
        self._apply_all()

    def rooms(self) -> list[str]:
        return self._sim.rooms()

    def _apply_all(self) -> None:
        for light in self._sim.status()["lights"]:
            led = self._leds.get(light["room"])
            if led is not None:
                led.value = light["brightness"] / 100 if light["on"] else 0.0

    def set(self, room: str, *, on=None, brightness=None, color=None) -> dict[str, Any]:
        result = self._sim.set(room, on=on, brightness=brightness, color=color)
        self._apply_all()
        return result

    def status(self) -> dict[str, Any]:
        return dict(self._sim.status(), backend=self.backend)

    def close(self) -> None:
        for led in self._leds.values():
            led.close()


class MpvPlayer(MediaPlayer):
    """mpv over its JSON IPC socket; plays files from MUSIC_DIR (any format mpv reads).

    `play(query)` matches file names and parent folders (so a folder per genre or playlist works).
    Volume ducking while someone speaks is a good next step (set volume to 20 on user/bot speech).
    """

    backend = "mpv"

    def __init__(self, music_dir: str | Path, **sim_kwargs: Any):
        try:
            from python_mpv_jsonipc import MPV  # type: ignore
        except ImportError as e:  # pragma: no cover - needs mpv
            raise DeviceError("python-mpv-jsonipc is not installed (pip install python-mpv-jsonipc; sudo apt install mpv)") from e
        self._dir = Path(music_dir)
        if not self._dir.is_dir():
            raise DeviceError(f"MUSIC_DIR {self._dir} does not exist")
        self._mpv = MPV(start_mpv=True, ipc_socket=str(Path.home() / ".me3-mpv.sock"), video=False)
        self._files = sorted(p for p in self._dir.rglob("*") if p.suffix.lower() in (".mp3", ".flac", ".ogg", ".wav", ".m4a", ".opus"))
        self._sim = SimMediaPlayer(**sim_kwargs, library=self._library())  # queue, volume, shuffle and status shape
        self._mpv.volume = self._sim.status()["volume"]

    def _library(self) -> list[dict[str, Any]]:
        lib = []
        for i, p in enumerate(self._files, start=1):
            lib.append({"id": i, "title": p.stem.replace("_", " "), "artist": p.parent.name, "genre": p.parent.name.lower(), "duration_s": 0,
                        "playlists": [p.parent.name.lower()], "art": ["#60a5fa", "#1e3a8a"], "path": str(p)})
        if not lib:
            raise DeviceError(f"no audio files under {self._dir}")
        return lib

    def _load_current(self) -> None:
        st = self._sim.status()
        q, i = self._sim._s["queue"], self._sim._s["index"]
        if q:
            self._mpv.loadfile(self._sim._by_id[q[i]]["path"])
            self._mpv.pause = st["state"] != "playing"

    def play(self, query: str = "") -> dict[str, Any]:
        st = self._sim.play(query)
        self._load_current()
        return st

    def pause(self) -> dict[str, Any]:
        self._mpv.pause = True
        return self._sim.pause()

    def resume(self) -> dict[str, Any]:
        st = self._sim.resume()
        self._mpv.pause = False
        return st

    def stop(self) -> dict[str, Any]:
        self._mpv.command("stop")
        return self._sim.stop()

    def next(self) -> dict[str, Any]:
        st = self._sim.next()
        self._load_current()
        return st

    def previous(self) -> dict[str, Any]:
        st = self._sim.previous()
        self._load_current()
        return st

    def set_volume(self, level: int) -> dict[str, Any]:
        st = self._sim.set_volume(level)
        self._mpv.volume = st["volume"]
        return st

    def volume_step(self, delta: int) -> dict[str, Any]:
        return self.set_volume(self._sim.status()["volume"] + int(delta))

    def set_mute(self, muted: bool) -> dict[str, Any]:
        self._mpv.mute = bool(muted)
        return self._sim.set_mute(muted)

    def set_shuffle(self, on: bool) -> dict[str, Any]:
        return self._sim.set_shuffle(on)

    def set_repeat(self, mode: str) -> dict[str, Any]:
        return self._sim.set_repeat(mode)

    def tick(self) -> bool:
        changed = self._sim.tick()
        if changed:
            self._load_current()
        return changed

    def status(self) -> dict[str, Any]:
        return dict(self._sim.status(), backend=self.backend)

    def close(self) -> None:
        try:
            self._mpv.terminate()
        except Exception:  # noqa: BLE001
            pass


class TelegramPhone(PhoneDevice):
    """Messages through a Telegram bot (free; arrives on your own phone). Calls are not possible this way."""

    backend = "telegram"

    def __init__(self, token: str, chat_ids: dict[str, str], **sim_kwargs: Any):
        if not token or not chat_ids:
            raise DeviceError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_IDS are needed for the telegram phone backend")
        self._token = token
        self._chats = {name.strip(): cid.strip() for name, cid in chat_ids.items()}
        self._sim = SimPhone(list(self._chats), **sim_kwargs)

    def contacts(self) -> list[str]:
        return list(self._chats)

    def call(self, contact: str) -> dict[str, Any]:
        raise DeviceError("I can send messages but I cannot place calls")

    def hang_up(self) -> dict[str, Any]:
        raise DeviceError("there is no call to end")

    def send_message(self, contact: str, text: str) -> dict[str, Any]:
        import httpx

        result = self._sim.send_message(contact, text)  # resolves the name and records it
        name = result["message"]["contact"]
        t0 = time.time()
        try:
            r = httpx.post(f"https://api.telegram.org/bot{self._token}/sendMessage", json={"chat_id": self._chats[name], "text": text}, timeout=8)
            r.raise_for_status()
        except Exception as e:  # noqa: BLE001
            raise DeviceError(f"Telegram did not accept the message: {e}") from e
        result["message"]["state"] = "delivered"
        result["note"] = f"sent through Telegram in {round((time.time() - t0) * 1000)} ms"
        return result

    def status(self) -> dict[str, Any]:
        return dict(self._sim.status(), backend=self.backend, call=None)


def pins_from_env(text: str | None) -> dict[str, int]:
    """'living room:18,bedroom:23' -> {'living room': 18, 'bedroom': 23}."""
    out: dict[str, int] = {}
    for part in (text or "").split(","):
        if ":" in part:
            room, pin = part.rsplit(":", 1)
            try:
                out[room.strip().lower()] = int(pin)
            except ValueError:
                continue
    return out


def chats_from_env(text: str | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in (text or "").split(","):
        if ":" in part:
            name, cid = part.rsplit(":", 1)
            out[name.strip()] = cid.strip()
    return out
