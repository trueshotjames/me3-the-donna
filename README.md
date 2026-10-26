# ME3 · Full-duplex voice assistant (Raspberry Pi 4)

Design A from `docs/ME3_ Three Ways to Build It.html`: cloud streaming ASR/LLM/TTS behind
our own MCP servers, Pipecat on the Pi, Silero VAD, barge-in. Due Nov 7, 1:59 PM.

## Layout

```
app/            launcher, desktop icon, web UI (pywebview window), stub event backend
  launcher.py     starts everything in launch.json, serves app/ui, opens the window
  launch.json     which processes to start (edit here if entry points move)
  ui/             index.html, app.js, style.css  — state orb, clock, reminders, alarms, sounds
  stub_events.py  fake backend so the UI runs before the pipeline exists
  UI_EVENTS.md    WebSocket messages the UI consumes/sends
  assets/         logo.png / logo.ico / logo-512.png
  Voice Assistant.bat, make_shortcut.ps1   Windows launcher + icon shortcut
  voice-assistant.desktop, install_pi.sh   Raspberry Pi launcher + icon
assistant/      Pipecat pipeline (main.py, the only process that must run; it spawns the ASR/TTS servers), EVENTS.md
                tasks.py (Hermes), announcer.py (speech queue), memory.py (long-term memory)
mcp_servers/
  asr/            ASR MCP server → cloud speech-to-text (Deepgram Flux)
  tts/            TTS MCP server → cloud text-to-speech (Deepgram Aura-2)
  tools/          tools MCP server: time, reminders, alarms, timers, weather (Open-Meteo) and the house
    devices/        lights, thermostat, music player, phone: simulated by default, real backends plug in here (README inside)
benchmark/      latency benchmark rig, prompts, results
docs/           brief (ESSENTIALS.md), scenarios, papers, design report
requirements.txt   pipeline + MCP servers (pinned)
```

## Run it

**Windows (development)**
1. Use **Python 3.13** (PyAudio and onnxruntime have no Windows wheels for 3.14 yet): `py install 3.13`, then
   `py -3.13 -m venv .venv && .venv\Scripts\pip install -r app\requirements-app.txt -r requirements.txt`, then copy `.env.example` → `.env` and add the API keys (`python -m assistant.main --check` verifies them).
2. `powershell -ExecutionPolicy Bypass -File app\make_shortcut.ps1` (once) → creates **Voice Assistant** on the Desktop and in this folder.
3. Double-click **Voice Assistant**. Logs go to `app/logs/`.

**Raspberry Pi**
1. `bash app/install_pi.sh` (once; installs GTK/WebKit for pywebview, venv, Desktop icon).
2. Double-click **Voice Assistant** on the Desktop.

**From a terminal (any OS)**: `python app/launcher.py` · `--browser` opens the system
browser instead of a window · `--stub` forces the stub backend.

The launcher skips any process whose entry point is missing, so the UI works with only
`app/` present (stub backend plays a scripted conversation, including an alarm that rings
until you press Stop). Copy `.env.example` → `.env` with the API keys once the servers exist.

## Fast model, Hermes Agent and voice emotion

The pipeline calls a fast model directly (OpenRouter, `LLM_MODEL=openai/gpt-oss-20b` pinned to
Groq hardware; see `.env.example`). Multi-step or open-world requests are delegated to a
**Hermes Agent** running on the same machine as a background tool (`delegate`): the fast model
keeps talking while Hermes (DeepSeek flash via OpenRouter) works, then speaks the result.
Set it up the same way on the laptop and the Pi: `hermes/setup.ps1` (Windows) or
`hermes/setup.sh` (Pi), details in `hermes/README.md`.

**Announcements.** Everything she says on her own (a Hermes result, a timed reminder, an alarm,
"One moment") goes through one queue (`assistant/announcer.py`). A result waits until the user's
current query has been answered, even if he was still mid-sentence when it arrived, and is
repeated once if he cuts it off. Alarms only wait for him to finish his sentence.

**Memory** (`assistant/memory.py`, `MEMORY_*` in `.env.example`). She answers from the last
`LLM_HISTORY_MESSAGES` messages plus a memory note in her prompt (`data/memory/note.md`, at most
4000 characters). Every conversation is written to `data/memory/episodes/` as it happens; when a
conversation ends (30 minutes of silence, or 40 messages) a background model call merges it into
the note, without delaying her replies. A `recall` tool searches the transcripts for things from
earlier days. Delete `data/memory/` to start her fresh.

**Wav2Small** (audEERING) scores the user's voice for arousal, dominance and valence while they
speak; a one-line hint goes into the system prompt when a value leaves its neutral band
(`EMOTION_*` in `.env.example`). Export the ONNX model once with `python tools/export_wav2small.py`
(needs torch; the file `models/wav2small.onnx` is under 200 KB and runs on onnxruntime on the Pi).

Tests: `python -m pytest tests` (no network, no API keys needed).

## The house: music, lights, thermostat, timers, phone, weather

The top household commands each have a tool on the tools MCP server (`mcp_servers/tools/server.py`;
names in `catalog.py`): `control_music` (play by song, artist, genre or playlist; pause, resume, stop,
next, previous, volume, mute, shuffle, repeat, status), `set_light` (room or all; on/off, brightness,
colour), `set_thermostat` (target, mode), `home_status`, `create_timer` / `cancel_timer` (a done timer
rings like an alarm; "stop" silences it), `get_weather` (Open-Meteo, no key; `WEATHER_DEFAULT_CITY`
in `.env`), `send_message`, `make_call`, `end_call`. The LLM sees 22 tools in all.

The devices are **simulated** (`mcp_servers/tools/devices/sim.py`): their state lives in
`data/assistant.db`, a track plays out in real time, a call rings four seconds and connects, a
message is delivered after a moment, the room warms or cools three degrees a minute. Everything
goes through the interfaces in `devices/base.py`, so a real device is one subclass and a
`*_BACKEND` line in `.env` (`LIGHT_BACKEND=gpio`, `MEDIA_BACKEND=mpv`, `PHONE_BACKEND=telegram`;
skeletons in `devices/hardware.py`, details in `devices/README.md`). The UI shows one widget per
device and animates each command while its tool runs.

## What the UI shows

Listening / thinking / speaking orb with barge-in button · live transcript and reply ·
tool calls · clock · reminders (done / delete) · alarms (toggle / delete) · timers (countdown rings) ·
full-screen ringing alarm or timer with Stop · the house: now playing (spinning disc, progress,
transport, volume, queue), lights (bulbs that glow with the colour and brightness, sliders, swatches),
thermostat (dial, heating / cooling), phone (call card, message thread with delivery ticks), weather
(current + 3 days) · every widget has its own buttons, which call the same tools the voice does ·
chime on new reminder/alarm/timer, ring on alarm or timer, ring-ring while a call connects, blips on
state change. Sounds are synthesised in the browser (WebAudio), so no audio files are needed.
