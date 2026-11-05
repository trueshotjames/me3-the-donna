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

JABRA_BUTTONS=0 turns it off.
"""

from __future__ import annotations

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


def parse_report(report: bytes | list[int]) -> dict[str, bool] | None:
    """Input report 3 -> {"hook": off hook?, "mute": mute pressed?}; None for other reports."""
    r = bytes(report)
    if len(r) < 2 or r[0] != REPORT_ID:
        return None
    return {"hook": bool(r[1] & HOOK_BIT), "mute": bool(r[1] & MUTE_BIT)}


class JabraButtons:
    """Reads the Jabra's phone buttons in a daemon thread and calls on_hook(off_hook: bool) when the hook changes."""

    def __init__(self, on_hook: Callable[[bool], None], *, path: bytes | None = None):
        try:
            import hid
        except ImportError as e:
            raise RuntimeError("hidapi is not installed (pip install hidapi)") from e
        if path is None:
            found = [d for d in hid.enumerate(JABRA_VENDOR) if d.get("usage_page") == TELEPHONY_PAGE]
            if not found:
                raise RuntimeError("no Jabra speakerphone with phone buttons is connected")
            path = found[0]["path"]
            self.name = found[0].get("product_string") or "Jabra"
        else:
            self.name = "Jabra"
        self._dev = hid.device()
        self._dev.open_path(path)
        self._dev.set_nonblocking(True)
        self._on_hook = on_hook
        self._hook: bool | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="jabra-buttons", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                with self._lock:
                    r = self._dev.read(64)
            except Exception as e:  # noqa: BLE001  (unplugged)
                logger.warning(f"jabra: stopped reading the buttons: {e!r}")
                return
            if not r:
                time.sleep(0.02)
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

    def set_leds(self, *, off_hook: bool = False, ring: bool = False, mute: bool = False) -> None:
        """Light the Jabra's call LEDs; off_hook=True makes the next red press send hook = 0."""
        val = (LED_OFF_HOOK if off_hook else 0) | (LED_RING if ring else 0) | (LED_MUTE if mute else 0)
        try:
            with self._lock:
                self._dev.write([REPORT_ID, val])
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
