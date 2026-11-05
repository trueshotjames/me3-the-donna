"""Telegram as Donna's phone (PHONE_BACKEND=telegram): free, works on the laptop and on the Pi.

- Messages go out from your own Telegram account through Telethon (the Telegram client API, not a bot), so
  Donna can text any of your Telegram contacts and they see it from you.
- Calls start on your Android phone over ADB: Telegram puts a "Voice call" entry on each synced contact in
  Android's contacts, and opening that entry starts the call without a tap. You talk on the phone; Donna only
  sets the call up. (Telegram Desktop has no such hook, and the Pi has no Telegram app, so the phone it is.)

Setup (once): see devices/README.md, "Telegram". In short:
    pip install telethon                       # pure Python, also on the Pi
    .env: TELEGRAM_API_ID, TELEGRAM_API_HASH   # from my.telegram.org > API development tools (you paste them)
    python -m mcp_servers.tools.devices.telegram_login    # log in once; writes data/telegram.session (secret)
    phone: USB debugging on, plugged in (or `adb connect`); Telegram > Settings > Privacy > Sync Contacts on
Optional:
    TELEGRAM_CONTACTS="Mom:Maria Santos,Bro:@kingsbrother"   # short names for Telegram contacts or usernames
    TELEGRAM_CALLS=desktop | adb   # desktop (Windows default): Donna presses Call in Telegram Desktop here
    TELEGRAM_ALLOWED="Love"   # only these contacts can be messaged or called (unset: all contacts)
    ADB_PATH (default: adb on PATH)   ADB_SERIAL (when more than one phone is connected)

The phone state (call, recent messages) keeps the sim's shape, so the UI widget and the LLM see the same fields.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

try:
    from loguru import logger
except ImportError:  # pragma: no cover
    import logging

    logger = logging.getLogger(__name__)  # type: ignore[assignment]

from mcp_servers.tools.devices.base import DeviceError, PhoneDevice
from mcp_servers.tools.devices.sim import SimPhone

CALL_MIME = "vnd.android.cursor.item/vnd.org.telegram.messenger.android.call"  # Telegram's ContactsController
SESSION = Path(__file__).resolve().parents[3] / "data" / "telegram"  # Telethon adds .session
STALE_CALL_SECS = 15 * 60


class _Loop:
    """One asyncio loop in a daemon thread for Telethon, so the tools server's own loop never waits on it."""

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, name="telegram", daemon=True).start()

    def run(self, coro, timeout: float = 10):
        try:
            return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)
        except TimeoutError as e:
            raise DeviceError("Telegram did not answer in time; check the internet connection") from e


class Adb:
    """The Android phone over ADB (USB or `adb connect <ip>:<port>`)."""

    def __init__(self, path: str | None = None, serial: str | None = None):
        path = path or "adb"
        self.path = shutil.which(path) or (path if Path(path).exists() else None)
        if not self.path and path == "adb":  # not on PATH and no ADB_PATH: try ~/Tools/platform-tools
            home_adb = Path.home() / "Tools" / "platform-tools" / ("adb.exe" if os.name == "nt" else "adb")
            self.path = str(home_adb) if home_adb.exists() else None
        self.serial = serial or None

    def _run(self, *args: str, timeout: float = 8) -> str:
        if not self.path:
            raise DeviceError("ADB is not installed, so I cannot start calls on your phone")
        cmd = [self.path] + (["-s", self.serial] if self.serial else []) + list(args)
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired as e:
            raise DeviceError("your phone did not answer; check the USB cable or Wi-Fi debugging") from e
        out = (p.stdout or "") + (p.stderr or "")
        if p.returncode != 0:
            if "no devices" in out or "not found" in out or "offline" in out or "unauthorized" in out:
                raise DeviceError("your phone is not connected; plug it in with USB debugging on and allow this computer")
            raise DeviceError(f"the phone refused the command: {out.strip()[:200]}")
        return out

    def call_row(self, user_id: int) -> str | None:
        """The _id of the contact entry "Voice call" that Telegram added for this Telegram user, if synced."""
        out = self._run(
            "shell",
            f"content query --uri content://com.android.contacts/data --projection _id:data1 "
            f"--where \"mimetype='{CALL_MIME}' AND data1='{int(user_id)}'\"",
        )
        m = re.search(r"_id=(\d+)", out)
        return m.group(1) if m else None

    def start_call(self, row_id: str) -> None:
        self._run("shell", "input keyevent KEYCODE_WAKEUP")
        out = self._run("shell", f"am start -a android.intent.action.VIEW -t {CALL_MIME} -d content://com.android.contacts/data/{int(row_id)}")
        if "Error" in out or "Exception" in out:
            raise DeviceError(f"Telegram on the phone did not start the call: {out.strip()[:200]}")


