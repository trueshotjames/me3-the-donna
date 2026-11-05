# Benchmarks

All scripts run from the project root with the project's venv (`.venv/bin/python` on the Pi,
`.venv\Scripts\python` on Windows). Run outputs (`audio_results/`, `replay_results/`,
`scenario_tests/results/`) stay local and are gitignored.

## Latency, from real use

Every turn writes its timings to `data/turns.jsonl` and `data/latency.jsonl` (end of your speech, first LLM
token, first audio out, and the steps between). Summarize median and p90:

```bash
python -m benchmark.summarize                 # all turns
python -m benchmark.summarize --last 20       # last 20 turns (the repeatable 20-run measurement)
python -m benchmark.summarize --csv out.csv   # one row per turn too
```

Repeatable method: same room, Donna calibrated (`python -m assistant.calibrate`), Ethernet, 20 turns from
`audio/prompts/` or spoken live, then `summarize --last 20`. Target: median and p90 under 1 s.

## Scripted runs

| script | what |
|---|---|
| `audio_replay.py` | real pipeline with the mic replaced by WAVs (noise, gain, echo optional); `audio_scenarios/*.json` |
| `audio_summary.py` | `summary.md` for an `audio_replay` results folder |
| `replay.py` | text replay through the LLM, tools and TTS (no ASR); `scenarios/*.json` |
| `scenario_tests/` | the conversation scenarios and tool-choice tests (its own README); `run_scenarios.ps1` is Windows-only, on the Pi run `audio_replay.py` per file |
| `groq_probe.py` | is the LLM model up, does it call tools, time to first word |
| `ui_probe.py` | exercise the UI WebSocket commands of a running assistant |
| `vad_calibrate.py` | suggest `VAD_CONFIDENCE` / `VAD_MIN_VOLUME` for the room |

The Full-Duplex-Bench style metrics (takeover rate, stop and response latency) come from the separate
fd-benchmark repo (`trueshotjames/ai231-me3-full-duplex-benchmarking`).
