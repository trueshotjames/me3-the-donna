# UI event contract

**The authoritative schema is `assistant/EVENTS.md`.** This file only notes how `app/ui` uses it.

- Transport: `ws://127.0.0.1:8765` (port from `app/launch.json` → `events_port`), one JSON object per frame.
- Consumed: `hello`, `state` (incl. `starting`), `transcript`, `assistant_text`, `interrupted`, `tool_call`,
  `tool_result`, `reminders`, `alarms` (an alarm with `ringing: true` opens the ring overlay), `reminder_created`,
  `reminder_due`, `alarm_created`, `alarm_fired` (ring until `alarm_stopped`), `alarm_stopped`, `alarm_deleted`,
  `timers` / `timer_created` / `timer_fired` (same ring overlay until `timer_stopped`) / `timer_stopped` / `timer_cancelled` (Timers card: countdown rings),
  `home` (the house widgets: Now playing, Lights, Thermostat, Phone; `home.weather` fills the Weather card on reconnect), `weather` (Weather card),
  `time_told` (toast + clock flash), `muted` (mic button), `latency` / `latency_breakdown` (voice-to-voice under the orb), `error`,
  `warning` (toast), `info` (toast), `llm_wait` (rate-limit countdown pill under the orb), `config` / `summary` / `calibrate` (Settings panel),
  `chat` / `chat_history` (conversation box; partial `transcript` shows as a live bubble), `levels` (Mic / Her meters under the orb), `net` (🌐 pill and its details panel).
- `tool_call` / `tool_result` for a device tool (`control_music`, `set_light`, `set_thermostat`, `send_message`, `make_call`, `end_call`, `get_weather`,
  `create_timer`, `cancel_timer`, `home_status`) put the matching widget in an "executing …" state (shimmer + chip) until the result flashes it green or red.
- Sent: `alarm_stop {id}` (Stop button, Esc, Space), `alarm_delete {id}`, `reminder_done {id}`, `reminder_delete {id}`,
  `timer_stop {id}` / `timer_cancel {id}` (Timers card and the ring overlay),
  `light_set {room, on | brightness | color}`, `thermostat_set {target | mode}`, `media {action, value?}`, `phone {action, contact?, text?}` (the widgets' own buttons),
  `mute {muted}` (Mic button; the "Sounds" button only mutes browser sounds), `interrupt` (Stop talking),
  `chat_clear` (Clear), `net_check` (Check now), `config`, `summarize {last}`, `vad_calibrate {secs}`, `vad_calibrate_cancel`, `vad_apply {confidence, min_volume}` (⚙ Settings).
- The clock is computed in the browser; `time_told` only highlights it. The music progress bar, the call duration and the timer rings count in the browser from the last snapshot.
- `app/stub_events.py` implements enough of the schema to drive the UI without the pipeline; with the project next to `app/` it uses the real simulated devices and plays a house script (music, lights, thermostat, timer, message, weather, call).