class DesktopCaller:
    """Telegram Desktop on this computer (Windows): opens the contact's chat with a tg:// link, then presses its
    "Call" button through UI Automation. The call rings from the desktop app and the user talks on this computer."""

    WAIT_SECS = 4.0

    def __init__(self):
        if os.name != "nt":
            raise DeviceError("Telegram Desktop calls are only set up on Windows (TELEGRAM_CALLS=desktop)")
        try:
            import uiautomation  # noqa: F401
        except ImportError as e:
            raise DeviceError("uiautomation is not installed (pip install uiautomation)") from e

    @staticmethod
    def link_for(user: Any) -> str:
        if getattr(user, "username", None):
            return f"tg://resolve?domain={user.username}"
        if getattr(user, "phone", None):
            return f"tg://resolve?phone={user.phone}"
        return f"tg://user?id={int(user.id)}"

    @staticmethod
    def _exe(pid: int) -> str:
        """The executable name of a process (Windows), '' if it cannot be read."""
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(1024)
            ok = k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
            return Path(buf.value).name.lower() if ok else ""
        finally:
            k32.CloseHandle(h)

    @classmethod
    def _windows(cls, auto: Any) -> list[Any]:
        """Telegram Desktop's top-level windows (main window, call panel)."""
        return [w for w in auto.GetRootControl().GetChildren() if cls._exe(w.ProcessId) == "telegram.exe"]

    @staticmethod
    def _title(w: Any) -> str:
        return (w.Name or "").replace("‎", "").replace("‏", "").strip()

    def find_call_button(self, contact: str, wait: float | None = None) -> Any:
        """Wait until Telegram's main window shows `contact`'s chat and return its Call button (not pressed)."""
        import uiautomation as auto

        deadline = time.time() + (self.WAIT_SECS if wait is None else wait)
        seen_window = False
        while True:
            for w in self._windows(auto):
                seen_window = True
                if not self._title(w).lower().startswith(contact.lower() + " "):
                    continue  # the title is "<chat name> – (unread)"
                btn = w.ButtonControl(Name="Call", searchDepth=12)
                if btn.Exists(0, 0) and str(btn.AutomationId).endswith("TopBarWidget.class Ui::IconButton"):
                    return btn
            if time.time() >= deadline:
                break
            time.sleep(0.2)
        if not seen_window:
            raise DeviceError("Telegram Desktop is not open on this computer; open it and log in, then try again")
        raise DeviceError(f"Telegram Desktop did not show a Call button for {contact}")

    # Telegram Desktop 7.3 (seen 2026-11-05): the chat's "Call" button opens a separate call window (class
    # Ui::RpWindow, title = the contact) whose buttons are Ui::CallButton: first "Start Call" / "Cancel" /
    # "Start Video"; the call only rings after "Start Call". Hang-up and answer are matched by name.
    START_NAMES = ("start call",)
    IDLE_NAMES = ("start call", "call again")  # the window is open but no call is ringing or connected
    HANGUP_RE = re.compile(r"end|hang|cancel|decline|discard", re.I)
    ANSWER_RE = re.compile(r"accept|answer", re.I)

    @staticmethod
    def _call_buttons(w: Any) -> dict[str, Any]:
        """The Ui::CallButton buttons of a window, by lowercase name."""
        import uiautomation as auto

        out: dict[str, Any] = {}
        for c, _depth in auto.WalkControl(w, maxDepth=14):
            if c.ControlTypeName == "ButtonControl" and str(c.AutomationId).endswith("Ui::CallButton") and c.Name:
                out[c.Name.strip().lower()] = c
        return out

    def _call_window(self, auto: Any) -> tuple[Any, dict[str, Any]] | None:
        """Telegram Desktop's call window and its call buttons, if one is open."""
        for w in self._windows(auto):
            if w.ClassName == "class MainWindow":
                continue
            buttons = self._call_buttons(w)
            if buttons:
                return w, buttons
        return None

    def start_call(self, user: Any, contact: str, *, dry_run: bool = False) -> str:
        """Open the chat, press Call, then "Start Call" in the call window. dry_run stops before pressing Call."""
        import uiautomation as auto

        with auto.UIAutomationInitializerInThread():
            os.startfile(self.link_for(user))  # type: ignore[attr-defined]  # Windows only
            btn = self.find_call_button(contact)
            if dry_run:
                return btn.Name
            btn.GetInvokePattern().Invoke()
            deadline = time.time() + self.WAIT_SECS
            while time.time() < deadline:
                found = self._call_window(auto)
                if found:
                    w, buttons = found
                    start = next((buttons[n] for n in self.START_NAMES if n in buttons), None)
                    if start is not None:
                        start.GetInvokePattern().Invoke()
                        return "Start Call"
                    return "Call"  # the window went straight to ringing
                time.sleep(0.2)
            raise DeviceError("Telegram Desktop did not open its call window")

    def in_call(self) -> bool | None:
        """Is a call ringing or connected in Telegram Desktop? None when it cannot tell."""
        import uiautomation as auto

        try:
            with auto.UIAutomationInitializerInThread():
                found = self._call_window(auto)
                if not found:
                    return False
                return not any(n in found[1] for n in self.IDLE_NAMES)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"phone: Telegram call window check failed: {e!r}")
            return None

    def answer(self) -> str | None:
        """Press Accept on an incoming Telegram call, if one is showing. Returns the caller (window title) or None."""
        import uiautomation as auto

        with auto.UIAutomationInitializerInThread():
            found = self._call_window(auto)
            if not found:
                return None
            w, buttons = found
            for name, btn in buttons.items():
                if self.ANSWER_RE.search(name):
                    btn.GetInvokePattern().Invoke()
                    return self._title(w) or "incoming call"
        return None

    def end_call(self) -> bool:
        """Press the call window's hang-up / cancel button. True when pressed."""
        import uiautomation as auto

        with auto.UIAutomationInitializerInThread():
            found = self._call_window(auto)
            if not found:
                return False
            for name, btn in found[1].items():
                if self.HANGUP_RE.search(name):
                    btn.GetInvokePattern().Invoke()
                    return True
        return False

