# Donna · ME3 full-duplex voice assistant (Raspberry Pi 4)

Design A from the ME3 design report: cloud speech-to-text, language model and text-to-speech, reached
only through our own MCP servers; Pipecat, Silero VAD, barge-in and the tools on the Pi.
Due Nov 7, 2026. Backend details: `BACKEND.md`. Real devices: `mcp_servers/tools/devices/README.md`.

## Layout

```
app/            launcher, desktop icon, web UI (pywebview window), stub event backend
  launcher.py     starts everything in launch.json, serves app/ui, opens the window
  launch.json     which processes to start
  ui/             orb, chat, clock, reminders, alarms, timers, the house widgets, settings, sounds
  install_pi.sh   one-time Pi setup (apt packages, venv, Jabra udev rule, Desktop icon)
  Voice Assistant.bat, make_shortcut.ps1   Windows launcher + icon shortcut
assistant/      the Pipecat pipeline (main.py is the only process that must run)
  services/       MCP-backed STT and TTS services, timed LLM, playout buffer, Hermes client
  calibrate.py    room calibration (noise floor, your level, music leak) -> data/calibration.json
  ...             turn taking, barge-in, voice gate, music ducking, memory, announcer (see BACKEND.md)
mcp_servers/
  asr/            ASR MCP server -> Deepgram Flux (streaming speech-to-text)
  tts/            TTS MCP server -> Deepgram Aura-2 (streaming text-to-speech)
  tools/          tools MCP server: time, reminders, alarms, timers, weather and the house
    devices/        lights (Tapo), music (Spotify), phone (Telegram), Jabra buttons; simulated fallbacks
hermes/         Hermes Agent setup: the background agent for multi-step tasks (e.g. "build me a website")
benchmark/      latency and conversation-scenario rigs, test prompts
tests/          python -m pytest tests  (no network, no keys)
tools/          model export, LLM probe, go-librespot installer (kept, off)
```

## Raspberry Pi 4 (4 GB): install and run

Needs: 64-bit Raspberry Pi OS Bookworm **with the desktop**, Ethernet (or good Wi-Fi), a heatsink or fan,
and a USB speakerphone with echo cancellation (we use a Jabra Speak 510).

```bash
git clone https://github.com/trueshotjames/me3-the-donna.git ~/me3 && cd ~/me3
bash app/install_pi.sh             # add --spotify for Donna as the Spotify speaker (Chromium + Widevine)
nano .env                          # keys and devices, see "Settings for the Pi" below
.venv/bin/python -m assistant.main --list-devices
.venv/bin/python -m assistant.main --check
bash hermes/setup.sh               # optional: the background agent (hermes/README.md)
.venv/bin/python -m assistant.calibrate   # once per room, Donna closed; ~1 minute
```

Then double-click **Voice Assistant** on the Desktop (or `.venv/bin/python app/launcher.py`). Logs go to
`app/logs/`.

**Settings for the Pi** (in `.env`; everything else can stay as in `.env.example`):

```
DEEPGRAM_API_KEY=...   LLM_API_KEY=...   HERMES_API_KEY=...
AUDIO_INPUT_DEVICE=Jabra
AUDIO_OUTPUT_DEVICE=Jabra
AUDIO_OUT_SAMPLE_RATE=48000        # the Jabra plays 48 kHz; 16 kHz makes ALSA resample (crackles, lag)
MCP_TRANSPORT=inproc               # ASR/TTS servers inside the assistant process (saves ~155 MB)
LIGHT_BACKEND=tapo                 # + TAPO_BULBS, TAPO_USERNAME, TAPO_PASSWORD
MEDIA_BACKEND=spotify              # + SPOTIFY_CLIENT_ID, SPOTIFY_WEBPLAYER=1
PHONE_BACKEND=telegram             # + TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_ALLOWED=Love
```

Copy the logins from the laptop or redo them on the Pi (each is a secret, never commit it):
`data/telegram.session` (or `python -m mcp_servers.tools.devices.telegram_login`) and the Spotify login
(`python -m mcp_servers.tools.devices.spotify_login --no-browser`). `data/calibration.json` is per room and
microphone, so run the calibration on the Pi itself.

### Updating the Pi to v11 (for whoever runs the Pi, person or agent)

