"""Log Donna into your Telegram account once (PHONE_BACKEND=telegram). Run it yourself in a terminal:

    python -m mcp_servers.tools.devices.telegram_login        # add --check to test the phone too

Telegram asks for your phone number, then sends a login code to your Telegram app (and your 2FA password, if
you set one). It writes data/telegram.session: that file IS your login, keep it private and never commit it.
Log it out any time from Telegram > Settings > Devices.
"""

from __future__ import annotations

import asyncio
import os
import sys

from mcp_servers.common import setup


async def _login(check_phone: bool) -> None:
    from telethon import TelegramClient
    from telethon.tl.functions.contacts import GetContactsRequest

    from mcp_servers.tools.devices.telegram import SESSION, Adb

    api_id, api_hash = os.getenv("TELEGRAM_API_ID", ""), os.getenv("TELEGRAM_API_HASH", "")
    if not api_id or not api_hash:
        sys.exit("Put TELEGRAM_API_ID and TELEGRAM_API_HASH in .env first (from my.telegram.org > API development tools).")
    SESSION.parent.mkdir(exist_ok=True)
    client = TelegramClient(str(SESSION), int(api_id), api_hash)
    await client.start()  # prompts for phone number, code and 2FA password on first run
    me = await client.get_me()
    res = await client(GetContactsRequest(hash=0))
    users = [u for u in res.users if not u.bot and not u.deleted]
    print(f"Logged in as {me.first_name}. {len(users)} Telegram contacts.")
    if check_phone:
        adb = Adb(os.getenv("ADB_PATH"), os.getenv("ADB_SERIAL"))
        found = sum(1 for u in users if adb.call_row(u.id))
        print(f"Phone: {found} of {len(users)} contacts have Telegram's call entry (needed for calls).")
    await client.disconnect()


def main() -> None:
    setup("telegram-login")
    asyncio.run(_login("--check" in sys.argv))


if __name__ == "__main__":
    main()
