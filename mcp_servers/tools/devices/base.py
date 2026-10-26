"""Device interfaces for the household tools (lights, thermostat, music player, phone).

The tools MCP server talks to devices only through these classes, so a real device is a
new subclass plus one line in `devices/__init__.py` (or a `*_BACKEND` setting in .env):
the tools, the UI events and the prompt stay the same. See devices/README.md.

Every method returns a plain dict (JSON-able) that describes the new state; the tools server
passes it to the LLM and, through the scheduler, to the UI widget. A method raises
`DeviceError` for a refused command ("no such room"); the tool turns it into {"error": ...}.

State that changes on its own over time (a track playing, a call connecting, a room warming up)
is advanced lazily in `tick()`, which the server calls once a second from `poll_due`: no threads,
nothing runs while the house is quiet.
"""

from __future__ import annotations

from typing import Any


class DeviceError(Exception):
    """A command the device cannot carry out; the message is spoken to the user."""


class Device:
    name = "device"
    backend = "base"

    def status(self) -> dict[str, Any]:
        raise NotImplementedError

    def tick(self) -> bool:
        """Advance time-based state; return True when something observable changed."""
        return False

    def close(self) -> None:
        pass


class LightDevice(Device):
    """One or more lights, addressed by room name ("all" = every room)."""

    name = "lights"

    def rooms(self) -> list[str]:
        raise NotImplementedError

    def set(self, room: str, *, on: bool | None = None, brightness: int | None = None, color: str | None = None) -> dict[str, Any]:
        """Change one light (or all). brightness 0-100, color a name or #rrggbb. Returns the lights changed."""
        raise NotImplementedError


class ThermostatDevice(Device):
    name = "thermostat"

    def set(self, *, target: float | None = None, mode: str | None = None) -> dict[str, Any]:
        """mode: heat | cool | auto | off."""
        raise NotImplementedError


class MediaPlayer(Device):
    """Music playback. `play(query)` picks something by title, artist, genre or playlist name."""

    name = "media"

    def play(self, query: str = "") -> dict[str, Any]:
        raise NotImplementedError

    def pause(self) -> dict[str, Any]:
        raise NotImplementedError

    def resume(self) -> dict[str, Any]:
        raise NotImplementedError

    def stop(self) -> dict[str, Any]:
        raise NotImplementedError

    def next(self) -> dict[str, Any]:
        raise NotImplementedError

    def previous(self) -> dict[str, Any]:
        raise NotImplementedError

    def set_volume(self, level: int) -> dict[str, Any]:
        """0-100."""
        raise NotImplementedError

    def volume_step(self, delta: int) -> dict[str, Any]:
        raise NotImplementedError

    def set_mute(self, muted: bool) -> dict[str, Any]:
        raise NotImplementedError

    def set_shuffle(self, on: bool) -> dict[str, Any]:
        raise NotImplementedError

    def set_repeat(self, mode: str) -> dict[str, Any]:
        """off | one | all."""
        raise NotImplementedError


class PhoneDevice(Device):
    """Calls and messages to named contacts."""

    name = "phone"

    def contacts(self) -> list[str]:
        raise NotImplementedError

    def call(self, contact: str) -> dict[str, Any]:
        raise NotImplementedError

    def hang_up(self) -> dict[str, Any]:
        raise NotImplementedError

    def send_message(self, contact: str, text: str) -> dict[str, Any]:
        raise NotImplementedError