v11 is `main` from Nov 6 (PRs #16 and #17). Do these steps on the Pi, in order:

1. Stop her (`donna stop`; if she was started from the desktop icon, close her window instead) and get `main`.
   The Pi has been left on feature branches before, and a plain `git pull` would keep it there:
   ```bash
   donna stop
   cd ~/Documents/MLOps/"Machine Exercise 3"/me3-the-donna
   git status                    # note the branch; commit or stash nothing without asking the user
   git checkout main && git pull
   .venv/bin/pip install -r requirements.txt     # no new packages in v11; harmless to run
   ```
2. Edit Donna's own `.env` in this folder (not `fd-benchmark/.env`). `git pull` never touches it, and both switches
   are off in code, so they must be in `.env`. First see which keys already exist, so that each key ends up there
   once (a second line next to an old `HOLD_TOOL_CALLS=0` leaves two conflicting entries):
   ```bash
   grep -nE '^(TOOL_COMMENT|HOLD_TOOL_CALLS|EAGER_END_OF_TURN|FAST_CONFIRM|WEATHER_DIRECT|DONNA_PERSONA|DEEPGRAM_AURA_VOICE|TTS_SPEED|REPROMPT_SECS)=' .env
   ```
   Change the value where a key exists, add a line where it does not, keep every other line as it is:
   ```
   HOLD_TOOL_CALLS=1        # v11: a tool call from an early reply waits for the confirmed turn (needs EAGER_END_OF_TURN=1)
   TOOL_COMMENT=1           # v11: a short tail of hers after a fixed reminder / sent message / plain weather reply
   DONNA_PERSONA=1          # v10: Donna's personality (off in code; without it she keeps the old persona)
   DEEPGRAM_AURA_VOICE=aura-2-hera-en   # v10: her voice (the code default is thalia)
   TTS_SPEED=0.95           # v10: start-up speaking rate (the code default is 1.0)
   ```
   The three v10 lines matter if the Pi's `.env` predates v10. The start-up log does not warn when they are missing;
   check the line `v10 switches: DONNA_PERSONA=True TTS_SPEED=0.95 voice=aura-2-hera-en`.
   If a line `REPROMPT_SECS=...` is there, delete it: the "Are you still there?" reprompt is gone in v11
   (the setting is ignored now). Check that `EAGER_END_OF_TURN=1` and `WEATHER_DIRECT=1` are set and that `FAST_CONFIRM` is not 0 (on by default).
3. Run the tests: `.venv/bin/python -m pytest -q tests` (all should pass, on the Pi too; no network or keys needed).
4. Start her: `donna start` (one beep, then two rising notes = live), or the desktop icon if that is how she runs.
5. Check `app/logs/donna.log` for the two start-up lines:
   `v11 switch: HOLD_TOOL_CALLS=True` and `v11 switch: TOOL_COMMENT=True tools=['create_reminder', 'get_weather', 'send_message']`.
6. Try it: "What's the weather in Manila?", "Remind me to call the dentist at 3 PM tomorrow.", and a message with
   its "yes". With `PHONE_BACKEND=telegram` (the Pi) that "yes" sends a **real** Telegram message: send it only to
   a contact who expects it (for example "Text Love that I'm testing Donna"), or answer "no" to test the read-back
   only. After each fixed reply she may add a few words, or nothing. Each try logs one line,
   `tool comment spoken|passed|dropped (...) in N ms`. If every line says `dropped (not ready ...)`, the LLM is slower
   than her fixed reply on this link; that is safe (she just says the fixed reply), and the line shows the ms.
   A tool call from an early reply logs `tool hold: ... holding until the turn is confirmed`, then
   `tool hold: turn confirmed; running [...]` or `tool hold: dropped [...]`.

To go back without changing code, set `HOLD_TOOL_CALLS=0` and `TOOL_COMMENT=0` and `donna restart`. That turns
off both v11 features, but it is not all of v10: the "Are you still there?" reprompt is deleted in v11 and does not
come back. For exactly v10, check out the v10 merge (`git checkout 5386c15`), and `git checkout main` to return.

### Telegram on the Pi (messages)

Donna texts from your own Telegram account through Telethon (pure Python, installed by `install_pi.sh`).
Run these in a terminal on the Pi, from `~/me3`:

1. In `.env`, set:
   ```
   PHONE_BACKEND=telegram
   TELEGRAM_API_ID=...        # my.telegram.org > API development tools (same values as on the laptop)
   TELEGRAM_API_HASH=...
   TELEGRAM_ALLOWED=Love      # only these contacts can be messaged (comma-separated)
   JABRA_BUTTONS=0            # no calls on the Pi, so the call buttons are not needed
   ```
2. Log in once, either way:
   - **New login (simplest):** `.venv/bin/python -m mcp_servers.tools.devices.telegram_login`. Enter your phone
     number with the country code, then the code Telegram sends to your Telegram app, then your 2FA password if
     you have one. It prints "Logged in as ... N Telegram contacts".
   - **Or copy the laptop's login:** copy `data/telegram.session` to `~/me3/data/` over a private channel (USB
     stick or `scp`, never git or chat), then `chmod 600 ~/me3/data/telegram.session`. A session used on two
     machines at once can be logged out by Telegram, so prefer a new login if both will run.
