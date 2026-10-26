"""Builds the household devices the tools server uses, simulated by default.

    LIGHT_BACKEND=sim|gpio   THERMOSTAT_BACKEND=sim   MEDIA_BACKEND=sim|mpv   PHONE_BACKEND=sim|telegram

A backend that cannot start (library missing, no token) logs a warning and falls back to the
simulation, so the demo never loses a widget because a cable is unplugged.
"""

from __future__ import annotations

import os
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
        if changed:
            self.bump()
        return changed

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
    if backend == "gpio":
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
    if backend == "mpv":
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
