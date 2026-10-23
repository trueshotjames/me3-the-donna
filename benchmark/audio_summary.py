"""Summarize an audio_replay results folder into summary.md (one row per scenario)."""

from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path


def _fmt(v, digits=0):
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:.{digits}f}" if digits else f"{round(v)}"
    return str(v)


def _verdict(r: dict) -> tuple[str, str]:
    steps = r["steps"]
    problems = []
    for s in steps:
        tc = s.get("tool_check")
        if tc and not tc["ok"]:
            got = ", ".join(f"{t['name']}({json.dumps(t['args'], ensure_ascii=False)})" for t in s["tools"]) or "no tool"
            problems.append(f"'{s['text'][:30]}': expected {tc['expect']}{json.dumps(tc['args']) if tc['args'] else ''}, got {got}")
        nr = s.get("no_reply_check")
        if nr and not nr["ok"]:
            problems.append(f"replied to side-talk: {' '.join(s['bot'])[:60]!r}")
        if not s["turn_detected"]:
            problems.append(f"no turn for '{s['text'][:30]}'")
    if r["false_turns"]:
        problems.append(f"{len(r['false_turns'])} false turn(s) at {r['false_turns']}")
    if r["self_interruptions"]:
        problems.append(f"{len(r['self_interruptions'])} self-interruption(s) at {r['self_interruptions']}")
    if r.get("alarm") is None and any((s.get("tool_check") or {}).get("expect") == "stop_alarm" for s in steps):
        problems.append("alarm never rang")
    return ("PASS" if not problems else "FAIL"), "; ".join(problems)


def write_summary(folder: Path) -> str:
    folder = Path(folder)
    rows = []
    for f in sorted(folder.glob("*.json")):
        try:
            r = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if "steps" not in r:
            continue
        verdict, why = _verdict(r)
        steps = r["steps"]
        tools = "; ".join(",".join(t["name"] for t in s["tools"]) or "-" for s in steps)
        warns = sum(len(s["warnings"]) for s in steps)
        e2e = r["e2e_ms"]
        heard = " | ".join(" / ".join(s["transcripts"]) or "(nothing)" for s in steps)
        obs = why
        if r.get("alarm"):
            a = r["alarm"]
            obs = (obs + "; " if obs else "") + f"ring->announcement {a['announcement_ms']} ms, stopped at {a['stopped_t']} s (fired {a['fired_t']} s)"
        rows.append(
            "| {sc} | {det}/{n} | {wer} | {e2e} | {barge} | {ft} | {si} | {tools} | {w429} | {warns} | {verdict} | {obs} |".format(
                sc=r["scenario"],
                det=r["turns_detected"],
                n=len(steps),
                wer=_fmt(r["wer_mean"], 2) if r["wer_mean"] is not None else "-",
                e2e=", ".join(_fmt(v) for v in e2e) or "-",
                barge=", ".join(_fmt(v) for v in r["barge_in_ms"]) or "-",
                ft=len(r["false_turns"]),
                si=len(r["self_interruptions"]),
                tools=tools,
                w429=f"{len(r['llm_429_waits'])}" + (f" ({sum(r['llm_429_waits']):.0f} s)" if r["llm_429_waits"] else ""),
                warns=warns,
                verdict=verdict,
                obs=(obs or "ok") + f" — heard: {heard}"[:400],
            )
        )
    all_e2e = []
    for f in sorted(folder.glob("*.json")):
        try:
            all_e2e += json.loads(f.read_text(encoding="utf-8")).get("e2e_ms") or []
        except (OSError, json.JSONDecodeError):
            pass
    head = (
        "| scenario | turns detected | WER | e2e ms | barge-in ms | false turns | self-interruptions | tools | 429 waits | errors/warnings | verdict | observation |\n"
        "|---|---|---|---|---|---|---|---|---|---|---|---|\n"
    )
    tail = ""
    if all_e2e:
        s = sorted(all_e2e)
        tail = f"\nAll turns: e2e n={len(s)} median {statistics.median(s):.0f} ms, p90 {s[min(len(s) - 1, int(0.9 * (len(s) - 1) + 0.999))]:.0f} ms\n"
    text = "# Audio scenario results\n\n" + head + "\n".join(rows) + "\n" + tail
    (folder / "summary.md").write_text(text, encoding="utf-8")
    return text


if __name__ == "__main__":
    print(write_summary(Path(sys.argv[1])))
