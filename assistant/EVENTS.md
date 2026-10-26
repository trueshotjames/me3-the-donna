# UI event schema (backend ⇄ UI)

Transport: WebSocket on `ws://127.0.0.1:8765` (`UI_WS_HOST` / `UI_WS_PORT` in `.env`).
The backend (`python -m assistant.main`) hosts the server; the UI connects.
One JSON object per message. Every message has `"type"` and `"ts"` (unix milliseconds).
Times inside payloads (`due_at`, `next_fire`, `fired_at`, `iso`) are ISO 8601 **local** time, no timezone suffix.

## On connect

The backend sends, in order: `hello`, `state`, `reminders` (snapshot), `alarms` (snapshot), `timers` (snapshot), `config`, `home` (snapshot), `chat_history`, and `net` once the first internet check has run.

## Backend → UI

| type | fields | when |
|---|---|---|
| `hello` | `version: 1` | first message after connect |
| `state` | `state: "starting" \| "idle" \| "listening" \| "thinking" \| "speaking"` | every change; `starting` only while the pipeline boots |
| `transcript` | `text`, `final: bool` | partial (`final=false`) and final (`final=true`) user transcript |
| `assistant_text` | `text`, `final: true` | each sentence as it is handed to TTS (spoken order) |
| `interrupted` | – | the user barged in while the bot was speaking (play a short "stop" blip if you like) |
| `tool_call` | `name`, `args`, `id` | the LLM called a tool |
| `tool_result` | `name`, `result` (parsed JSON or string), `id` | the tool returned |
| `reminders` | `items: [Reminder]` | full snapshot: on connect and after any change |
| `reminder_created` | `id`, `text`, `due_at \| null` | the LLM created a reminder (widget + sound) |
| `reminder_due` | `id`, `text`, `due_at` | a timed reminder fired (bot also speaks it) |
| `reminder_done` | `id` | marked done (by voice or UI) |
| `reminder_deleted` | `id` | deleted |
| `alarms` | `items: [Alarm]` | full snapshot: on connect and after any change |
| `alarm_created` | `id`, `label`, `time "HH:MM"`, `next_fire`, `repeat_daily` | the LLM set an alarm (widget + sound) |
| `alarm_fired` | `id`, `label`, `time` | an alarm is ringing: **UI plays the alarm sound in a loop until `alarm_stopped`** (bot announces it by voice, un-interruptibly, and repeats every 45 s) |
| `alarm_stopped` | `id` | alarm silenced by voice ("stop") or by the UI |
| `alarm_deleted` | `id` | deleted |
| `time_told` | `iso`, `spoken` | the LLM called `get_current_time` (show a clock widget) |
| `timers` | `items: [Timer]` | full snapshot of running and ringing timers: on connect and after any change |
| `timer_created` | `id`, `label`, `duration_s`, `ends_at` | the LLM started a timer (widget + chime) |
| `timer_fired` | `id`, `label`, `duration_s` | a timer is done: **UI rings until `timer_stopped`** (bot announces it, un-interruptibly, every 45 s) |
| `timer_stopped` | `id` | silenced by voice ("stop" → `stop_alarm`) or by the UI (`timer_stop`) |
| `timer_cancelled` | `id` | cancelled before it rang |
| `home` | `version`, `lights: [Light]`, `rooms`, `thermostat`, `media`, `phone`, `backends`, `weather \| null` | the house for the widgets (shapes below): on connect, right after a device tool call, and whenever a device changed on its own (a track ended, a call connected, the room warmed up; `poll_due` reports a new `version`) |
| `weather` | `city`, `unit`, `now: {temperature, feels_like, humidity, wind_kmh, conditions, icon, is_day}`, `days: [{date, label, high, low, rain_chance, conditions, icon}]`, `asked_for?`, `spoken` | the LLM called `get_weather` (Open-Meteo); the same object comes back in `home.weather` on reconnect |
| `muted` | `muted: bool` | mic mute state changed |
| `latency` | `stage`, `ms` | per-stage timings; `stage` is `voice_to_voice` or `<Service>:ttfb` / `<Service>:processing` |
| `latency_breakdown` | `total_ms`, `contributions: [{label, owner, ms}]`, `ttfb: [...]`, `function_calls: [...]` | Pipecat's per-turn latency breakdown, once per bot reply |
| `error` | `message`, `fatal: bool` | pipeline or provider error (show a toast) |
| `task_started` | `id`, `task` | the LLM delegated a task to Hermes (show a "working on: …" chip) |
| `task_progress` | `id`, `progress`, `tool` | Hermes started a tool ("searching the web") |
| `task_done` | `id`, `task`, `result` | Hermes finished; the bot speaks the result at the next quiet moment |
| `task_failed` | `id`, `task`, `error` | Hermes failed, timed out or was cancelled |
| `announcement` | `id`, `source` (`hermes`, `reminder`, `alarm`, `llm_wait`, `ui`), `state` (`queued`, `speaking`, `spoken`, `requeued`, `interrupted`, `dropped:<why>`), `mode`, `wait` (on `queued`) | something she says on her own moved through the announcement queue (assistant/announcer.py) |
| `memory` | `state` (`episode_closed`, `note_updated`, `update_failed`), plus `file`, `reason`, `turns` / `chars`, `episodes` / `error` | a conversation was closed and written to data/memory, or the memory note in her prompt was refreshed |
| `voice` | `arousal`, `dominance`, `valence` (0..1), `infer_ms`, `window_s` | Wav2Small scores while the user speaks (about once a second) |
| `voice_hint` | `hint: string \| null` | the line added to the prompt for this turn, or null when nothing was outside the deadzone |
| `warning` | `message`, `kind` | e.g. `kind: "no_tool_call"`: the LLM said an alarm/reminder is set but called no tool, so nothing was saved |
| `info` | `message` | a note for a toast (e.g. VAD thresholds applied) |
| `llm_wait` | `seconds`, `remaining_tokens`, `limit_tokens` | Groq answered 429 (rate limit); the SDK retries after `seconds`. The bot also says "One moment." when the wait is 2 s or more |
| `config` | `model`, `reasoning_effort`, `history_messages`, `vad_confidence`, `vad_min_volume`, `vad_start_secs`, `vad_stop_secs`, `turn_start`, `eager_end_of_turn`, `tts_voice`, `turns_log`, `calibrating` | current settings: on connect and after `config` / `vad_apply` |
| `summary` | `turns`, `replied`, `from`, `to`, `rate_limit_waits`, `rate_limit_wait_s`, `rows: [{metric, n, median, p90, min, max}]`, or `error` | answer to `summarize` (same numbers as `python -m benchmark.summarize`) |
| `chat` | `item: {id, role: "user" \| "assistant" \| "tool", text, t, interrupted?, ok?}` | one entry of this session's conversation was added or updated (an assistant entry grows sentence by sentence; same `id` = replace). In memory only, last 200 (assistant/chat_log.py) |
| `chat_history` | `items: [chat item]` | whole chat on connect, and `items: []` after `chat_clear` |
| `levels` | `mic_db`, `mic_peak_db`, `out_db` (dBFS, -90 = silence) | every 100 ms while a UI is connected: loudest mic frame and loudest played speaker chunk since the last event (assistant/levels.py) |
| `net` | `online`, `targets: [{name, label, host, state: good\|fair\|poor\|down\|unknown, rtt_ms, dns_ms, tcp_ms, tls_ms, error, age_s}]`, `handshakes: [{t, provider, ms, ok, reconnect}]` | internet check every 15 s (5 s while a host is down) and on `net_check`. `rtt_ms` = median TCP connect of the last 3 checks; good < 150 ms, fair < 400 ms. `handshakes`: Deepgram connects in the last 10 min, timed by the MCP servers (data/net_events.jsonl). A connect of 2 s or more also sends `warning` (`kind: "slow_connect"`) |
| `calibrate` | `phase: "quiet" \| "talk" \| "done" \| "cancelled" \| "error"`, `secs`, `message`, `result` (on done: `confidence`, `min_volume`, `speech_pass_pct`, `quiet_pass_pct`, `quiet`, `speech`, `too_quiet`, `current`) | progress of the in-app VAD calibration (`vad_calibrate`); the STT is muted while it runs (`muted` events bracket it) |

