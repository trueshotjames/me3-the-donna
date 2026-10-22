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
mcp_servers/
  asr/            ASR MCP server → cloud speech-to-text (Deepgram Flux)
  tts/            TTS MCP server → cloud text-to-speech (Deepgram Aura-2)
  tools/          tools MCP server: create_reminder, get_current_time, create_alarm
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

## What the UI shows

Listening / thinking / speaking orb with barge-in button · live transcript and reply ·
tool calls · clock · reminders (done / delete) · alarms (toggle / delete) · full-screen
ringing alarm with Stop · chime on new reminder/alarm, ring on alarm, blips on state change.
Sounds are synthesised in the browser (WebAudio), so no audio files are needed.
