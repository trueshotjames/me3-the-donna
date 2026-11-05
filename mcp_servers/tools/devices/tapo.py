"""LIGHT_BACKEND=tapo: TP-Link Tapo bulbs (L530E and friends) over the local network, no cloud round trip.

    TAPO_BULBS="living room:192.168.1.50"           room name : bulb IP (several: comma separated)
    TAPO_USERNAME / TAPO_PASSWORD                   your TP-Link (Tapo app) account, in .env only

Uses python-kasa (KLAP protocol, pure Python, works on the Pi's aarch64). The bulb must be on the same
network as the laptop or Pi, and newer firmware needs Tapo app > Me > Third-Party Services >
Third-Party Compatibility switched on. Give the bulb a fixed IP (DHCP reservation in the router).

The room names, the state shape and the color names are the simulation's (SimLights keeps the
state and matches rooms), so the lights widget, the prompt and set_light do not change. One
asyncio loop on one small thread talks to the bulbs; the state is re-read every TAPO_POLL_SECS
(default 15) so a change made in the Tapo app shows up in the widget.
"""

from __future__ import annotations

import asyncio
import colorsys
import copy
import os
import threading
from typing import Any

from mcp_servers.tools.devices.base import DeviceError, LightDevice
from mcp_servers.tools.devices.sim import COLORS, LIGHT_TRANSITION_MS, SimLights

try:
    from loguru import logger
except ImportError:  # pragma: no cover
    import logging

    logger = logging.getLogger(__name__)  # type: ignore[assignment]

WHITES_K = {"warm": 2700, "warm white": 2700, "white": 4000, "cool white": 6500, "daylight": 6500}
TIMEOUT_S = 5.0


def bulbs_from_env(text: str | None) -> dict[str, str]:
    """'living room:192.168.1.50, bedroom:192.168.1.51' -> {'living room': '192.168.1.50', ...}."""
    out: dict[str, str] = {}
    for part in (text or "").split(","):
        if ":" in part:
            room, host = part.rsplit(":", 1)
            if room.strip() and host.strip():
                out[room.strip().lower()] = host.strip()
    return out


def to_bulb(light: dict[str, Any]) -> dict[str, Any]:
    """The sim's light dict -> LightState fields for the bulb."""
    if not light["on"]:
        return {"light_on": False}
    st: dict[str, Any] = {"light_on": True, "brightness": max(1, int(light["brightness"]))}
    name = (light.get("color_name") or "").lower()
    if name in WHITES_K:
        st["color_temp"] = WHITES_K[name]
    else:
        hexv = light.get("color") or "#ffd9a0"
        r, g, b = (int(hexv[i : i + 2], 16) / 255 for i in (1, 3, 5))
        h, s, _v = colorsys.rgb_to_hsv(r, g, b)
        st.update(hue=round(h * 360) % 360, saturation=max(1, round(s * 100)), color_temp=0)
    return st


def from_bulb(on: bool, brightness: int | None, hsv: tuple[int, int, int] | None, color_temp: int | None) -> dict[str, Any]:
    """What the bulb reports -> the sim's light fields (on, brightness, color, color_name)."""
    out: dict[str, Any] = {"on": bool(on), "brightness": int(brightness if brightness is not None else 100)}
    if color_temp:
        name = min(("warm white", 2700), ("white", 4000), ("cool white", 6500), key=lambda kv: abs(kv[1] - color_temp))[0]
        out.update(color=COLORS[name], color_name=name)
    elif hsv is not None:
        h, s, _v = hsv
        r, g, b = colorsys.hsv_to_rgb((h % 360) / 360, max(0, min(100, s)) / 100, 1.0)
        hexv = "#%02x%02x%02x" % (round(r * 255), round(g * 255), round(b * 255))
        best, dist = hexv, 1e9
        for cname, cv in COLORS.items():  # the nearest named color, so she can say "it's blue"
            if cname in WHITES_K:
                continue
            ch, cs, _ = colorsys.rgb_to_hsv(*(int(cv[i : i + 2], 16) / 255 for i in (1, 3, 5)))
            d = min(abs(ch * 360 - h), 360 - abs(ch * 360 - h)) + abs(cs * 100 - s) * 0.5
            if d < dist:
                best, dist = cname, d
        out.update(color=hexv, color_name=best if dist < 25 else hexv)
    return out