### Reminder

```json
{"id": 3, "text": "water the plants", "due_at": "2026-10-22T18:30:00", "done": false, "notified": false, "created_at": "..."}
```

### Alarm

```json
{"id": 1, "label": "gym", "time": "19:30", "next_fire": "2026-10-22T19:30:00", "repeat_daily": false,
 "enabled": true, "ringing": false, "fired_at": null, "created_at": "..."}
```

`enabled=false` means a one-shot alarm that already rang. `ringing=true` means the UI should be playing the alarm sound.

### Timer

```json
{"id": 2, "label": "pasta", "duration_s": 600, "ends_at": "2026-10-26T18:40:00", "remaining_s": 412,
 "state": "running", "ringing": false, "fired_at": null, "created_at": "..."}
```

`state` is `running` | `ringing` | `done` | `cancelled`; snapshots only carry the first two. The UI counts down from `ends_at`.

### The house (`home`)

Simulated devices by default (`mcp_servers/tools/devices/`); `backends` says which backend each one uses (`sim`, `gpio`, `mpv`, `telegram`).

```json
{"version": 12,
 "rooms": ["living room", "bedroom", "kitchen"],
 "lights": [{"room": "living room", "on": true, "brightness": 30, "color": "#5a8dff", "color_name": "blue"}, "..."],
 "thermostat": {"unit": "C", "target": 21.0, "current": 23.4, "mode": "auto", "activity": "cooling", "min": 10.0, "max": 32.0, "backend": "sim"},
 "media": {"state": "playing", "track": {"title": "Morning Light", "artist": "Sim Quartet", "genre": "jazz", "duration_s": 212, "art": ["#f59e0b", "#ef4444"]},
           "position_s": 14.2, "started_at": 1793025403.4, "volume": 60, "muted": false, "shuffle": false, "repeat": "off",
           "source": "jazz", "queue_length": 2, "queue_index": 0, "up_next": [{"title": "Blue Hour", "artist": "Sim Quartet"}], "backend": "sim"},
 "phone": {"contacts": ["Mom", "Dad", "Alex", "Sam", "Dr. Reyes", "Work"],
           "call": {"contact": "Mom", "state": "ringing", "started_at": 1793025403.4, "connected_at": null, "ended_at": null, "duration_s": 0},
           "messages": [{"id": 1, "contact": "Alex", "text": "running late", "state": "delivered", "t": 1793025403.4, "delivered_at": 1793025405.0}], "backend": "sim"},
 "backends": {"lights": "sim", "thermostat": "sim", "media": "sim", "phone": "sim"},
 "weather": null}
```

