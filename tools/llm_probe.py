"""Check the fast model without speaking: does it pick the right tool, and how fast does it start?

    .venv\\Scripts\\python tools\\llm_probe.py                      # model from .env, 3 rounds
    .venv\\Scripts\\python tools\\llm_probe.py --model openai/gpt-oss-20b --reasoning low
    .venv\\Scripts\\python tools\\llm_probe.py --rounds 5 --csv data\\llm_probe.csv

Sends the same system prompt and tools the assistant uses (tools from the tools MCP server, plus
delegate and task_status) with one user request at a time, and checks which tool the model calls.
Nothing is executed: reminders and alarms are not created, Hermes is not called. Keys are read
from .env by assistant.config and never printed.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# (request, expected tool or None for a plain spoken answer)
CASES = [
    ("What time is it?", "get_current_time"),
    ("Remind me to submit ME3 tomorrow at nine.", "create_reminder"),
    ("Set an alarm for seven AM.", "create_alarm"),
    ("What alarms do I have?", "list_alarms"),
    ("Find out tomorrow's weather in Manila and tell me if I need an umbrella.", "delegate"),
    ("What is the latest news about the Philippine peso?", "delegate"),
    ("Compare the price of a Raspberry Pi 5 in three online shops in the Philippines.", "delegate"),
    ("How are you today?", None),
    ("Thanks, that's all.", None),
]


def openai_tools(mcp_tools, extra_schemas) -> list[dict]:
    tools = []
    for t in mcp_tools:
        tools.append({"type": "function", "function": {"name": t.name, "description": t.description or "", "parameters": getattr(t, "input_schema", None) or getattr(t, "inputSchema", None) or {"type": "object", "properties": {}}}})
    for s in extra_schemas:
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": s.name,
                    "description": s.description,
                    "parameters": {"type": "object", "properties": s.properties, "required": s.required},
                },
            }
        )
    return tools


async def ask(client, model: str, messages, tools, extra: dict) -> dict:
    t0 = time.perf_counter()
    first = None
    text, calls, provider = "", {}, None
    stream = await client.chat.completions.create(model=model, messages=messages, tools=tools, stream=True, **extra)
    async for chunk in stream:
        provider = provider or (getattr(chunk, "model_extra", None) or {}).get("provider")  # OpenRouter adds it
        if not chunk.choices:
            continue
        d = chunk.choices[0].delta
        got = bool(d.content) or bool(d.tool_calls)
        if got and first is None:
            first = time.perf_counter() - t0
        if d.content:
            text += d.content
        for tc in d.tool_calls or []:
            c = calls.setdefault(tc.index, {"name": "", "args": ""})
            if tc.function and tc.function.name:
                c["name"] += tc.function.name
            if tc.function and tc.function.arguments:
                c["args"] += tc.function.arguments
    total = time.perf_counter() - t0
    names = [c["name"] for c in calls.values()]
    return {"first_ms": round((first or total) * 1000, 1), "total_ms": round(total * 1000, 1), "tools": names, "calls": list(calls.values()), "text": text.strip(), "provider": provider}


async def main() -> int:
    from openai import AsyncOpenAI

    from assistant.config import Config
    from assistant.main import connect_tools_server
    from assistant.prompts import system_prompt
    from assistant.tasks import DELEGATE_SCHEMA, TASK_STATUS_SCHEMA
    from mcp_servers.tools.catalog import LLM_TOOLS

    ap = argparse.ArgumentParser()
    ap.add_argument("--model", help="override LLM_MODEL")
    ap.add_argument("--reasoning", help="override LLM_REASONING_EFFORT ('' to send none)")
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--csv", type=Path)
    a = ap.parse_args()

    cfg = Config()
    model = a.model or cfg.llm_model
    effort = cfg.llm_reasoning_effort if a.reasoning is None else a.reasoning
    if "gpt-oss" in model and effort.lower() in ("", "none"):
        effort = "low"
    extra: dict = {"max_tokens": cfg.llm_max_tokens, "temperature": cfg.llm_temperature}
    if effort:
        extra["reasoning_effort"] = effort
    if cfg.llm_extra_body:
        extra["extra_body"] = cfg.llm_extra_body

    bridge, proc = await connect_tools_server(cfg)
    try:
        listed = await bridge._session.list_tools()  # noqa: SLF001 - schemas, not just names
        mcp_tools = [t for t in listed.tools if t.name in LLM_TOOLS]
    finally:
        await bridge.close()
        if proc is not None:
            proc.terminate()
    tools = openai_tools(mcp_tools, [DELEGATE_SCHEMA, TASK_STATUS_SCHEMA])
    prompt = system_prompt(cfg.assistant_name, cfg.user_name, cfg.engagement_window_secs, delegate=True)
    client = AsyncOpenAI(api_key=cfg.llm_api_key, base_url=cfg.llm_base_url)

    print(f"model {model} at {cfg.llm_base_url}, reasoning_effort={effort or '(not sent)'}, {cfg.llm_extra_body or ''}")
    print(f"{len(tools)} tools, {args_rounds(a.rounds)}\n")
    rows, ok_count, firsts = [], 0, []
    for rnd in range(1, a.rounds + 1):
        for req, want in CASES:
            messages = [{"role": "system", "content": prompt}, {"role": "user", "content": req}]
            try:
                r = await ask(client, model, messages, tools, extra)
            except Exception as e:  # noqa: BLE001
                print(f"[{rnd}] ERROR {req!r}: {e}")
                rows.append({"round": rnd, "request": req, "expected": want, "got": "ERROR", "ok": False, "first_ms": None, "total_ms": None, "provider": None, "detail": str(e)[:200]})
                continue
            got = r["tools"][0] if r["tools"] else None
            ok = got == want
            ok_count += ok
            firsts.append(r["first_ms"])
            detail = json.dumps(r["calls"][0]["args"])[:90] if r["calls"] else repr(r["text"][:90])
            print(f"[{rnd}] {'ok  ' if ok else 'MISS'} {r['first_ms']:>7.0f} ms first, {r['total_ms']:>7.0f} ms total  {req[:52]:52} -> {got or 'speaks'} {detail}" + (f"  [{r['provider']}]" if r["provider"] else ""))
            rows.append({"round": rnd, "request": req, "expected": want, "got": got, "ok": ok, **{k: r[k] for k in ("first_ms", "total_ms", "provider")}, "detail": detail})
            await asyncio.sleep(0.3)
    n = len(rows)
    print(f"\ntool choice correct: {ok_count}/{n}")
    if firsts:
        firsts.sort()
        p90 = firsts[min(len(firsts) - 1, int(0.9 * (len(firsts) - 1) + 0.5))]
        print(f"first token: median {statistics.median(firsts):.0f} ms, p90 {p90:.0f} ms, min {firsts[0]:.0f} ms, max {firsts[-1]:.0f} ms")
    if a.csv:
        with open(a.csv, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        print(f"wrote {a.csv}")
    await client.close()
    return 0 if ok_count == n else 1


def args_rounds(n: int) -> str:
    return f"{len(CASES)} requests x {n} round{'s' if n != 1 else ''}"


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