class TapoLights(LightDevice):
    backend = "tapo"

    def __init__(self, bulbs: dict[str, str], *, username: str | None = None, password: str | None = None,
                 poll_secs: float | None = None, connect: Any = None, start_poller: bool = True, **sim_kwargs: Any):
        if not bulbs:
            raise DeviceError('TAPO_BULBS is empty; set it like TAPO_BULBS="living room:192.168.1.50"')
        self._user = username if username is not None else os.getenv("TAPO_USERNAME", "")
        self._pass = password if password is not None else os.getenv("TAPO_PASSWORD", "")
        if not (self._user and self._pass):
            raise DeviceError("TAPO_USERNAME and TAPO_PASSWORD are needed in .env for the Tapo bulb")
        if connect is None:
            try:
                import kasa  # noqa: F401
            except ImportError as e:
                raise DeviceError("python-kasa is not installed (pip install python-kasa)") from e
            connect = self._kasa_connect
        self._connect = connect
        self._hosts = dict(bulbs)
        self._sim = SimLights(list(bulbs), **sim_kwargs)  # state, persistence and room matching
        self._devs: dict[str, Any] = {}
        self._lock = threading.RLock()
        self._changed = False
        self._poll = float(poll_secs if poll_secs is not None else os.getenv("TAPO_POLL_SECS", "15") or 15)
        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(target=self._loop.run_forever, name="tapo-loop", daemon=True)
        self._loop_thread.start()
        self._stop = threading.Event()
        if start_poller:
            threading.Thread(target=self._poller, name="tapo-poll", daemon=True).start()

    # ------------------------------------------------------------ bulb I/O
    async def _kasa_connect(self, host: str) -> Any:
        from kasa import Credentials, Discover

        dev = await Discover.discover_single(host, credentials=Credentials(self._user, self._pass), timeout=int(TIMEOUT_S))
        if dev is None:
            raise DeviceError("no answer")
        await dev.update()
        return dev

    async def _dev(self, room: str) -> Any:
        dev = self._devs.get(room)
        if dev is None:
            dev = await asyncio.wait_for(self._connect(self._hosts[room]), TIMEOUT_S * 2)
            self._devs[room] = dev
        return dev

    @staticmethod
    def _light_module(dev: Any) -> Any:
        from kasa import Module

        return dev.modules[Module.Light]

    async def _apply(self, room: str, light: dict[str, Any]) -> None:
        from kasa.interfaces.light import LightState

        for attempt in (1, 2):  # a bulb that rebooted or changed session: reconnect once
            try:
                dev = await self._dev(room)
                await asyncio.wait_for(self._light_module(dev).set_state(LightState(**to_bulb(light))), TIMEOUT_S)
                return
            except Exception:
                self._devs.pop(room, None)
                if attempt == 2:
                    raise

    async def _read(self, room: str) -> dict[str, Any]:
        dev = await self._dev(room)
        try:
            await asyncio.wait_for(dev.update(), TIMEOUT_S)
        except Exception:
            self._devs.pop(room, None)
            raise
        lm = self._light_module(dev)
        hsv = tuple(lm.hsv) if lm.has_feature("hsv") else None
        ct = lm.color_temp if lm.has_feature("color_temp") else None
        return from_bulb(dev.is_on, lm.brightness, hsv, ct)  # type: ignore[arg-type]

    def _run(self, coro: Any, timeout: float = TIMEOUT_S * 3) -> Any:
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout)

    # --------------------------------------------------------- interface
    def rooms(self) -> list[str]:
        return self._sim.rooms()

    def set(self, room: str, *, on: bool | None = None, brightness: int | None = None, color: str | None = None) -> dict[str, Any]:
        with self._lock:
            before = copy.deepcopy(self._sim._lights)
            result = self._sim.set(room, on=on, brightness=brightness, color=color)  # validates and computes the new state
            failed, last_err = [], ""
            for light in result["lights"]:
                try:
                    self._run(self._apply(light["room"], light))
                except Exception as e:  # noqa: BLE001
                    last_err = repr(e)
                    logger.warning(f"tapo {light['room']}: {e!r}")
                    failed.append(light["room"])
            if failed:
                for r in failed:  # keep the state honest: the bulbs we could not reach did not change
                    self._sim._lights[r] = before[r]
                self._sim._persist(self._sim.name, self._sim._lights)
                if len(failed) == len(result["lights"]):
                    if "TPAP" in last_err or "Unsupported" in last_err:
                        raise DeviceError("the bulb's firmware uses TP-Link's newer TPAP login, which this python-kasa cannot speak yet")
                    raise DeviceError(f"I couldn't reach the {' and '.join(failed)} light; is it powered and on the same Wi-Fi?")
                result = dict(result, lights=[l for l in result["lights"] if l["room"] not in failed], unreachable=failed)
            return result

    def _poller(self) -> None:
        first = True  # read the bulbs straight away, so the widget starts from the real state
        while first or not self._stop.wait(self._poll):
            first = False
            for room in self.rooms():
                try:
                    fresh = self._run(self._read(room))
                except Exception as e:  # noqa: BLE001
                    logger.debug(f"tapo poll {room}: {e!r}")
                    continue
                with self._lock:
                    cur = self._sim._lights[room]
                    if any(cur.get(k) != v for k, v in fresh.items()):
                        cur.update(fresh)
                        self._sim._persist(self._sim.name, self._sim._lights)
                        self._changed = True

    def tick(self) -> bool:
        with self._lock:
            changed, self._changed = self._changed, False
        return changed

    def status(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._sim.status(), backend=self.backend)

    def close(self) -> None:
        self._stop.set()

        async def _bye():
            for d in self._devs.values():
                try:
                    await d.disconnect()
                except Exception:  # noqa: BLE001
                    pass

        try:
            self._run(_bye(), 3)
        except Exception:  # noqa: BLE001
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)