`thermostat.activity` is `heating` | `cooling` | `idle` | `off`; the room moves 3 °C a minute toward the target in simulation. `media.state` is `playing` | `paused` | `stopped`; the UI runs the progress bar from `position_s` at the time of the message. A call is `ringing` (4 s in simulation), then `connected` (the UI shows the duration from `connected_at`), then `ended`. A message is `sent`, then `delivered` (1.5 s). A device tool that changes something is also visible as `tool_call` / `tool_result` (`control_music`, `set_light`, `set_thermostat`, `send_message`, `make_call`, `end_call`, `get_weather`, `create_timer`, `cancel_timer`, `home_status`): the widget shows "executing" between the two.

## UI → backend

| type | fields | effect |
|---|---|---|
| `alarm_stop` | `id` (optional; omit to stop all) | silences; backend emits `alarm_stopped` + `alarms` |
| `alarm_delete` | `id` | deletes; backend emits `alarms` |
| `reminder_done` | `id` | marks done; backend emits `reminder_done` + `reminders` |
| `reminder_delete` | `id` | deletes; backend emits `reminders` |
| `timer_stop` | `id` (optional; omit to stop all) | silences a ringing timer; backend emits `timer_stopped` + `timers` |
| `timer_cancel` | `id` (optional; omit to cancel all) | cancels a running timer; backend emits `timer_cancelled` + `timers` |
| `light_set` | `room`, `on?`, `brightness?`, `color?` | same as the LLM's `set_light`; backend emits `home` (and `warning` with `kind: "device"` if refused) |
| `thermostat_set` | `target?`, `mode?` | same as `set_thermostat`; backend emits `home` |
| `media` | `action`, `query?`, `value?` | same as `control_music` (`resume`, `pause`, `next`, `previous`, `set_volume`, `mute`, `shuffle_on`, `repeat_all`, …); backend emits `home` |
| `phone` | `action: call \| hang_up \| message`, `contact?`, `text?` | same as `make_call` / `end_call` / `send_message`; backend emits `home` |
| `home_refresh` | – | resend the `home` snapshot |
| `mute` | `muted: bool` | mutes the microphone path (STT) and bot-initiated speech |
| `interrupt` | – | same as a voice barge-in: stops the bot speaking |
| `refresh` | – | resend `reminders` and `alarms` snapshots |
| `say` | `text` | debug: make the bot speak the text (not added to the conversation) |
| `config` | – | resend the `config` snapshot |
| `summarize` | `last` (0 = all turns) | compute the latency summary of `data/turns.jsonl`; backend emits `summary` |
| `vad_calibrate` | `secs` (per part, 3-30) | calibrate the VAD from the running pipeline: `secs` quiet, then `secs` talking; backend emits `calibrate` events |
| `vad_calibrate_cancel` | – | abort a running calibration |
| `chat_clear` | – | empty the chat box (her memory and the LLM context are not touched); backend emits `chat_history` |
| `net_check` | – | run the internet check now; backend emits `net` |
| `vad_apply` | `confidence`, `min_volume` | apply VAD thresholds to the running detector and save them to `.env`; backend emits `config` + `info` |

