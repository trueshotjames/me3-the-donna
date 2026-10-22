# ME3 voice assistant — backend (Design A)

Pipecat 1.13 on the Pi; speech and the language model in the cloud, reached only
through our own MCP servers. See `docs/ME3_ Three Ways to Build It.html` for the design.

```
mic ─► Silero VAD ─► MCPSTTService ──► mcp_servers/asr  (stdio) ──► Deepgram Flux
                          │
                   user aggregator ─► Groq LLM ──► mcp_servers/tools (HTTP :8766, SQLite)
                          │
                   PassFilter ─► MCPTTSService ──► mcp_servers/tts  (stdio) ──► Deepgram Aura-2
                          │
                       speaker          UI events ──► ws://127.0.0.1:8765 (assistant/EVENTS.md)
```

## Layout

| path | what |
|---|---|
| `assistant/main.py` | entry point: builds the pipeline, starts the UI WebSocket and the scheduler |
| `assistant/services/mcp_stt.py` | custom Pipecat STT: pushes 60 ms audio blocks to the ASR server, long-polls events |
| `assistant/services/mcp_tts.py` | custom Pipecat TTS: streams 80 ms audio blocks back from the TTS server |
| `assistant/mcp_bridge.py` | small MCP client (stdio or HTTP) used by the services and the scheduler |
| `assistant/scheduler.py` | fires alarms and timed reminders (bot-initiated speech), serves UI commands |
| `assistant/ui_bus.py`, `ui_events.py` | WebSocket hub and the frame→event observer |
| `assistant/processors.py` | `PassFilter`: drops replies that start with `<pass/>` (speech not meant for the bot) |
| `assistant/prompts.py` | system prompt |
| `mcp_servers/asr/server.py` | ASR MCP server: `asr_start / asr_push / asr_events / asr_finalize / asr_stop` |
| `mcp_servers/tts/server.py` | TTS MCP server: `tts_start / tts_read / tts_stop / tts_warmup` |
| `mcp_servers/tools/server.py` | tools MCP server: reminders, alarms, time (SQLite in `data/assistant.db`) |
| `benchmark/` | latency rig (not built yet); per-turn timings already land in `data/latency.jsonl` |

## Setup

Windows (development), Python 3.11+:

```powershell
py -3.11 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env      # then fill in DEEPGRAM_API_KEY and GROQ_API_KEY
```

Raspberry Pi 4 (64-bit Raspberry Pi OS):

```bash
sudo apt install -y python3-venv python3-dev portaudio19-dev
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # fill in the keys; use Ethernet
```

## Run

```bash
python -m assistant.main --list-devices   # find the speakerphone; put its index or name in .env
python -m assistant.main --check          # keys + all three MCP servers answer
python -m assistant.main                  # run (tools server is auto-started if not running)
```

The launcher may start the tools server itself: `python -m mcp_servers.tools.server`
(HTTP on 127.0.0.1:8766). The ASR and TTS servers are spawned by the assistant over stdio.

## Knobs that matter (all in `.env`)

- `TURN_START_STRATEGY=vad` starts a user turn from local Silero (barge-in reflex);
  `asr` leaves it to Flux's StartOfTurn (one cloud round trip slower, fewer false starts).
- `EAGER_END_OF_TURN=1` answers Flux's EagerEndOfTurn early and discards the answer if you keep talking.
  Costs extra LLM calls; turn off if the Groq daily quota is a concern.
- `AUDIO_OUT_SAMPLE_RATE=48000` for a Jabra Speak on the Pi.
- `VAD_*` thresholds: day-1 audio checklist in the design doc.

## Measured

Headless test (fake Deepgram + fake LLM, this container): MCP overhead ≈ 4 ms per `asr_push`,
first `tts_read` audio ≈ 20 ms after `tts_start`. Real cloud numbers come from `data/latency.jsonl`.