3. Start Donna. The tools log (`app/logs/mcp-tools.log`) should show `phone: Telegram as <your name>, N contacts`.
   If it says "using the simulation", the line before it gives the reason (missing keys, not logged in).
4. Test: "Text Love that I'm testing Donna." She reads it back and asks "Send it?"; only a clear "yes" sends it.

Calls run on the Pi through `py-tgcalls` (`TELEGRAM_CALLS=direct`, the default there): you talk on the Jabra. To log
Donna out, use Telegram > Settings > Devices on your phone. Untested on a real Pi so far.

### What works on the Pi, and what is untested

The voice pipeline last ran on a Pi 4 on Oct 27 (local `pi-logs/`, not in the repo); everything after that was built and tested on the
Windows laptop. The code paths below are written to run on the Pi, but **only the first group has run there**.

| feature | on the Pi |
|---|---|
| mic, Silero VAD, ASR/TTS MCP servers, LLM, barge-in, tools server, UI | ran on Oct 27 (v5); later versions are laptop-tested only |
| reminders, alarms, timers, weather, memory, voice tone (Wav2Small) | pure Python / onnxruntime, expected to work; untested on the Pi since v5 |
| Tapo L530E light (python-kasa, LAN) | same code as the laptop; untested on the Pi |
| Telegram messages (Telethon), consent "Send it?" flow | pure Python; copy `data/telegram.session` or log in again |
| Jabra red/green call buttons (hidapi) | needs the udev rule `install_pi.sh` adds; untested on the Pi |
| Spotify: Donna as the speaker (`SPOTIFY_WEBPLAYER=1`) | needs `install_pi.sh --spotify` (Chromium + Widevine); untested. Costs ~250-350 MB RAM. On the Wayland desktop the small player window may show on screen; if so, switch to X11 (`sudo raspi-config` > Advanced > Wayland > X11) |
| Spotify on another device (`SPOTIFY_WEBPLAYER=0`, `SPOTIFY_DEVICE=<phone or laptop>`) | Web API only, no browser; the safe fallback |
| Telegram calls | `TELEGRAM_CALLS=direct` (default on the Pi, `py-tgcalls`, you talk on the Jabra); not yet tried on a real call. The laptop stays on `desktop`; `adb` is untested. |
| room calibration (`assistant.calibrate`) | numpy + PyAudio, expected to work; run on the Pi |
| scenario runner (`benchmark/scenario_tests/run_scenarios.ps1`) | Windows PowerShell; on the Pi run `benchmark/audio_replay.py` per scenario |
| Hermes Agent | `hermes/setup.sh` installs it as a systemd user service; check RAM with `free -m` |
| v11 `HOLD_TOOL_CALLS`, `TOOL_COMMENT` | pure Python, laptop-tested on Nov 6; untested on the Pi. `BENCHMARK_MODE` runs have no tools, so neither acts there |

**Memory (4 GB):** not yet measured with every feature on. The two big optional pieces are the Chromium speaker
(estimated 250-350 MB) and Hermes. Watch `free -m` the first time; if it gets tight, turn off the browser speaker
(`SPOTIFY_WEBPLAYER=0`) first.

## Windows (development)

1. Python 3.13: `py install 3.13`, then
   `py -3.13 -m venv .venv && .venv\Scripts\pip install -r app\requirements-app.txt -r requirements.txt`.