class TelegramPhone(PhoneDevice):
    backend = "telegram"

    def __init__(self, api_id: str, api_hash: str, aliases: dict[str, str] | None = None, *, adb: Adb | None = None, **sim_kwargs: Any):
        try:
            import telethon  # noqa: F401
        except ImportError as e:
            raise DeviceError("telethon is not installed (pip install telethon)") from e
        if not api_id or not api_hash:
            raise DeviceError("TELEGRAM_API_ID and TELEGRAM_API_HASH are needed for the telegram phone backend")
        if not SESSION.with_suffix(".session").exists():
            raise DeviceError("Telegram is not logged in yet: run python -m mcp_servers.tools.devices.telegram_login")
        self._api_id, self._api_hash = int(api_id), api_hash
        self._aliases = aliases or {}
        self._calls = (os.getenv("TELEGRAM_CALLS") or ("desktop" if os.name == "nt" else "adb")).strip().lower()
        self._adb = adb or Adb(os.getenv("ADB_PATH"), os.getenv("ADB_SERIAL"))
        self._desktop: DesktopCaller | None = None
        if self._calls == "desktop":
            try:
                self._desktop = DesktopCaller()
            except DeviceError as e:
                logger.warning(f"phone: Telegram Desktop calls unavailable: {e}")
        self._jabra = None  # the speakerphone's green/red phone buttons (jabra.py)
        if os.getenv("JABRA_BUTTONS", "1").strip() != "0":
            try:
                from mcp_servers.tools.devices.jabra import JabraButtons

                self._jabra = JabraButtons(self._on_jabra_hook)
                self._jabra.set_leds()
                logger.info(f"phone: {self._jabra.name} buttons: red hangs up, green answers")
            except Exception as e:  # noqa: BLE001
                logger.info(f"phone: no Jabra phone buttons ({e})")
        self._users: dict[str, Any] = {}  # contact name -> Telethon User
        self._allowed = {n.strip().lower() for n in (os.getenv("TELEGRAM_ALLOWED") or "").split(",") if n.strip()}
        self._blocked: set[str] = set()  # lowercase names of contacts not on TELEGRAM_ALLOWED
        self._call: dict[str, Any] | None = None
        self._call_handle = None  # InputPhoneCall, when Telegram tells this session about the call
        self._dirty = False
        self._last_window_check = 0.0
        self._loop = _Loop()
        self._client = None
        self._loop.run(self._start(), timeout=25)
        self._sim = SimPhone(list(self._users), **sim_kwargs)  # name matching and the message log
        logger.info(f"phone: Telegram as {self._me}, {len(self._users)} contacts, calls via {self._calls}" + (f", adb {'found' if self._adb.path else 'missing'}" if self._calls == "adb" else ""))

    # ------------------------------------------------------------ Telethon side (runs in the telegram thread)

    async def _start(self) -> None:
        from telethon import TelegramClient, events
        from telethon.tl import types
        from telethon.tl.functions.contacts import GetContactsRequest

        client = TelegramClient(str(SESSION), self._api_id, self._api_hash)
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            raise DeviceError("Telegram is not logged in yet: run python -m mcp_servers.tools.devices.telegram_login")
        self._client = client
        me = await client.get_me()
        self._me = " ".join(x for x in (me.first_name, me.last_name) if x) or "you"
        res = await client(GetContactsRequest(hash=0))
        by_name: dict[str, Any] = {}
        for u in res.users:
            name = " ".join(x for x in (u.first_name, u.last_name) if x).strip()
            if name and not u.bot and not u.deleted:
                by_name[name] = u
        for alias, target in self._aliases.items():  # "Mom:Maria Santos" or "Bro:@username"
            t = target.strip()
            u = None
            if t.startswith("@"):
                try:
                    u = await client.get_entity(t)
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"phone: alias {alias!r}: no Telegram user {t}: {e!r}")
            else:
                u = next((v for k, v in by_name.items() if k.lower() == t.lower()), None)
                if u is None:
                    logger.warning(f"phone: alias {alias!r}: {t!r} is not in your Telegram contacts")
            if u is not None:
                by_name[alias.strip()] = u
        if self._allowed:  # TELEGRAM_ALLOWED: only these contacts (and aliases pointing at them) can be reached
            ok_ids = {u.id for k, u in by_name.items() if k.lower() in self._allowed}
            self._blocked = {k.lower() for k, u in by_name.items() if u.id not in ok_ids}
            by_name = {k: u for k, u in by_name.items() if u.id in ok_ids}
        self._users = by_name

        @client.on(events.Raw(types.UpdatePhoneCall))
        async def _on_call(update):  # noqa: ANN001
            self._on_phone_call(update.phone_call)

    def _on_phone_call(self, pc: Any) -> None:
        from telethon.tl import types

        call = self._call
        if call is None or call["state"] == "ended":
            return
        if hasattr(pc, "access_hash"):
            self._call_handle = types.InputPhoneCall(id=pc.id, access_hash=pc.access_hash)
        now = time.time()
        if isinstance(pc, types.PhoneCall) and call["state"] != "connected":
            call["state"], call["connected_at"] = "connected", now
        elif isinstance(pc, types.PhoneCallDiscarded):
            call["state"], call["ended_at"] = "ended", now
            call["duration_s"] = int(getattr(pc, "duration", 0) or (now - call["connected_at"] if call.get("connected_at") else 0))
            self._call_handle = None
        self._dirty = True

    # ------------------------------------------------------------------- PhoneDevice

    def contacts(self) -> list[str]:
        return self._sim.contacts()

    def _resolve(self, contact: str) -> str:
        """Strict on purpose (these are real people): the full name, an alias, or a first name only one contact has."""
        c = (contact or "").strip().lower()
        for prefix in ("my ", "to "):
            if c.startswith(prefix):
                c = c[len(prefix):]
        if not c:
            raise DeviceError("who should I contact?")
        blocked = [n for n in self._blocked if n == c or n.split()[0] == c]
        if blocked and not any(n.lower() == c or n.lower().split()[0] == c for n in self.contacts()):
            raise DeviceError(f"{contact.strip()} is not on your allowed list, so I can't message or call them")
        names = self.contacts()
        exact = [n for n in names if n.lower() == c]
        if exact:
            return exact[0]
        first = [n for n in names if n.lower().split()[0] == c]
        if len(first) == 1:
            return first[0]
        close = first or [n for n in names if c in n.lower()]
        if close:
            raise DeviceError(f"which one: {', '.join(close[:4])}?")
        raise DeviceError(f"I can't find {contact!r} in your Telegram contacts")

    def send_message(self, contact: str, text: str) -> dict[str, Any]:
        name = self._resolve(contact)
        body = re.sub(r"[   ]", " ", text or "").strip()  # the LLM sometimes writes no-break spaces
        if not body:
            raise DeviceError("what should the message say?")
        t0 = time.time()
        try:
            self._loop.run(self._client.send_message(self._users[name], body), timeout=12)
        except DeviceError:
            raise
        except Exception as e:  # noqa: BLE001
            raise DeviceError(f"Telegram did not accept the message: {e}") from e
        result = self._sim.send_message(name, body)  # recorded only once Telegram took it
        result["message"]["state"] = "delivered"
        result["note"] = f"sent on Telegram in {round((time.time() - t0) * 1000)} ms"
        return result

    def call(self, contact: str) -> dict[str, Any]:
        name = self._resolve(contact)
        cur = self._call
        if cur and cur["state"] in ("ringing", "connected") and time.time() - cur["started_at"] < STALE_CALL_SECS:
            raise DeviceError(f"already on a call with {cur['contact']}; say hang up first")
        if self._calls == "desktop":
            if self._desktop is None:
                raise DeviceError("Telegram Desktop calls are not available on this computer")
            self._desktop.start_call(self._users[name], name)
            self._call_handle = None
            self._call = {"contact": name, "state": "ringing", "started_at": time.time(), "connected_at": None, "ended_at": None, "duration_s": 0}
            if self._jabra is not None:
                self._jabra.set_leds(off_hook=True)  # now the red button sends "hang up"
            return {"call": dict(self._call), "note": "Telegram call started in Telegram Desktop on this computer; the user talks there"}
        row = self._adb.call_row(self._users[name].id)
        if row is None:
            raise DeviceError(
                f"{name} has no Telegram call entry on your phone: save them in the phone's contacts and turn on "
                "Sync Contacts in Telegram's privacy settings"
            )
        self._adb.start_call(row)
        self._call_handle = None
        self._call = {"contact": name, "state": "ringing", "started_at": time.time(), "connected_at": None, "ended_at": None, "duration_s": 0}
        return {"call": dict(self._call), "note": "Telegram call started on the user's phone; they talk on the phone"}

    def hang_up(self) -> dict[str, Any]:
        call = self._call
        if not call or call["state"] == "ended":
            raise DeviceError("there is no call to end")
        if self._call_handle is None:
            if self._desktop is not None and self._desktop.end_call():
                self._call_ended()
                return {"call": dict(call)}
            raise DeviceError("I cannot end this Telegram call from here; please hang up in Telegram")
        from telethon.tl import types
        from telethon.tl.functions.phone import DiscardCallRequest

        dur = int(time.time() - call["connected_at"]) if call.get("connected_at") else 0
        try:
            self._loop.run(self._client(DiscardCallRequest(peer=self._call_handle, duration=dur, reason=types.PhoneCallDiscardReasonHangup(), connection_id=0)))
        except DeviceError:
            raise
        except Exception as e:  # noqa: BLE001
            raise DeviceError(f"Telegram did not end the call ({e}); please hang up on your phone") from e
        call["state"], call["ended_at"], call["duration_s"] = "ended", time.time(), dur
        self._call_handle = None
        if self._jabra is not None:
            self._jabra.set_leds()
        return {"call": dict(call)}

    def _call_ended(self) -> None:
        """Mark the current call ended (hung up here, or Telegram Desktop's call window closed)."""
        call = self._call
        if call and call["state"] != "ended":
            now = time.time()
            call["state"], call["ended_at"] = "ended", now
            call["duration_s"] = int(now - call["connected_at"]) if call.get("connected_at") else 0
        self._call_handle = None
        self._dirty = True
        if self._jabra is not None:
            self._jabra.set_leds()

    def _on_jabra_hook(self, off_hook: bool) -> None:
        """Jabra buttons (listener thread): red during a call hangs up; green with no call answers an incoming one."""
        call = self._call
        active = bool(call and call["state"] in ("ringing", "connected"))
        if not off_hook and active:
            logger.info("phone: Jabra red button: hanging up")
            try:
                self.hang_up()
            except DeviceError as e:
                logger.warning(f"phone: Jabra hang up failed: {e}")
        elif off_hook and not active and self._desktop is not None:
            name = self._desktop.answer()
            if name:
                logger.info("phone: Jabra green button: answered the incoming Telegram call")
                self._call = {"contact": name, "state": "connected", "started_at": time.time(), "connected_at": time.time(), "ended_at": None, "duration_s": 0}
                self._dirty = True
            elif self._jabra is not None:
                self._jabra.set_leds()  # nothing to answer: back to idle

    def tick(self) -> bool:
        changed = self._sim.tick()
        call = self._call
        if (call and call["state"] in ("ringing", "connected") and self._desktop is not None
                and time.time() - call["started_at"] > 4 and time.time() - self._last_window_check > 1.5):
            self._last_window_check = time.time()
            if self._desktop.in_call() is False:
                logger.info("phone: Telegram Desktop's call window closed: the call ended")
                self._call_ended()
        if call and call["state"] in ("ringing", "connected") and time.time() - call["started_at"] > STALE_CALL_SECS:
            call["state"], call["ended_at"] = "ended", time.time()  # Telegram never told us; do not show it forever
            self._dirty = True
        if self._dirty:
            self._dirty = False
            changed = True
        return changed

    def status(self) -> dict[str, Any]:
        call = self._call
        if call and call["state"] == "connected" and call.get("connected_at"):
            call = dict(call, duration_s=int(time.time() - call["connected_at"]))
        return dict(self._sim.status(), backend=self.backend, call=call)

    def close(self) -> None:
        if getattr(self, "_jabra", None) is not None:
            self._jabra.close()
        if self._client is not None:
            try:
                self._loop.run(self._client.disconnect(), timeout=5)
            except Exception:  # noqa: BLE001
                pass


def aliases_from_env(text: str | None) -> dict[str, str]:
    """'Mom:Maria Santos,Bro:@kingsbrother' -> {'Mom': 'Maria Santos', 'Bro': '@kingsbrother'}."""
    out: dict[str, str] = {}
    for part in (text or "").split(","):
        if ":" in part:
            name, target = part.split(":", 1)
            if name.strip() and target.strip():
                out[name.strip()] = target.strip()
    return out
