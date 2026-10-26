"""Median and p90 of every per-turn timing in data/turns.jsonl.

    .venv\\Scripts\\python -m benchmark.summarize                 # all turns
    .venv\\Scripts\\python -m benchmark.summarize --last 20       # last 20 turns
    .venv\\Scripts\\python -m benchmark.summarize --since 11:30   # turns from 11:30 today
    .venv\\Scripts\\python -m benchmark.summarize --csv out.csv   # also one row per turn
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
import time
from datetime import datetime
from pathlib import Path

DEFAULT = Path(__file__).resolve().parents[1] / "data" / "turns.jsonl"


def _first(calls, kinds):
    return next((c for c in calls if c.get("kind") in kinds), None)


def flatten(r: dict) -> dict:
    """One turn record -> {metric name: number or None}."""
    e2e = r.get("end_to_end") or {}
    asr = r.get("asr") or {}
    llm = (r.get("llm") or {}).get("calls") or []
    split = (r.get("llm") or {}).get("split") or {}
    sents = (r.get("tts") or {}).get("sentences") or []
    tools = r.get("tools") or []
    vad = r.get("vad") or {}
    vstats = vad.get("stats") or {}
    reply = _first(llm, ("normal", "early"))
    follow = _first(llm, ("follow-up",))
    s0 = sents[0] if sents else {}
    barge = r.get("barge_in") or {}
    g = lambda d, k: (d or {}).get(k)  # noqa: E731
    return {
        "e2e.total_ms": e2e.get("total_ms"),
        "e2e.end_of_turn_ms": e2e.get("end_of_turn_ms"),
        "e2e.llm_remaining_ms": e2e.get("llm_remaining_ms"),
        "e2e.tts_first_audio_ms": e2e.get("tts_first_audio_ms"),
        "e2e.speaker_ms": e2e.get("speaker_ms"),
        "asr.flux_decision_ms": asr.get("flux_decision_ms"),
        "asr.delivery_ms": asr.get("delivery_ms"),
        "asr.push_ms.median": g(asr.get("push_ms"), "median"),
        "asr.push_ms.max": g(asr.get("push_ms"), "max"),
        "asr.update_lag_ms.median": g(asr.get("update_lag_ms"), "median"),
        "asr.kept_guess_lead_ms": asr.get("kept_guess_lead_ms"),
        "asr.eager_discarded": asr.get("eager_discarded"),
        "llm.first_token_ms": g(reply, "first_token_ms"),
        "llm.total_ms": g(reply, "total_ms"),
        "llm.tokens_per_s": g(reply, "tokens_per_s"),
        "llm.token_gap_ms.median": g(g(reply, "token_gap_ms"), "median"),
        "llm.followup_first_token_ms": g(follow, "first_token_ms"),
        "llm.tool_decide_ms": split.get("decide_ms"),
        "llm.tools_ms": split.get("tools_ms"),
        "llm.follow_up_ms": split.get("follow_up_ms"),
        "tool.round_trip_ms": tools[0]["round_trip_ms"] if tools else None,
        "tool.exec_ms": tools[0]["exec_ms"] if tools else None,
        "tool.mcp_overhead_ms": tools[0]["mcp_overhead_ms"] if tools else None,
        "tts.first_audio_ms": s0.get("first_audio_ms"),
        "tts.aura_first_byte_ms": s0.get("aura_first_byte_ms"),
        "tts.server_to_client_ms": s0.get("server_to_client_ms"),
        "tts.chunk_gap_ms.median": g(s0.get("chunk_gap_ms"), "median"),
        "tts.rtf": s0.get("rtf"),
        "tts.leading_silence_ms": s0.get("leading_silence_ms"),
        "out.sound_card_latency_ms": (r.get("output") or {}).get("sound_card_latency_ms"),
        "speech.vad_vs_flux_end_ms": (r.get("speech") or {}).get("vad_vs_flux_end_ms"),
        "vad.lead_over_flux_ms": vad.get("lead_over_flux_ms"),
        "vad.median_confidence": vstats.get("median_confidence"),
        "vad.median_loudness": vstats.get("median_loudness"),
        "vad.pct_both_pass": vstats.get("pct_both_pass"),
        "barge.stop_ms": barge.get("stop_ms"),
        "voice.infer_ms": (r.get("voice") or {}).get("infer_ms"),
        "voice.arousal": (r.get("voice") or {}).get("arousal"),
        "voice.dominance": (r.get("voice") or {}).get("dominance"),
        "voice.valence": (r.get("voice") or {}).get("valence"),
    }


def task_summary(tasks: list[dict]) -> list[str]:
    """Delegated Hermes tasks: how many, how they ended, how long they took, how long results waited."""
    if not tasks:
        return []
    done = [t for t in tasks if t.get("state") == "done"]
    lines = [f"\ndelegated tasks: {len(tasks)} ({len(done)} done, "
             f"{sum(t.get('state') == 'failed' for t in tasks)} failed, "
             f"{sum(t.get('state') == 'cancelled' for t in tasks)} cancelled)"]
    for key, label in (("task_ms", "task time (delegate -> result ready)"), ("deliver_ms", "result wait (ready -> queued to speak)")):
        vals = [t[key] for t in done if isinstance(t.get(key), (int, float))]
        if vals:
            lines.append(f"  {label:44} n={len(vals)} median {statistics.median(vals) / 1000:.1f} s p90 {pct(vals, 0.9) / 1000:.1f} s max {max(vals) / 1000:.1f} s")
    tools = [name for t in tasks for name in t.get("tools_used", [])]
    if tools:
        counts = {n: tools.count(n) for n in sorted(set(tools))}
        lines.append("  Hermes tools used: " + ", ".join(f"{n} x{c}" for n, c in counts.items()))
    return lines


def voice_summary(turns: list[dict]) -> list[str]:
    scored = [t["voice"] for t in turns if t.get("voice")]
    if not scored:
        return []
    hinted = [v for v in scored if v.get("hint")]
    lines = [f"\nvoice hints: {len(hinted)} of {len(scored)} turns got a hint ({100 * len(hinted) / len(scored):.0f}%)"]
    for v in hinted[-5:]:
        lines.append(f"  A={v.get('arousal')} D={v.get('dominance')} V={v.get('valence')}: {v['hint']}")
    return lines


def pct(values: list[float], p: float) -> float:
    v = sorted(values)
    k = (len(v) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (k - lo)


def load(path: Path, last: int | None, since: float | None) -> list[dict]:
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if since is not None and (r.get("t") or 0) < since:
                continue
            rows.append(r)
    return rows[-last:] if last else rows


def summarize(recs: list[dict]) -> dict:
    """Median/p90/min/max per metric. Used by the CLI below and by the UI's Settings panel."""
    flat = [flatten(r) for r in recs]
    rows = []
    for key in flat[0] if flat else []:
        vals = [f[key] for f in flat if isinstance(f[key], (int, float)) and not isinstance(f[key], bool)]
        if not vals:
            continue
        rows.append(
            {
                "metric": key,
                "n": len(vals),
                "median": round(statistics.median(vals), 1),
                "p90": round(pct(vals, 0.9), 1),
                "min": round(min(vals), 1),
                "max": round(max(vals), 1),
            }
        )
    waits = [w for r in recs for c in ((r.get("llm") or {}).get("calls") or []) for w in (c.get("waits") or [])]
    return {
        "turns": len(recs),
        "replied": sum(f["e2e.total_ms"] is not None for f in flat),
        "from": time.strftime("%H:%M:%S", time.localtime(recs[0].get("t", 0))) if recs else None,
        "to": time.strftime("%H:%M:%S", time.localtime(recs[-1].get("t", 0))) if recs else None,
        "rate_limit_waits": len(waits),
        "rate_limit_wait_s": round(sum(w.get("retry_after") or 0 for w in waits), 1),
        "rows": rows,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", type=Path, default=DEFAULT)
    ap.add_argument("--last", type=int)
    ap.add_argument("--since", help="HH:MM today")
    ap.add_argument("--csv", type=Path)
    a = ap.parse_args()
    since = None
    if a.since:
        hh, mm = map(int, a.since.split(":"))
        since = datetime.now().replace(hour=hh, minute=mm, second=0, microsecond=0).timestamp()
    rows = load(a.file, a.last, since)
    tasks = [r for r in rows if r.get("kind") == "task"]
    recs = [r for r in rows if r.get("kind") != "task"]
    if not recs:
        print(f"no turns in {a.file}")
        return
    flat = [flatten(r) for r in recs]
    summary = summarize(recs)
    print(f"{summary['turns']} turns ({summary['from']} to {summary['to']}), {summary['replied']} with a full end-to-end measurement")
    if summary["rate_limit_waits"]:
        print(f"LLM rate limit hit {summary['rate_limit_waits']} time(s), {summary['rate_limit_wait_s']} s waited in total")
    print()
    print(f"{'metric':32} {'n':>4} {'median':>9} {'p90':>9} {'min':>9} {'max':>9}")
    for row in summary["rows"]:
        print(f"{row['metric']:32} {row['n']:>4} {row['median']:>9.1f} {row['p90']:>9.1f} {row['min']:>9.1f} {row['max']:>9.1f}")
    for line in task_summary(tasks) + voice_summary(recs):
        print(line)
    if a.csv:
        with open(a.csv, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=["turn", "t", "transcript", *flat[0].keys()])
            w.writeheader()
            for r, f in zip(recs, flat):
                w.writerow({"turn": r.get("turn"), "t": r.get("t"), "transcript": r.get("transcript"), **f})
        print(f"\nwrote {a.csv}")


if __name__ == "__main__":
    main()
