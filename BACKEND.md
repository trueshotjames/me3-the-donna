# Donna · backend (Design A)

Pipecat 1.13 on the Pi; speech and the language model in the cloud, reached only through our own MCP servers.

```
Jabra mic ─► Silero VAD ─► voice gate ─► MCPSTTService ──► ASR MCP server ──► Deepgram Flux
   (barge-in: duck her audio)               │   (inproc or stdio)
                                  user aggregator ─► fast LLM (OpenRouter, Groq hosts) ──► tools MCP server (HTTP :8766)
                                            │          └─ delegate ─► Hermes Agent (:8642, background)
                                  PassFilter ─► MCPTTSService ──► TTS MCP server ──► Deepgram Aura-2
                                            │
                                     Jabra speaker      UI events ──► ws://127.0.0.1:8765 (assistant/EVENTS.md)
```

The ASR and TTS MCP servers are ours (`mcp_servers/asr`, `mcp_servers/tts`). MCP is request/response, so audio is
streamed as small blocks: `asr_push` sends 60 ms of microphone audio, `asr_events` long-polls transcripts, and
`tts_read` returns 80 ms of speech at a time. With `MCP_TRANSPORT=inproc` (Pi default) the two servers run in the
assistant's process over an in-memory MCP session (same protocol, ~155 MB less RAM); `stdio` runs them as
subprocesses.

## Layout

| path | what |
|---|---|
| `assistant/main.py` | entry point: builds the pipeline, starts the UI WebSocket, scheduler, memory, Hermes client |
| `assistant/config.py` | every `.env` switch and its default |
| `assistant/services/mcp_stt.py`, `mcp_tts.py` | custom Pipecat STT/TTS services over MCP |
| `assistant/services/playout.py` | adaptive playout buffer (no delay on a fast link, holds audio after stutters) |
| `assistant/services/timed_llm.py` | LLM service with timing, hedged second request, end-of-turn check |
| `assistant/services/hermes_client.py`, `assistant/tasks.py` | `delegate` to Hermes Agent and speaking its results |
| `assistant/mcp_bridge.py` | small MCP client (inproc, stdio or HTTP) |
| `assistant/vad.py`, `duck.py` | Silero VAD; duck-and-decide barge-in (pause her audio, stop on words, resume on "okay") |
| `assistant/voice_gate.py`, `engagement.py` | near-voice gate (TV/radio/side talk), name/broadcast check |
| `assistant/eot_check.py` | "cut off mid-sentence?" check |
| `assistant/fast_confirm.py`, `message_consent.py`, `tool_comment.py` | template confirmations for simple tools; hard-coded "Send it?" consent; v11 short LLM tail after a fixed reply |
| `assistant/music.py`, `calibrate.py` | music ducking and the stricter gate while music plays; room calibration |
| `assistant/spoken.py`, `processors.py` | what she actually said goes into history; `PassFilter`, text clean-up |
| `assistant/speaking_rate.py`, `site_check.py` | "speak slower"; local checks on files Hermes wrote |
| `assistant/announcer.py`, `scheduler.py` | one queue for everything she says on her own; alarms, reminders, UI commands |
| `assistant/memory.py`, `emotion.py` | long-term memory; Wav2Small voice tone hints |
| `assistant/ui_bus.py`, `ui_events.py`, `levels.py`, `netstatus.py` | UI WebSocket, events, level meters, network monitor |
| `mcp_servers/asr/server.py` | `asr_start / asr_push / asr_events / asr_finalize / asr_stop` |
| `mcp_servers/tts/server.py` | `tts_start / tts_read / tts_stop / tts_warmup / tts_set_speed` |
| `mcp_servers/tools/server.py` | reminders, alarms, timers, weather, the house (SQLite in `data/assistant.db`) |
| `mcp_servers/tools/devices/` | Tapo, Spotify (Web API + browser speaker), Telegram, Jabra, simulations |
| `benchmark/` | latency and scenario rigs; per-turn timings land in `data/latency.jsonl` and `data/turns.jsonl` |

## Run

```bash
python -m assistant.main --list-devices   # find the speakerphone; put its index or name in .env
python -m assistant.main --check          # keys + all three MCP servers answer
python -m assistant.main                  # run (the tools server is auto-started if nothing answers on :8766)
python -m assistant.calibrate             # once per room, with Donna closed
```

Setup for the Pi and Windows is in `README.md`.

## Knobs that matter (all in `.env`)

- `TURN_START_STRATEGY=asr` lets Flux's StartOfTurn start turns; `vad` uses local Silero (faster, more false starts).
- `EAGER_END_OF_TURN=1` answers Flux's EagerEndOfTurn early and discards the answer if you keep talking.
- `HOLD_TOOL_CALLS=1` keeps a tool call from an early reply, unexecuted, until the turn is confirmed, instead of
  running the LLM a second time (`assistant/tool_hold.py`). It needs `EAGER_END_OF_TURN=1`. Look for `tool hold:` in
  the log. Off in code by default, on in `.env.example`.
- `TOOL_COMMENT=1` lets a small separate LLM request add up to 8 words after the fixed reply, or nothing
  (`assistant/tool_comment.py`). It needs `FAST_CONFIRM=1`; plain weather also needs `WEATHER_DIRECT=1`.
  `TOOL_COMMENT_TOOLS` sets the tools (default `create_reminder,send_message,get_weather`). Off in code by default,
  on in `.env.example`.
- `DUCK_BARGE_IN=1`: barge-in pauses her audio at your speech onset and decides with her silent.
- `VOICE_GATE=1`, `VOICE_GATE_DB`: turns much quieter than your learned level are not for her.
  `VOICE_GATE_SPEAKER=0` keeps the voice print off.
- `MUSIC_DUCK=1`, `MUSIC_DUCK_LEVEL=15`: music level while you talk.
- `AUDIO_OUT_SAMPLE_RATE=48000` for a Jabra on the Pi; `16000` on a laptop.
- `LLM_MODEL`: the fast model; `LLM_PROVIDER_ORDER=groq` keeps it on Groq hardware.
