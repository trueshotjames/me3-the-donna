# UI event schema (backend ⇄ UI)

Transport: WebSocket on `ws://127.0.0.1:8765` (`UI_WS_HOST` / `UI_WS_PORT` in `.env`).
The backend (`python -m assistant.main`) hosts the server; the UI connects.
One JSON object per message. Every message has `"type"` and `"ts"` (unix milliseconds).
Times inside payloads (`due_at`, `next_fire`, `fired_at`, `iso`) are ISO 8601 **local** time, no timezone suffix.

## On connect

The backend sends, in order: `hello`, `state`, `reminders` (snapshot), `alarms` (snapshot).

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
| `muted` | `muted: bool` | mic mute state changed |
| `latency` | `stage`, `ms` | per-stage timings; `stage` is `voice_to_voice` or `<Service>:ttfb` / `<Service>:processing` |
| `latency_breakdown` | `total_ms`, `contributions: [{label, owner, ms}]`, `ttfb: [...]`, `function_calls: [...]` | Pipecat's per-turn latency breakdown, once per bot reply |
| `error` | `message`, `fatal: bool` | pipeline or provider error (show a toast) |
| `warning` | `message`, `kind` | e.g. `kind: "no_tool_call"`: the LLM said an alarm/reminder is set but called no tool, so nothing was saved |
| `info` | `message` | a note for a toast (e.g. VAD thresholds applied) |
| `llm_wait` | `seconds`, `remaining_tokens`, `limit_tokens` | Groq answered 429 (rate limit); the SDK retries after `seconds`. The bot also says "One moment." when the wait is 2 s or more |
| `config` | `model`, `reasoning_effort`, `history_messages`, `vad_confidence`, `vad_min_volume`, `vad_start_secs`, `vad_stop_secs`, `turn_start`, `eager_end_of_turn`, `tts_voice`, `turns_log`, `calibrating` | current settings: on connect and after `config` / `vad_apply` |
| `summary` | `turns`, `replied`, `from`, `to`, `rate_limit_waits`, `rate_limit_wait_s`, `rows: [{metric, n, median, p90, min, max}]`, or `error` | answer to `summarize` (same numbers as `python -m benchmark.summarize`) |
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

## UI → backend

| type | fields | effect |
|---|---|---|
| `alarm_stop` | `id` (optional; omit to stop all) | silences; backend emits `alarm_stopped` + `alarms` |
| `alarm_delete` | `id` | deletes; backend emits `alarms` |
| `reminder_done` | `id` | marks done; backend emits `reminder_done` + `reminders` |
| `reminder_delete` | `id` | deletes; backend emits `reminders` |
| `mute` | `muted: bool` | mutes the microphone path (STT) and bot-initiated speech |
| `interrupt` | – | same as a voice barge-in: stops the bot speaking |
| `refresh` | – | resend `reminders` and `alarms` snapshots |
| `say` | `text` | debug: make the bot speak the text (not added to the conversation) |
| `config` | – | resend the `config` snapshot |
| `summarize` | `last` (0 = all turns) | compute the latency summary of `data/turns.jsonl`; backend emits `summary` |
| `vad_calibrate` | `secs` (per part, 3-30) | calibrate the VAD from the running pipeline: `secs` quiet, then `secs` talking; backend emits `calibrate` events |
| `vad_calibrate_cancel` | – | abort a running calibration |
| `vad_apply` | `confidence`, `min_volume` | apply VAD thresholds to the running detector and save them to `.env`; backend emits `config` + `info` |

Unknown command types are ignored. A malformed message gets an `error` reply.
