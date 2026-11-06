"""The Jabra speakerphone's phone buttons (green = pick up, red = hang up) for Telegram calls.

The Jabra SPEAK 510 (USB vendor 0x0B0E) has a HID interface on the Telephony usage page (0x0B). Its input
report 3 carries, in byte 1: bit 0 Hook Switch (1 = off hook, after green; 0 = on hook, after red), bit 4 Phone
Mute. Its output report 3 sets the LEDs: bit 0 Off-Hook (green light, "in a call"), bit 3 Ring, bit 2 Mute.
With no call, the Jabra flips the hook state itself on every press; once the host lights Off-Hook, red reliably
sends hook = 0. So: light Off-Hook when a call starts, and treat hook = 0 during a call as "hang up".

Cross-platform through hidapi (pip install hidapi): Windows needs nothing more; on the Pi (Linux) the user needs
read/write access to the hidraw node, e.g. /etc/udev/rules.d/50-jabra.rules:
    KERNEL=="hidraw*", ATTRS{idVendor}=="0b0e", MODE="0666"
then `sudo udevadm control --reload && sudo udevadm trigger`.

The + and - buttons arrive as input report 1 (Consumer page), byte 1: bit 0 Volume Down, bit 1 Volume Up. Windows
and the Pi's desktop act on them already; with no desktop nothing does, so on Linux a press that changed nothing
moves the PipeWire output, which Donna, Spotify and calls all play through.

JABRA_BUTTONS=0 turns it off.
"""

from __future__ import annotations

import shutil
import subprocess
import threading
import time
from typing import Callable

try:
    from loguru import logger
except ImportError:  # pragma: no cover
    import logging

    logger = logging.getLogger(__name__)  # type: ignore[assignment]

JABRA_VENDOR = 0x0B0E
TELEPHONY_PAGE = 0x0B
REPORT_ID = 0x03
HOOK_BIT, MUTE_BIT = 0x01, 0x10  # input report 3, byte 1
LED_OFF_HOOK, LED_MUTE, LED_RING = 0x01, 0x04, 0x08  # output report 3, byte 1
RETRY_SECS = 2.0
CONSUMER_REPORT = 0x01
VOL_DOWN_BIT, VOL_UP_BIT = 0x01, 0x02  # input report 1, byte 1
VOLUME_STEP = "5%"
DESKTOP_WAIT_SECS = 0.25  # time for the desktop to act on the press first
SINK = "@DEFAULT_AUDIO_SINK@"


def parse_report(report: bytes | list[int]) -> dict[str, bool] | None:
    """Input report 3 -> {"hook": off hook?, "mute": mute pressed?}; None for other reports."""
    r = bytes(report)
    if len(r) < 2 or r[0] != REPORT_ID:
        return None
    return {"hook": bool(r[1] & HOOK_BIT), "mute": bool(r[1] & MUTE_BIT)}


def _wpctl(*args: str) -> str:
    try:
        return subprocess.run(["wpctl", *args], capture_output=True, text=True, timeout=2).stdout.strip()
    except (OSError, subprocess.SubprocessError) as e:
        logger.debug(f"jabra: wpctl failed: {e!r}")
        return ""


def volume_press(up: bool) -> None:
    """One step of the master volume, unless the desktop already took this press (it would count twice)."""
    before = _wpctl("get-volume", SINK)
    time.sleep(DESKTOP_WAIT_SECS)
    if _wpctl("get-volume", SINK) == before:
        _wpctl("set-volume", "-l", "1.0", SINK, VOLUME_STEP + ("+" if up else "-"))


def _hid():
    """hidapi's module. On Linux `hid` is the libusb build, which reports usage page 0, so prefer `hidraw` there."""
    try:
        import hidraw

        return hidraw
    except ImportError:
        pass
    try:
        import hid
    except ImportError as e:
        raise RuntimeError("hidapi is not installed (pip install hidapi)") from e
    return hid


class JabraButtons:
    """Reads the Jabra's phone buttons in a daemon thread and calls on_hook(off_hook: bool) when the hook changes."""

    def __init__(self, on_hook: Callable[[bool], None], *, path: bytes | None = None):
        self._hid = _hid()
        self._path = path
        self.name = "Jabra"
        self._dev = self._open()
        self._leds = 0
        self._on_hook = on_hook
        self._volume = shutil.which("wpctl") is not None  # Linux with PipeWire; Windows handles these keys itself
        self._vol_bits = 0
        self._hook: bool | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="jabra-buttons", daemon=True)
        self._thread.start()

    def _open(self):
        path = self._path
        if path is None:
            found = [d for d in self._hid.enumerate(JABRA_VENDOR) if d.get("usage_page") == TELEPHONY_PAGE]
            if not found:
                raise RuntimeError("no Jabra speakerphone with phone buttons is connected")
            path = found[0]["path"]
            self.name = found[0].get("product_string") or "Jabra"
        dev = self._hid.device()
        dev.open_path(path)
        dev.set_nonblocking(True)
        return dev

    def _reopen(self) -> None:
        """The Jabra dropped off USB (replugged, or reset): wait for it, and keep the buttons and lights working."""
        while not self._stop.wait(RETRY_SECS):
            try:
                with self._lock:
                    try:
                        self._dev.close()
                    except Exception:  # noqa: BLE001
                        pass
                    self._dev = self._open()
                    self._dev.write([REPORT_ID, self._leds, 0])
                logger.info("jabra: reconnected the buttons")
                return
            except Exception as e:  # noqa: BLE001
                logger.debug(f"jabra: still waiting for the speakerphone: {e!r}")

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                with self._lock:
                    r = self._dev.read(64)
            except Exception as e:  # noqa: BLE001  (unplugged)
                logger.warning(f"jabra: lost the buttons, retrying: {e!r}")
                self._reopen()
                continue
            if not r:
                time.sleep(0.02)
                continue
            if r[0] == CONSUMER_REPORT and len(r) > 1:
                self._on_volume(r[1])
                continue
            ev = parse_report(r)
            logger.debug(f"jabra: report {bytes(r).hex()} -> {ev}")
            if ev is None or ev["hook"] == self._hook:
                continue
            self._hook = ev["hook"]
            try:
                self._on_hook(ev["hook"])
            except Exception as e:  # noqa: BLE001
                logger.warning(f"jabra: button handler failed: {e!r}")

    def _on_volume(self, bits: int) -> None:
        pressed = bits & ~self._vol_bits  # a press, not the release or a held button
        self._vol_bits = bits
        if self._volume and pressed & (VOL_UP_BIT | VOL_DOWN_BIT):
            threading.Thread(target=volume_press, args=(bool(pressed & VOL_UP_BIT),), name="jabra-volume", daemon=True).start()

    def set_leds(self, *, off_hook: bool = False, ring: bool = False, mute: bool = False) -> None:
        """Light the Jabra's call LEDs; off_hook=True makes the next red press send hook = 0."""
        val = (LED_OFF_HOOK if off_hook else 0) | (LED_RING if ring else 0) | (LED_MUTE if mute else 0)
        try:
            self._leds = val
            with self._lock:
                self._dev.write([REPORT_ID, val, 0])  # report 3 is 2 data bytes; a short write reset the Jabra on the Pi
            self._hook = off_hook  # the Jabra follows the host's state from here
        except Exception as e:  # noqa: BLE001
            logger.debug(f"jabra: could not set the LEDs: {e!r}")

    def close(self) -> None:
        self._stop.set()
        self.set_leds()
        try:
            self._dev.close()
        except Exception:  # noqa: BLE001
            pass
