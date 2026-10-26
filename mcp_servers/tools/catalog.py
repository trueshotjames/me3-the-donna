"""Names of the tools the LLM may call (the rest are internal to the assistant).

Kept deliberately short: the fast model (gpt-oss-20b) picks tools more reliably from about
twenty well-named tools than from forty, and every schema is re-sent on each call. The house
is one tool per device (control_music takes an `action`), not one tool per command.
"""

LLM_TOOLS = [
    # time, reminders, alarms
    "get_current_time",
    "create_reminder",
    "list_reminders",
    "complete_reminder",
    "delete_reminder",
    "create_alarm",
    "list_alarms",
    "stop_alarm",
    "delete_alarm",
    # timers
    "create_timer",
    "cancel_timer",
    # weather (Open-Meteo)
    "get_weather",
    # the house (simulated devices; see mcp_servers/tools/devices/README.md)
    "control_music",
    "set_light",
    "set_thermostat",
    "home_status",
    "send_message",
    "make_call",
    "end_call",
]

# Tools whose result changes what the house widgets show (the scheduler pushes a home snapshot after them).
HOME_TOOLS = {"control_music", "set_light", "set_thermostat", "send_message", "make_call", "end_call"}