Unknown command types are ignored. A malformed message gets an `error` reply.

## Network time per turn (data/turns.jsonl)

Each turn record has a `net` block (assistant/netstatus.py), also logged as one `network` line after the turn block:

| field | meaning |
|---|---|
| `rtt_ms.deepgram`, `rtt_ms.llm` | round trip to each provider at the time of the turn (median of the last 3 checks) |
| `handshakes`, `handshake_ms` | **measured**: Flux / Aura connects (and failed connects) that overlapped the turn, in ms; only those finished before her first audio count in `handshake_ms` |
| `estimate_ms.asr_ms` | **estimate**: one Deepgram round trip (your last audio up, end of turn down) |
| `estimate_ms.llm_ms` | **estimate**: one LLM round trip per call kept for the reply |
| `estimate_ms.llm_new_connection_ms` | **estimate**: DNS + TCP + TLS of the last check, for each LLM call made more than 5 s after the previous one (the HTTP client drops idle connections after 5 s) |
| `estimate_ms.tts_ms` | **estimate**: one Deepgram round trip (sentence up, first audio down) |
| `total_ms`, `share_of_e2e_pct` | estimates + measured handshakes, and that as a share of `end_to_end.total_ms` |

Why the streaming stages are estimates: Deepgram and the LLM report neither how long they worked nor when they
received the request, so their time-to-first-byte cannot be split exactly into wire time and provider time from this side.
