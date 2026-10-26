# Household devices (lights, thermostat, music, phone)

The tools MCP server (`mcp_servers/tools/server.py`) controls the house only through the
interfaces in `base.py`. By default every device is **simulated** (`sim.py`): its state lives in
`data/assistant.db` (table `device_state`) and the UI widgets show it. Nothing runs in the
background; time-based behaviour (a track ending, a call connecting, the room warming up) is
advanced in `tick()`, which the server calls once a second from `poll_due`.

| device | interface | simulated behaviour | real backend |
|---|---|---|---|
| lights | `LightDevice.set(room, on, brightness, color)` | rooms from `LIGHT_ROOMS`; switch and fade (`transition_ms`) | `hardware.GpioLights` (`LIGHT_BACKEND=gpio`, `LIGHT_GPIO_PINS="living room:18,bedroom:23"`) |
| thermostat | `ThermostatDevice.set(target, mode)` | room temperature moves 3 °C/min toward the target while heating/cooling | none yet: subclass `ThermostatDevice` |
| media | `MediaPlayer.play/pause/resume/stop/next/previous/set_volume/volume_step/set_mute/set_shuffle/set_repeat` | a 12-track made-up library (genres, playlists); position runs from `started_at` | `hardware.MpvPlayer` (`MEDIA_BACKEND=mpv`, `MUSIC_DIR`) |
| phone | `PhoneDevice.call/hang_up/send_message` | contacts from `PHONE_CONTACTS`; a call rings 4 s then connects; a message is delivered after 1.5 s | `hardware.TelegramPhone` (`PHONE_BACKEND=telegram`, messages only) |

## Plugging in real hardware

1. Subclass the interface in `base.py` (or wrap the sim class the way `hardware.py` does, so the
   state and `status()` shape stay identical and the UI keeps working).
2. Return the same dict shapes as the simulation: the LLM reads them to confirm, and
   `assistant/scheduler.py` forwards `home_snapshot` to the widgets unchanged.
3. Register it in `build_devices()` (`__init__.py`) behind a `*_BACKEND` value. A backend that
   fails to start falls back to the simulation with a warning, so the demo never loses a widget.

Everything the devices need from the environment is read in `build_devices()`; nothing else in
the server knows which backend is live (`home_status` reports it under `backends`).
