"""`donna status`: is she running, what she last reported, and can the Pi reach Deepgram and the LLM right now.

    python -m assistant.status
Exit code 0 when she is live and both providers answer, 1 otherwise.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import time

from assistant.config import DATA_DIR, load
from assistant.cues import STATUS_FILE_NAME


def _service_state() -> str:
    if not shutil.which("systemctl"):
        return "n/a (no systemd)"
    try:
        out = subprocess.run(["systemctl", "--user", "is-active", "donna"], capture_output=True, text=True, timeout=5)
        return out.stdout.strip() or out.stderr.strip()
    except Exception as e:  # noqa: BLE001
        return f"unknown ({e.__class__.__name__})"


def _ago(t: float | None) -> str:
    if not t:
        return "?"
    s = int(time.time() - t)
    return f"{s} s ago" if s < 120 else f"{s // 60} min ago"


async def _probe(cfg) -> list[tuple[str, dict]]:
    from assistant.netstatus import NetMonitor, targets_from

    mon = NetMonitor(None, targets_from(cfg))
    results = await asyncio.gather(*(mon.probe(t) for t in mon.targets))
    return [(t.label + " " + t.host, r) for t, r in zip(mon.targets, results)]


def main() -> None:
    cfg = load()
    service = _service_state()
    try:
        st = json.loads((DATA_DIR / STATUS_FILE_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        st = {}
    print(f"service : {service}")
    if st:
        print(f"donna   : {st.get('state', '?').upper()} since {_ago(st.get('since'))}"
              + (f" - {st['detail']}" if st.get("detail") else ""))
    else:
        print("donna   : no status yet")
    ok = (service == "active" or service.startswith("n/a")) and st.get("state") == "live"
    for name, r in asyncio.run(_probe(cfg)):
        if r["ok"]:
            print(f"net     : OK   {name}  tcp {r['tcp_ms']} ms, tls {r['tls_ms']} ms")
        else:
            ok = False
            print(f"net     : DOWN {name}  {r['error']}")
    print("verdict : " + ("she is live, talk to her" if ok else "NOT ready - see `donna logs`"))
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
