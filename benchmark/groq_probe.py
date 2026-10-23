"""Quick Groq check: does the model exist, does it call tools, and how fast is the first spoken word.

    .venv\\Scripts\\python -m benchmark.groq_probe            # model from .env
    .venv\\Scripts\\python -m benchmark.groq_probe --compare  # also gpt-oss-20b (low) for reference
"""
from __future__ import annotations

import argparse
import json
import statistics
import time

from openai import OpenAI

from assistant.config import load
from assistant.prompts import system_prompt

TOOLS = [{
    "type": "function",
    "function": {
        "name": "create_alarm",
        "description": "Set an alarm.",
        "parameters": {"type": "object", "properties": {"time": {"type": "string", "description": "HH:MM 24h"},
                                                        "label": {"type": "string"}}, "required": ["time"]},
    },
}]
QUESTIONS = ["What is the capital of the Philippines?", "Hello? Can you hear me?", "Tell me a short story."]


def run(client, model, effort, messages, tools=None):
    kw = {"reasoning_effort": effort} if effort else {}
    t0 = time.perf_counter()
    first = None
    text, tool = "", None
    stream = client.chat.completions.create(model=model, messages=messages, stream=True, max_tokens=300,
                                            temperature=0.4, tools=tools, **kw)
    for chunk in stream:
        if not chunk.choices:
            continue
        d = chunk.choices[0].delta
        if (d.content or d.tool_calls) and first is None:
            first = time.perf_counter() - t0
        if d.content:
            text += d.content
        if d.tool_calls:
            tool = (tool or "") + "".join(tc.function.name or "" for tc in d.tool_calls if tc.function)
    return first, time.perf_counter() - t0, text.strip(), tool


def probe(client, cfg, model, effort):
    print(f"\n== {model}  reasoning_effort={effort or '(unset)'}")
    sysmsg = {"role": "system", "content": system_prompt(cfg.assistant_name, cfg.user_name, cfg.engagement_window_secs)}
    firsts = []
    for q in QUESTIONS * 2:
        try:
            f, total, text, _ = run(client, model, effort, [sysmsg, {"role": "user", "content": q}])
        except Exception as e:  # noqa: BLE001
            print(f"   FAILED: {e}")
            return
        firsts.append(f)
        print(f"   first word {f*1000:5.0f} ms | done {total*1000:5.0f} ms | {q!r} -> {text[:70]!r}")
    print(f"   median first word: {statistics.median(firsts)*1000:.0f} ms")
    f, total, text, tool = run(client, model, effort, [sysmsg, {"role": "user", "content": "Set an alarm for six AM."}], TOOLS)
    print(f"   tool test: {'called ' + tool if tool else 'NO TOOL CALL'} after {f*1000:.0f} ms {text[:60]!r}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--compare", action="store_true")
    args = ap.parse_args()
    cfg = load()
    client = OpenAI(api_key=cfg.groq_api_key, base_url=cfg.llm_base_url)
    probe(client, cfg, cfg.llm_model, cfg.llm_reasoning_effort)
    if args.compare:
        probe(client, cfg, "openai/gpt-oss-20b", "low")


if __name__ == "__main__":
    main()
