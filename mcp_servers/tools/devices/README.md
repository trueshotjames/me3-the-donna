# Household devices (lights, thermostat, music, phone)

The tools MCP server (`mcp_servers/tools/server.py`) controls the house only through the
interfaces in `base.py`. By default every device is **simulated** (`sim.py`): its state lives in
`data/assistant.db` (table `device_state`) and the UI widgets show it. Nothing runs in the
background; time-based behaviour (a track ending, a call connecting, the room warming up) is
advanced in `tick()`, which the server calls once a second from `poll_due`.

| device | interface | simulated behaviour | real backend |
|---|---|---|---|
| lights | `LightDevice.set(room, on, brightness, color)` | rooms from `LIGHT_ROOMS`; no room named ("lights on") = `LIGHT_DEFAULT_ROOM` (else the first room), "all" = every room; switch and fade (`transition_ms`) | `tapo.TapoLights` (`LIGHT_BACKEND=tapo`, `TAPO_BULBS`, `TAPO_USERNAME/PASSWORD`; local LAN via python-kasa; check with `python -m mcp_servers.tools.devices.tapo`) or `hardware.GpioLights` (`LIGHT_BACKEND=gpio`, `LIGHT_GPIO_PINS="living room:18,bedroom:23"`) |
| thermostat | `ThermostatDevice.set(target, mode)` | room temperature moves 3 °C/min toward the target while heating/cooling | none yet: subclass `ThermostatDevice` |
| media | `MediaPlayer.play/pause/resume/stop/next/previous/set_volume/volume_step/set_mute/set_shuffle/set_repeat` | a 12-track made-up library (genres, playlists); position runs from `started_at` | `spotify.SpotifyPlayer` (`MEDIA_BACKEND=spotify`, `SPOTIFY_CLIENT_ID`, one-time `python -m mcp_servers.tools.devices.spotify_login`); plays on Donna herself (`SPOTIFY_WEBPLAYER=1`, `webplayer.py`) or on another Connect device (`SPOTIFY_DEVICE`). Or `hardware.MpvPlayer` (`MEDIA_BACKEND=mpv`, `MUSIC_DIR`) |
| phone | `PhoneDevice.call/hang_up/send_message` | contacts from `PHONE_CONTACTS`; a call rings 4 s then connects; a message is delivered after 1.5 s | `telegram.TelegramPhone` (`PHONE_BACKEND=telegram`: your own account via Telethon; messages everywhere, calls via Telegram Desktop on Windows or adb on the Pi) or `hardware.TelegramPhone` (`PHONE_BACKEND=telegram_bot`, messages only) |

## Plugging in real hardware

1. Subclass the interface in `base.py` (or wrap the sim class the way `hardware.py` does, so the
   state and `status()` shape stay identical and the UI keeps working).
2. Return the same dict shapes as the simulation: the LLM reads them to confirm, and
   `assistant/scheduler.py` forwards `home_snapshot` to the widgets unchanged.
3. Register it in `build_devices()` (`__init__.py`) behind a `*_BACKEND` value. A backend that
   fails to start falls back to the simulation with a warning, so the demo never loses a widget.

Everything the devices need from the environment is read in `build_devices()`; nothing else in
the server knows which backend is live (`home_status` reports it under `backends`).

## Secrets

Spotify and Tapo credentials live only in `.env` and `data/spotify_token.json` (both gitignored). Nothing
prints, logs or sends them to the UI; a failed login is reported as a spoken error without the value.

## Setup: Spotify (MEDIA_BACKEND=spotify)

1. developer.spotify.com > Dashboard > Create app. Redirect URI: `http://127.0.0.1:8888/callback`; API: Web API.
   The app's owner must have Premium. Copy the **Client ID** into `.env` as `SPOTIFY_CLIENT_ID` (no secret is needed).
2. Laptop: `python -m mcp_servers.tools.devices.spotify_login` (browser opens, log in, Agree). Open the Spotify app once.
3. Pi: install a Connect receiver: `curl -sL https://dtcooper.github.io/raspotify/install.sh | sh`, then in
   `/etc/raspotify/conf` set `LIBRESPOT_NAME="Donna"` and restart it; set `SPOTIFY_DEVICE=Donna` in the Pi's `.env`.
   Log in on the Pi itself (each machine keeps its own token):
   `python -m mcp_servers.tools.devices.spotify_login --no-browser`.
4. Music played through the USB speakerphone is removed by its echo canceller, like Donna's own voice.

## Setup: Tapo bulb (LIGHT_BACKEND=tapo)

