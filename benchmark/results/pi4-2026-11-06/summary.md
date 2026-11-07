# Full-Duplex Voice Agent on a Raspberry Pi

Student 202521354 · run 2026-11-06_16-38-00 · 2026-11-06

## 5 · Evaluation & Benchmark

| Metric | Definition | Target | Mean | p95 | n |
|---|---|---|---|---|---|
| E2E latency | end of speech → audio out | < 4.00 s | 1.08 s | 1.59 s | 29 |
| ASR final | end of speech → transcript | < 0.80 s | 0.53 s | 1.22 s | 30 |
| LLM TTFT | transcript → first token | < 1.50 s | 0.30 s | 0.73 s | 30 |
| TTS TTFB | text → first audio chunk | < 0.60 s | 0.20 s | 0.27 s | 29 |
| Barge-in | speech → playback stopped | < 0.80 s | 0.26 s | 0.32 s | 18 |
| WER | ASR accuracy on test set | < 15 % | 0.0 % | — | 30 |

- One clock: the assistant machine's monotonic clock; every stage timestamped (VAD end → ASR final → LLM 1st token → TTS 1st audio → speaker)
- E2E = speaker − VAD end; barge-in = stop − speech start
- 10 phrases × 3 runs; mean + p95; JSON log per run: stage_latency.json (per turn: trials/<id>/stages.jsonl)

Notes:
- LLM TTFT: 30 of 30 replies came from an early LLM call started before the final transcript, so TTFT reads short there; E2E is the fair end-to-end figure

## Results (deck 2)

Latency chart: latency_chart.svg

- RPi model / RAM: Raspberry Pi 4 Model B Rev 1.5 · 4 GB
- Cooling: not stated · peak 52 °C · throttled never
- Network: cloud: Wi-Fi · benchmark link: Ethernet, 6 ms round trip
- Date: 2026-11-06
- Model versions: harness pipecat 1.13.0

## 3 · Context engineering: context size per run

| Context size | Median | Max | First → last call |
|---|---|---|---|
| Prompt tokens per LLM call (estimate, usage) | 2525 | 4364 | 2285 → 2601 |
| Messages in context | 27 | 78 | 2 → 27 |
