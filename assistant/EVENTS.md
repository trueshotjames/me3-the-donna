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

Unknown command types are ignored. A malformed message gets an `error` reply.