2. Copy `.env.example` to `.env`, add the keys; leave `AUDIO_*_DEVICE` empty for the laptop's default devices
   and set `AUDIO_OUT_SAMPLE_RATE=16000`. `python -m assistant.main --check` verifies them.
3. `powershell -ExecutionPolicy Bypass -File app\make_shortcut.ps1` (once), then double-click **Voice Assistant**.

From a terminal on any OS: `python app/launcher.py` (`--browser` opens the system browser, `--stub` runs the
scripted stub backend so the UI works without keys).

## What she can do

- **Talk full duplex**: interrupt her any time (her audio ducks the moment you speak, she stops on real words and
  carries on after "okay"); she waits through mid-sentence pauses; she ignores the TV and side talk (voice gate,
  engagement check); no wake word.
- **Tools** (tools MCP server, `mcp_servers/tools/server.py`): time, reminders, alarms, timers, weather
  (Open-Meteo), lights (`set_light`; "lights on" means `LIGHT_DEFAULT_ROOM`, default "living room"), thermostat
  (simulated), music (`control_music`: play, pause, skip, volume, shuffle, playlists), `send_message`, `make_call`,
  `end_call`, `home_status`.
- **Messages need your yes**: `send_message` never sends a first draft. She reads it back ("To Love: ... Send
  it?") and sends only on a clear yes in the next turn; anything else revises or cancels it
  (`assistant/message_consent.py`). Only contacts in `TELEGRAM_ALLOWED` can be messaged or called.
- **Music-aware listening**: music ducks to 15 % while you talk or she speaks, and a stricter gate stops song
  lyrics from opening turns (`assistant/music.py`, using the calibration).
- **Multi-step tasks**: `delegate` hands work like "create a website for my bakery" to Hermes Agent, which works
  in the background; she keeps talking, checks the files it wrote (`assistant/site_check.py`) and tells you when
  it is done.
- **Memory**: a running note and searchable transcripts of earlier conversations (`data/memory/`).
- **Speaking rate**: "speak slower" / "faster" changes Aura's speed.
- **A word of her own after fixed replies** (`TOOL_COMMENT=1`, v11): after "I'll remind you to ...", "Message sent
  to ..." or a plain weather answer she may add a few words ("Good luck with that."), or nothing.

The voice print part of the voice gate is off (`VOICE_GATE_SPEAKER=0`); the loudness gate still runs.

## Tests and benchmarks

`python -m pytest tests` (no network, no keys). The benchmark method and numbers: `benchmark/README.md`.

## Secrets

`.env`, everything under `data/` (Telegram login, Spotify token, calibration with your voice print, memory,
databases) and `app/logs/` are gitignored. Never commit them.

## Running on the Pi without a screen (service + sound cues)

Donna runs as a service on the Pi: it starts at boot, restarts her when something fails, and tells you how she
is doing with beeps, so you need nothing to look at.

| Sound | Meaning |
|---|---|
| one short beep | starting (Pi booted, or `donna start`) |
| two rising notes | live: connected and listening |
| two falling notes (repeats every minute) | stopped working: no internet, Deepgram or the LLM unreachable, or she crashed |
| three rising notes | back working |

One-time setup on the Pi (asks for the Pi password once; also prefers IPv4 and turns IPv6 off on the
"king james" hotspot, where Deepgram over IPv6 timed out):

```bash
cd ~/Documents/MLOps/"Machine Exercise 3"/me3-the-donna && git pull && bash pi/setup_service.sh
```

On the Pi (new terminal): `donna status`, `donna logs` (live output, Ctrl+C closes it, she keeps running),
`donna stop`, `donna start`, `donna restart`. The desktop icon "Donna logs" opens the same live view.

On the laptop, once: `powershell -ExecutionPolicy Bypass -File tools\donna.ps1 install`, then in a new
PowerShell window: `donna status | start | stop | restart | logs | ui`. `donna ui` opens the tunnel and her page
(http://localhost:18780/?ws=18765); the UI is optional and works while she runs as a service.

Settings: `CUES_ENABLED=1`, `CUE_VOLUME=0.3`, `CUE_DOWN_REPEAT_SECS=60`, `UI_PORT=8780` (0 = no page).
