# UI event contract

**The authoritative schema is `assistant/EVENTS.md`.** This file only notes how `app/ui` uses it.

- Transport: `ws://127.0.0.1:8765` (port from `app/launch.json` → `events_port`), one JSON object per frame.
- Consumed: `hello`, `state` (incl. `starting`), `transcript`, `assistant_text`, `interrupted`, `tool_call`,
  `tool_result`, `reminders`, `alarms` (an alarm with `ringing: true` opens the ring overlay), `reminder_created`,
  `reminder_due`, `alarm_created`, `alarm_fired` (ring until `alarm_stopped`), `alarm_stopped`, `alarm_deleted`,
  `time_told` (toast + clock flash), `muted` (mic button), `latency` / `latency_breakdown` (voice-to-voice under the orb), `error`.
- Sent: `alarm_stop {id}` (Stop button, Esc, Space), `alarm_delete {id}`, `reminder_done {id}`, `reminder_delete {id}`,
  `mute {muted}` (Mic button; the "Sounds" button only mutes browser sounds), `interrupt` (Stop talking).
- The clock is computed in the browser; `time_told` only highlights it.
- `app/stub_events.py` implements enough of the schema to drive the UI without the pipeline.
