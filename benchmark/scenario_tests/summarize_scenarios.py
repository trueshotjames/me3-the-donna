"""Print every scenario result (from benchmark/audio_replay.py --out) step by step, and write summary.json.

    python summarize_scenarios.py <results folder>
"""

import json
import sys
from pathlib import Path

out = Path(sys.argv[1])
summary = []
for f in sorted(out.glob("*.json")):
    if f.name == "summary.json":
        continue
    d = json.loads(f.read_text(encoding="utf-8"))
    print(f"\n### {d['scenario']} ({f.name})  false_turns={d['false_turns']} self_interrupts={d['self_interruptions']} barge_in_ms={d['barge_in_ms']}")
    steps = []
    for s in d["steps"]:
        row = {
            "said": s["text"],
            "heard": " / ".join(s.get("transcripts") or []),
            "tools": [f"{t['name']}({json.dumps(t['args'], ensure_ascii=False)})" for t in s.get("tools") or []],
            "reply": " ".join(b or "" for b in s.get("bot") or []),
            "e2e_ms": s.get("e2e_ms"),
            "barge_in_stop_ms": s.get("barge_in_stop_ms"),
            "interrupted": bool(s.get("interrupted")),
            "bot_speaking_at_start": s.get("bot_speaking_at_start"),
            "tool_check": s.get("tool_check"),
            "warnings": s.get("warnings"),
        }
        steps.append(row)
        print(f"- SAID: {row['said']}\n  HEARD: {row['heard']}\n  TOOLS: {row['tools']}  check={row['tool_check'] and row['tool_check']['ok']}\n"
              f"  REPLY: {row['reply'][:300]}\n  e2e={row['e2e_ms']} stop={row['barge_in_stop_ms']} interrupted={row['interrupted']} warn={row['warnings']}")
    summary.append({"file": f.name, "scenario": d["scenario"], "steps": steps, "e2e_ms": d["e2e_ms"],
                    "barge_in_ms": d["barge_in_ms"], "false_turns": d["false_turns"], "self_interruptions": d["self_interruptions"]})
(out / "summary.json").write_text(json.dumps(summary, indent=1, ensure_ascii=False), encoding="utf-8")