1. `.env`: `TAPO_BULBS=living room:<bulb IP>`, `TAPO_USERNAME` / `TAPO_PASSWORD` = the Tapo app account.
2. Tapo app > Me > Third-Party Services > Third-Party Compatibility: on (turn it off and on again after a firmware update).
3. Check: `python -m mcp_servers.tools.devices.tapo` (lists bulbs, tries the login, changes nothing).
   "needs a newer protocol ... TPAP" = the bulb's firmware uses TP-Link's TPAP login, which python-kasa 0.12 does not
   support yet (python-kasa issue #1590, PR #1592).

## Donna as the Spotify speaker (SPOTIFY_WEBPLAYER=1)

Music plays from Donna herself, through the default speaker, with the Spotify app closed everywhere. The tools
server opens a hidden browser window with Spotify's official Web Playback SDK (`webplayer.py`), which shows up in
Spotify as the speaker "Donna"; `control_music` always plays there.

- Needs the Spotify login with the `streaming` permission (re-run `spotify_login` after 2026-11-05) and Premium.
- Laptop: uses Chrome (or Edge) with its own profile in `data/webplayer-profile/`. Make the Jabra the Windows
  default playback device.
- Pi (64-bit Raspberry Pi OS with the desktop): `sudo apt install chromium libwidevinecdm0` (Spotify's player
  needs Widevine DRM), make the speakerphone the default output, set `AUDIO_INPUT_DEVICE=pipewire` and `AUDIO_OUTPUT_DEVICE=pipewire`
  (with `Jabra` there Donna locks the card and the music goes to the headphone jack), set `SPOTIFY_WEBPLAYER=1`. Not yet tested on
  the Pi: check that "Donna speaker ready" appears in the tools log and watch RAM (`free -m`) while playing.
- go-librespot (`speaker.py`, `SPOTIFY_SPEAKER=1`, `tools/install_speaker_pi.sh`) is kept but off: Spotify refuses
  its audio keys for newer accounts (go-librespot #279, librespot #1649), so it plays silence.

## Setup: Telegram (PHONE_BACKEND=telegram)

Donna messages and calls from **your own** Telegram account (Telethon, the client API, not a bot).

1. my.telegram.org > API development tools: create an app; put `TELEGRAM_API_ID` and `TELEGRAM_API_HASH` in `.env`.
2. `python -m mcp_servers.tools.devices.telegram_login` once (phone number, code, 2FA password if set). It writes
   `data/telegram.session`, which is a full login to your account: keep it secret, never commit it. To move to the
   Pi, copy that file over a private channel or simply log in again on the Pi.
3. `TELEGRAM_ALLOWED=Love` (comma-separated): only these contacts can be messaged or called. Others are refused.
4. Messages: `send_message` never sends the first draft. Donna reads it back and sends only on a clear "yes" in
   the next turn (`assistant/message_consent.py`); "no", "change it to ..." revises or cancels.
5. Calls (`TELEGRAM_CALLS`):
   - `desktop` (Windows, default there): opens the contact's chat in Telegram Desktop and presses Call through UI
     Automation (`uiautomation`). You talk through the PC's default audio device. **Windows only**: Telegram
     Desktop has no Linux ARM build and the automation is Windows-specific.
   - `adb` (default on the Pi): starts the call on an Android phone over USB (USB debugging on, Telegram >
     Settings > Privacy > Sync Contacts on). Written against Android's contacts intents; **never tested**.

## Jabra phone buttons (JABRA_BUTTONS=1)

During a Telegram call the Jabra Speak's red button hangs up and green answers (`jabra.py`, through hidapi).
Windows needs nothing more. On the Pi the HID node must be readable: `app/install_pi.sh` adds
`/etc/udev/rules.d/50-jabra.rules` (`KERNEL=="hidraw*", ATTRS{idVendor}=="0b0e", MODE="0666"`). Untested on the Pi.

## On the Raspberry Pi

| backend | Pi status |
|---|---|
| Tapo (python-kasa) | same code as on the laptop; needs the Pi on the bulb's LAN. Untested on the Pi |
| Spotify Web API | same code; log in on the Pi with `spotify_login --no-browser` |
| Spotify browser speaker | `bash app/install_pi.sh --spotify` (Chromium + libwidevinecdm0). The browser gets `--password-store=basic` on Linux so no keyring dialog blocks it; extra flags via `SPOTIFY_WEBPLAYER_ARGS`. On Wayland the small window may be visible (switch the desktop to X11 in raspi-config). A shell watchdog closes Chromium when the tools server dies. Untested on the Pi |
| Telegram messages | pure Python; untested on the Pi |
| Telegram calls | desktop mode impossible on the Pi; adb mode untested |
| GPIO lights | `python3-gpiozero` from apt (installed by `install_pi.sh`); untested |