__all__ = ["TapoLights", "bulbs_from_env", "to_bulb", "from_bulb", "LIGHT_TRANSITION_MS"]


def _check() -> int:
    """python -m mcp_servers.tools.devices.tapo : find bulbs on the network and try the .env login (reads only)."""
    from mcp_servers.common import setup

    setup("tapo-check")
    from kasa import Credentials, Discover

    async def main() -> int:
        async def unsupported(e):
            print(f"  needs a newer protocol: {e}")

        print("Looking for Tapo/Kasa devices on this network (6 s)...")
        found = await Discover.discover(discovery_timeout=6, on_unsupported=unsupported)
        for host, d in found.items():
            print(f"  found {host} {d.model}")
        bulbs = bulbs_from_env(os.getenv("TAPO_BULBS"))
        user, pw = os.getenv("TAPO_USERNAME", ""), os.getenv("TAPO_PASSWORD", "")
        if not (bulbs and user and pw):
            print("Set TAPO_BULBS, TAPO_USERNAME and TAPO_PASSWORD in .env to test the login.")
            return 1
        ok = True
        for room, host in bulbs.items():
            try:
                dev = await Discover.discover_single(host, credentials=Credentials(user, pw), timeout=5)
                await dev.update()
                print(f"  {room} ({host}): logged in, {'on' if dev.is_on else 'off'}, model {dev.model}")
                await dev.disconnect()
            except Exception as e:  # noqa: BLE001
                ok = False
                print(f"  {room} ({host}): FAILED {type(e).__name__}: {str(e)[:200]}")
        return 0 if ok else 1

    return asyncio.run(main())


if __name__ == "__main__":
    raise SystemExit(_check())
