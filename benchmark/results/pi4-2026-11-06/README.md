# Pi 4 benchmark run, Nov 6 2026 (`2026-11-06_16-38-00`)

Full fdbench run of Donna in `BENCHMARK_MODE=1` (no tools, Hermes or memory) on a **Raspberry Pi 4 Model B (4 GB)**.
fdbench ran on the Windows laptop and fed the clips to the Pi over Ethernet (direct rig, 6 ms round trip);
the Pi reached the cloud APIs over Wi-Fi. 12 rounds, seed 779831, 150 trials queued, 150 done, 0 failed;
scored 2026-11-06 19:14 (judge: GPT-4o via OpenRouter, transcripts: faster-whisper small).

**Code version:** the run started 16:38 PHT (08:38 UTC), after PR #13 (benchmark mode) and before v10 (PR #14,
14:08 UTC) and v11 (PRs #16-#17) were merged, so it measures Donna before the v10 persona/Hera voice and the v11
tool hold. The run does not record a commit hash; this is from the merge times.

**Open the results page:** [results.html (rendered)](https://htmlpreview.github.io/?https://github.com/trueshotjames/me3-the-donna/blob/main/benchmark/results/pi4-2026-11-06/results.html)
· [raw file](results.html) (download and open in a browser if the preview fails).

## Files

| file | what |
|---|---|
| `results.html` | the results page: deck evaluation table, latency chart, Full-Duplex-Bench metrics, Pi resources, every trial |
| `summary.md` | the deck template's "5 · Evaluation & Benchmark" table and run card, as Markdown |
| `results.json`, `results.csv` | every metric (median, p95, mean, n, k, p-values) |
| `stage_latency.json`, `latency_chart.svg` | per-turn stage timings for the 30 latency turns (VAD end → ASR final → LLM first token → TTS first audio → speaker) |
| `run.log` | per-trial log: what was played, what was expected, what she did, verdict |
| `events.jsonl`, `marks.jsonl` | raw run events and the Pi's stage timestamps |
| `pi_stats.jsonl`, `pi_stats_hello.json` | Pi CPU, per-core load, RAM, temperature, clock, throttle flags (one sample per second) |
| `session.json`, `check.json`, `queue.json` | run settings, preflight checks, trial order |
| `trials/<id>/` | per trial: `timing.json`, `stages.jsonl`, `asr_her.json` (what she said), `judge.json`, `pi_stats.jsonl` |
| `recordings/` | `INDEX.md` lists all 198 trials with verdicts; the 29 **failed** trials' stereo recordings are included (left = test audio, right = Donna) |

Left out to keep the repo small: the audio of passing trials (`recordings/ok_*.flac`) and the per-trial
`played.flac` / `her.flac` (about 80 MB). They stay on the laptop under `fd-benchmark/runs/202521354/2026-11-06_16-38-00/`.

Reproduce: from the laptop, `powershell -ExecutionPolicy Bypass -File fd-benchmark\run_pi_benchmark.ps1 -Rounds 12`
(fdbench lives in `trueshotjames/ai231-me3-full-duplex-benchmarking`).
