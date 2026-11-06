"""Builds the household devices the tools server uses, simulated by default.

    LIGHT_BACKEND=sim|tapo|gpio   THERMOSTAT_BACKEND=sim   MEDIA_BACKEND=sim|spotify|mpv   PHONE_BACKEND=sim|telegram|telegram_bot

A backend that cannot start (library missing, no token) logs a warning and falls back to the
simulation, so the demo never loses a widget because a cable is unplugged.
"""

from __future__ import annotations

import os
import threading
from typing import Any, Callable

try:
    from loguru import logger
except ImportError:  # app/stub_events.py imports the sim devices with the standard library only
    import logging

    logger = logging.getLogger(__name__)  # type: ignore[assignment]

from mcp_servers.tools.devices.base import Device, DeviceError, LightDevice, MediaPlayer, PhoneDevice, ThermostatDevice
from mcp_servers.tools.devices.sim import DEFAULT_CONTACTS, DEFAULT_ROOMS, SimLights, SimMediaPlayer, SimPhone, SimThermostat

__all__ = ["Device", "DeviceError", "LightDevice", "MediaPlayer", "PhoneDevice", "ThermostatDevice", "Devices", "build_devices"]


def _list(name: str, default: list[str]) -> list[str]:
    value = os.getenv(name, "")
    items = [x.strip() for x in value.split(",") if x.strip()]
    return items or default


class Devices:
    """The house: one device per kind, plus a version counter the scheduler polls for UI pushes."""

    def __init__(self, lights: LightDevice, thermostat: ThermostatDevice, media: MediaPlayer, phone: PhoneDevice):
        self.lights = lights
        self.thermostat = thermostat
        self.media = media
        self.phone = phone
        self.version = 1
        self._hold_lock = threading.Lock()
        self._music_held = False  # paused for a call, resumes when the call ends
        self._dialing = False
        self._held_call = None  # started_at of the call already seen, so a choice made during it sticks

    @property
    def all(self) -> list[Device]:
        return [self.lights, self.thermostat, self.media, self.phone]

    def bump(self) -> None:
        self.version += 1

    def tick(self) -> bool:
        """Advance every device; bump the version when something the UI shows changed."""
        changed = False
        for d in self.all:
            try:
                changed = d.tick() or changed
            except Exception as e:  # noqa: BLE001
                logger.warning(f"{d.name} tick failed: {e!r}")
        self._sync_call_music()
        if changed:
            self.bump()
        return changed

    # Music and calls. The pause goes through the player like any other, so Spotify, the house snapshot, the UI and
    # assistant/music.py all see it. Only the music this paused comes back, and "play" or "stop" during a call wins.
    def _call_now(self) -> dict[str, Any] | None:
        call = self.phone.status().get("call")
        return call if call and call.get("state") in ("ringing", "connected") else None

    def _sync_call_music(self) -> None:
        """From tick(), which must stay quick: the player is slow, so it is called on a thread."""
        try:
            call = self._call_now()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"music: could not read the call state: {e!r}")
            return
        if call and call.get("started_at") != self._held_call:
            self._held_call = call.get("started_at")  # e.g. an incoming call answered on the Jabra
            threading.Thread(target=self.hold_music, name="call-music", daemon=True).start()
        elif not call and self._music_held and not self._dialing:
            threading.Thread(target=self.release_music, name="call-music", daemon=True).start()

    def hold_music(self) -> None:
        with self._hold_lock:
            if self._music_held:
                return
            try:
                if self.media.status().get("state") != "playing":
                    return
                self.media.pause()
            except Exception as e:  # noqa: BLE001
                logger.warning(f"music: could not pause for the call: {e!r}")
                return
            self._music_held = True
        logger.info("music: paused for the call")
        self.bump()

    def release_music(self) -> None:
        with self._hold_lock:
            if not self._music_held or self._dialing or self._call_now():
                return
            self._music_held = False
            try:
                if self.media.status().get("state") == "paused":
                    self.media.resume()
            except Exception as e:  # noqa: BLE001
                logger.warning(f"music: could not resume after the call: {e!r}")
                return
        logger.info("music: resumed after the call")
        self.bump()

    def drop_music_hold(self) -> None:
        """The user changed the music themselves; leave it as they set it."""
        self._music_held = False

    def place_call(self, contact: str) -> dict[str, Any]:
        """Pause the music, then call. If the attempt fails, the music comes back at once."""
        self._dialing = True
        self.hold_music()
        try:
            return self.phone.call(contact)
        finally:
            self._dialing = False
            self.release_music()

    def status(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "lights": self.lights.status()["lights"],
            "rooms": self.lights.rooms(),
            "thermostat": self.thermostat.status(),
            "media": self.media.status(),
            "phone": self.phone.status(),
            "backends": {d.name: d.backend for d in self.all},
        }

    def close(self) -> None:
        for d in self.all:
            try:
                d.close()
            except Exception:  # noqa: BLE001
                pass


