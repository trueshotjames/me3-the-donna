"""Exercise the UI commands of a running assistant over its WebSocket and print what comes back.

    .venv\\Scripts\\python -m benchmark.ui_probe                 # ws://127.0.0.1:8765, all commands
    .venv\\Scripts\\python -m benchmark.ui_probe --port 8768 --only config,summarize
    .venv\\Scripts\\python -m benchmark.ui_probe --calibrate 3   # also run a 3 s + 3 s VAD calibration

Commands sent: config, summarize {last: 20}, refresh, and with --calibrate: vad_calibrate {secs}
then vad_apply with the suggested values (then vad_apply back to the previous values).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time

import websockets


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--only", default="config,summarize,refresh")
    ap.add_argument("--calibrate", type=float, default=0.0)
    a = ap.parse_args()
    only = [c for c in a.only.split(",") if c]
    t0 = time.time()

    def show(msg: dict) -> None:
        kind = msg.get("type")
        body = {k: v for k, v in msg.items() if k not in ("type", "ts")}
        text = json.dumps(body, ensure_ascii=False)
        if kind == "summary":
            rows = body.get("rows") or []
            text = f"turns={body.get('turns')} replied={body.get('replied')} rate_limit_waits={body.get('rate_limit_waits')} rows={len(rows)}"
            for r in rows[:6]:
                text += f"\n      {r['metric']:30} n={r['n']:>3} median={r['median']:>8} p90={r['p90']:>8}"
            if body.get("error"):
                text += f" error={body['error']}"
        elif kind in ("reminders", "alarms"):
            text = f"{len(body.get('items') or [])} items"
        print(f"[{time.time() - t0:6.2f}s] {kind:18} {text[:600]}")

    async with websockets.connect(f"ws://127.0.0.1:{a.port}", max_size=1 << 20) as ws:
        async def pump(seconds: float, until: str | None = None, phase: str | None = None) -> dict | None:
            end = time.time() + seconds
            while time.time() < end:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=max(0.05, end - time.time()))
                except asyncio.TimeoutError:
                    break
                msg = json.loads(raw)
                show(msg)
                if until and msg.get("type") == until and (phase is None or msg.get("phase") == phase):
                    return msg
            return None

        await pump(1.0)
        for cmd in only:
            payload = {"type": cmd}
            if cmd == "summarize":
                payload["last"] = 20
            print(f"\n>>> {payload}")
            await ws.send(json.dumps(payload))
            await pump(3.0)
        if a.calibrate:
            print(f"\n>>> vad_calibrate {a.calibrate}s (stay quiet, then talk)")
            cfg = None
            await ws.send(json.dumps({"type": "config"}))
            cfg = await pump(2.0, until="config")
            await ws.send(json.dumps({"type": "vad_calibrate", "secs": a.calibrate}))
            done = await pump(2 * a.calibrate + 10, until="calibrate", phase="done")
            if done and done.get("result"):
                r = done["result"]
                print(f"\n>>> vad_apply {r['confidence']} / {r['min_volume']}")
                await ws.send(json.dumps({"type": "vad_apply", "confidence": r["confidence"], "min_volume": r["min_volume"]}))
                await pump(2.0)
                if cfg:
                    print(f"\n>>> vad_apply back to {cfg['vad_confidence']} / {cfg['vad_min_volume']}")
                    await ws.send(json.dumps({"type": "vad_apply", "confidence": cfg["vad_confidence"], "min_volume": cfg["vad_min_volume"]}))
                    await pump(2.0)
        print("\ndone")


if __name__ == "__main__":
    asyncio.run(main())
