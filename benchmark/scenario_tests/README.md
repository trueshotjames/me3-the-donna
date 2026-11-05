# Scenario and tool-choice tests

Spoken tests for the laptop assistant. Each scenario file is played into the real pipeline (VAD, ASR MCP server + Flux,
LLM, tools, TTS MCP server) by `benchmark/audio_replay.py`, which replaces the microphone with synthesized speech.
Nothing in the assistant's code is changed.

| path | what |
|---|---|
| `scenarios/s01..s16_*.json` | the conversation scenarios from `conversation-scenarios.md` (13, tool/network failure, is not automated: it needs fault injection) |
| `scenarios/t1..t6_*.json` | tool-choice tests: t1/t2 clear requests, t3/t4 incomplete requests that need a question then a follow-up, t5/t6 look-alike tools (alarm/reminder/timer, inside/outside temperature, delegate, recall) |
| `clips/` | the speech clips the scenarios play (Deepgram Aura-2, voice orion, 16 kHz mono WAV) |
| `run_scenarios.ps1` | runs every scenario in a folder, one process each |
| `summarize_scenarios.py` | prints each step (said / heard / tools / reply / latency) and writes `summary.json` |
| `results/<date>/` | per-scenario `*.json`, console logs, `summary.json`; `fdbench_trials.json` and `fdbench_console.log` from the same day's fd-benchmark run |

## Run

The harness reads clips from `benchmark/audio/prompts/` (and synthesizes any that are missing, which needs the
Deepgram key). To replay with exactly the saved clips, copy them back first:

```powershell
Copy-Item benchmark\scenario_tests\clips\*.wav benchmark\audio\prompts\
powershell -ExecutionPolicy Bypass -File benchmark\scenario_tests\run_scenarios.ps1 `
  -Project . -Scenarios benchmark\scenario_tests\scenarios -Out benchmark\scenario_tests\results\NEW
.venv\Scripts\python benchmark\scenario_tests\summarize_scenarios.py benchmark\scenario_tests\results\NEW
```

Run from the project folder. One full pass takes about 50 minutes (21 files, 20 s apart). Every scenario waits 15 s
(`lead_secs`) before the first prompt so the Deepgram connections are up. Each scenario starts with an empty
`data/audio_replay.db`, so alarms and reminders from one scenario do not carry into the next.

The fd-benchmark run itself (recordings, results.html) is in `fd-benchmark/runs/202521354/2026-11-02_21-13-39/`.