def build_devices(load: Callable[[str], dict[str, Any] | None], persist: Callable[[str, dict[str, Any]], None]) -> Devices:
    kw = dict(load=load, persist=persist)
    rooms = _list("LIGHT_ROOMS", DEFAULT_ROOMS)
    contacts = _list("PHONE_CONTACTS", DEFAULT_CONTACTS)

    lights: LightDevice
    backend = os.getenv("LIGHT_BACKEND", "sim").lower()
    if backend == "tapo":
        try:
            from mcp_servers.tools.devices.tapo import TapoLights, bulbs_from_env

            lights = TapoLights(bulbs_from_env(os.getenv("TAPO_BULBS")), **kw)
        except DeviceError as e:
            logger.warning(f"lights: {e}; using the simulation")
            lights = SimLights(rooms, **kw)
    elif backend == "gpio":
        try:
            from mcp_servers.tools.devices.hardware import GpioLights, pins_from_env

            lights = GpioLights(pins_from_env(os.getenv("LIGHT_GPIO_PINS")), **kw)
        except DeviceError as e:
            logger.warning(f"lights: {e}; using the simulation")
            lights = SimLights(rooms, **kw)
    else:
        lights = SimLights(rooms, **kw)

    thermostat = SimThermostat(unit=os.getenv("THERMOSTAT_UNIT", "C"), **kw)

    media: MediaPlayer
    backend = os.getenv("MEDIA_BACKEND", "sim").lower()
    if backend == "spotify":
        try:
            from mcp_servers.tools.devices.spotify import SpotifyPlayer

            speaker = None
            if os.getenv("SPOTIFY_SPEAKER", "0").lower() in ("1", "true", "yes"):
                # v9: Donna is the Spotify speaker herself (go-librespot), music through the speakerphone
                from mcp_servers.tools.devices.speaker import Speaker

                try:
                    speaker = Speaker()
                    speaker.start()
                except DeviceError as e:
                    logger.warning(f"media: {e}; Spotify will play on other devices")
                    speaker = None
            media = SpotifyPlayer(device=speaker.name if speaker else None, speaker=speaker)
            if speaker is None and os.getenv("SPOTIFY_WEBPLAYER", "0").lower() in ("1", "true", "yes"):
                # v9: Donna as a speaker through Spotify's official Web Playback SDK in a hidden browser window
                from mcp_servers.tools.devices.webplayer import WebPlayer

                import threading

                wp = WebPlayer(media._access_token)
                media._speaker, media._device_name = wp, wp.name  # until it is ready, "play" says it is starting

                def _start_webplayer(m=media, w=wp):
                    try:
                        w.start()
                    except DeviceError as e:
                        w.stop()
                        m._speaker, m._device_name = None, os.getenv("SPOTIFY_DEVICE", "")
                        logger.warning(f"media: {e}; Spotify will play on other devices")

                threading.Thread(target=_start_webplayer, name="webplayer-start", daemon=True).start()
        except DeviceError as e:
            logger.warning(f"media: {e}; using the simulation")
            media = SimMediaPlayer(**kw)
    elif backend == "mpv":
        try:
            from mcp_servers.tools.devices.hardware import MpvPlayer

            media = MpvPlayer(os.getenv("MUSIC_DIR", "~/Music"), **kw)
        except DeviceError as e:
            logger.warning(f"media: {e}; using the simulation")
            media = SimMediaPlayer(**kw)
    else:
        media = SimMediaPlayer(**kw)

    phone: PhoneDevice
    backend = os.getenv("PHONE_BACKEND", "sim").lower()
    if backend == "telegram":
        # v9: your own Telegram account (Telethon) for messages, your Android phone (ADB) for calls
        try:
            from mcp_servers.tools.devices.telegram import TelegramPhone as TelegramUserPhone, aliases_from_env

            phone = TelegramUserPhone(os.getenv("TELEGRAM_API_ID", ""), os.getenv("TELEGRAM_API_HASH", ""),
                                      aliases_from_env(os.getenv("TELEGRAM_CONTACTS")), **kw)
        except DeviceError as e:
            logger.warning(f"phone: {e}; using the simulation")
            phone = SimPhone(contacts, **kw)
    elif backend == "telegram_bot":
        try:
            from mcp_servers.tools.devices.hardware import TelegramPhone, chats_from_env

            phone = TelegramPhone(os.getenv("TELEGRAM_BOT_TOKEN", ""), chats_from_env(os.getenv("TELEGRAM_CHAT_IDS")), **kw)
        except DeviceError as e:
            logger.warning(f"phone: {e}; using the simulation")
            phone = SimPhone(contacts, **kw)
    else:
        phone = SimPhone(contacts, **kw)

    devices = Devices(lights, thermostat, media, phone)
    logger.info("devices: " + ", ".join(f"{d.name}={d.backend}" for d in devices.all))
    return devices
