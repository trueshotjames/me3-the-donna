"""Speak the confirmation of a simple action tool directly, without a second LLM call (v1).

After an action tool (a light, the thermostat, a timer, an alarm, a reminder, a message, a call) the model's
second call only turns the result into "Kitchen light on." That call cost a median 1.2 s on the 2 Nov laptop
run, on about 60 % of turns, and was where repeated sentences and stray extra tool calls came from. Here the
confirmation is built from the tool's arguments and result, the result still goes into the context (so the
next turn knows what happened), and the LLM is not run again. Any error, or a tool not listed here, falls
back to the normal path (the model reads the result and answers).

No CPU cost on the Pi: string formatting only.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime
from typing import Any, Callable

from loguru import logger

ROOMS_ALL = {"all", "every", "everything", "house", "all lights", ""}
NAMES = {"mom", "dad", "alex", "sam", "dr", "dr.", "reyes", "work", "i", "i'm", "i'll"}  # v4: words kept capitalised


def _parse(text: str) -> dict | None:
    try:
        d = json.loads(text)
    except (TypeError, ValueError):
        return None
    if isinstance(d, str):  # some servers double-encode
        try:
            d = json.loads(d)
        except ValueError:
            return None
    return d if isinstance(d, dict) else None


def _clock(dt: datetime) -> str:
    """'seven thirty in the evening' style is left to the TTS; '7:30 PM' reads naturally on Aura."""
    h = dt.hour % 12 or 12
    m = f":{dt.minute:02d}" if dt.minute else ""
    return f"{h}{m} {'AM' if dt.hour < 12 else 'PM'}"


def _day(dt: datetime) -> str:
    today = datetime.now().date()
    d = dt.date()
    if d == today:
        return "today"
    if (d - today).days == 1:
        return "tomorrow"
    return dt.strftime("on %A")


def _when(iso: str | None) -> str | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return f"{_clock(dt)} {_day(dt)}"


def _room(room: Any) -> str:
    r = str(room or "").strip().lower()
    return "all" if r in ROOMS_ALL else r


def _light(a: dict, r: dict) -> str | None:
    room = _room(a.get("room"))
    done = [l.get("room") for l in (r.get("lights") or []) if isinstance(l, dict)] if isinstance(r, dict) else []
    if room == "all" and len(done) == 1 and done[0]:  # no room named: the default light (the living room)
        room = str(done[0]).lower()
    who = "All lights" if room == "all" else f"{room.capitalize()} light"
    parts = []
    if a.get("on") is False:
        return f"{who} off."
    if a.get("brightness") is not None:
        parts.append(f"at {int(a['brightness'])} percent")
    if a.get("color"):
        parts.append(str(a["color"]))
    if a.get("on") is True and not parts:
        return f"{who} on."
    if parts:
        return f"{who} {' and '.join(parts)}."
    return None


def _thermostat(a: dict, r: dict) -> str | None:
    t, mode = a.get("target"), a.get("mode")
    if t is None and a.get("change") is not None and isinstance(r, dict):
        t = r.get("target", (r.get("thermostat") or {}).get("target"))  # v6: relative change, the result has the target
    if t is not None and mode:
        return f"Thermostat set to {round(float(t))} degrees, {mode}."
    if t is not None:
        return f"Thermostat set to {round(float(t))} degrees."
    if mode:
        return "Thermostat off." if str(mode).lower() == "off" else f"Thermostat set to {mode}."
    return None


MUSIC = {
    "pause": "Paused.", "resume": "Resuming.", "unpause": "Resuming.", "continue": "Resuming.", "stop": "Music stopped.",
    "next": "Next track.", "skip": "Next track.", "previous": "Previous track.", "volume_up": "Volume up.",
    "louder": "Volume up.", "volume_down": "Volume down.", "quieter": "Volume down.", "mute": "Muted.",
    "unmute": "Unmuted.", "shuffle_on": "Shuffle on.", "shuffle": "Shuffle on.", "shuffle_off": "Shuffle off.",
    "repeat_off": "Repeat off.", "repeat_one": "Repeating this song.", "repeat_all": "Repeat on.", "repeat": "Repeat on.",
}


def _music(a: dict, r: dict) -> str | None:
    act = str(a.get("action") or "").strip().lower().replace(" ", "_").replace("-", "_")
    if act in ("set_volume", "volume") and a.get("value") is not None:
        return f"Volume {int(a['value'])}."
    if act in ("volume_up", "volume_down", "volume_up_little", "volume_down_little", "louder", "quieter", "softer") and isinstance(r.get("volume"), (int, float)):
        return f"Volume {int(r['volume'])}."  # v9: say the new level, so the change is clear even while ducked
    return MUSIC.get(act)  # play and status need the result read out: the model answers those


def _timer(a: dict, r: dict) -> str | None:
    d = r.get("spoken_duration")
    label = (r.get("label") or a.get("label") or "").strip()
    return f"{label.capitalize() + ' timer' if label else 'Timer'} set for {d}." if d else None


def _alarm(a: dict, r: dict) -> str | None:
    when = _when(r.get("next_fire"))
    if not when:
        return None
    daily = " and every day after" if r.get("repeat_daily") else ""
    return f"Alarm set for {when}{daily}."


def _reminder(a: dict, r: dict) -> str | None:
    text = (r.get("text") or a.get("text") or "").strip().rstrip(".")
    if not text:
        return None
    when = _when(r.get("due_at") or r.get("due"))
    if not when:
        return f"Reminder saved: {text}."
    # v3: v2 said "remind you to To call the dentist"; "to" is already in the template
    verb = text[3:] if text.lower().startswith("to ") else text
    # v4: the model capitalises the text ("Call the dentist"); keep names ("Mom", "Sam") and "I" as they are
    first = verb.split(" ", 1)[0]
    if first[:1].isupper() and first[1:].islower() and first.lower() not in NAMES:
        verb = verb[:1].lower() + verb[1:]
    # v5: text in Title Case ("Take My Medicine" was spoken "take My Medicine"): every word but the names
    words = verb.split(" ")
    if len(words) >= 3 and all(w[:1].isupper() for w in words[1:] if w[:1].isalpha()):
        verb = " ".join(
            w[:1].lower() + w[1:] if w[1:].islower() and w.lower().rstrip(".,") not in NAMES else w for w in words
        )
    return f"I'll remind you to {verb} at {when}."


def _stop_alarm(a: dict, r: dict) -> str | None:
    return "Stopped." if r.get("count") else "Nothing is ringing."


TEMPLATES: dict[str, Callable[[dict, dict], str | None]] = {
    "set_light": _light,
    "set_thermostat": _thermostat,
    "control_music": _music,
    "create_timer": _timer,
    "cancel_timer": lambda a, r: "Timer cancelled." if r.get("cancelled") else None,
    "create_alarm": _alarm,
    "create_reminder": _reminder,
    "complete_reminder": lambda a, r: "Marked as done." if r.get("completed") else None,
    "delete_reminder": lambda a, r: "Reminder deleted." if r.get("deleted") else None,
    "delete_alarm": lambda a, r: "Alarm deleted." if r.get("deleted") else None,
    "stop_alarm": _stop_alarm,
    "send_message": lambda a, r: f"Message sent to {a.get('contact')}." if a.get("contact") else None,
    "make_call": lambda a, r: f"Calling {a.get('contact')}." if a.get("contact") else None,
    "end_call": lambda a, r: "Call ended.",
}


# v8 (WEATHER_DIRECT): in v7 the slowest replies were tool turns waiting for the second LLM call, most of them
# get_weather, whose result already carries a sentence to speak ("Berlin now: mainly clear, 27 degrees, ...").
# A plain weather question gets that sentence directly; anything that needs judgement ("Is it cold outside?",
# "Do I need an umbrella?") still goes to the model.
WEATHER_DIRECT = False
_WEATHER_ASK = re.compile(r"\b(weather|forecast)\b", re.I)
_WEATHER_JUDGE = re.compile(
    r"^\s*(is|are|will|was|should|do|does|did|can|could|would|shall)\b|\b(umbrella|jacket|coat|wear|bring|need|should|"
    r"good day|nice day|compare|warmer|colder|than)\b",
    re.I,
)


def weather_sentence(user_text: str, result_text: str) -> str | None:
    """The weather tool's own sentence for a plain weather question, else None (the model answers)."""
    if not _WEATHER_ASK.search(user_text or "") or _WEATHER_JUDGE.search(user_text or ""):
        return None
    r = _parse(result_text)
    if r is None or "error" in r:
        return None
    spoken = str(r.get("spoken") or "").strip()
    return spoken or None


# v8 (TOOL_SLOW_SECS): a tool that has not answered after this long gets a spoken "One moment." (scenario 13: the
# weather service times out after 6 s; v7 was silent all that time). ON_SLOW is set by main.py.
TOOL_SLOW_SECS = 0.0
ON_SLOW: Callable[[str], Any] | None = None


def confirmation(name: str, args: dict | None, result_text: str) -> str | None:
    """The sentence to speak for this tool result, or None to let the model answer."""
    fn = TEMPLATES.get(name)
    if fn is None:
        return None
    r = _parse(result_text)
    if r is None or "error" in r:
        return None
    try:
        return fn(args or {}, r)
    except Exception as e:  # noqa: BLE001
        logger.debug(f"fast confirm {name} failed: {e!r}")
        return None


# v4: v3 scenario 11 called get_weather(Manila) twice in one turn (the follow-up LLM call asked again instead of
# answering) and read the weather out twice. An identical call (same tool, same arguments) in the same user turn is
# not run again: the model gets the earlier result back with a note to answer from it.
REPEATABLE = {"control_music"}  # "next, next" is a real request
# v8 (V8_FIXES): the v8 run's "Play some jazz" called control_music(play, jazz) twice in one turn and said "Playing
# jazz: ..." twice. Repeating a step (next, volume) is real; repeating the same play/pause/stop is not.
V8_FIXES = False
_ONCE_ACTIONS = {"play", "pause", "resume", "stop", "shuffle_on", "shuffle_off", "mute", "unmute"}
DUPLICATE_NOTE = "This exact call already ran in this turn; its result is below. Do not call it again: answer from it now."


class _TurnCalls:
    def __init__(self):
        self._turn = None
        self._calls: dict[str, str] = {}
        self._spoken: set[str] = set()  # v8: calls whose result was spoken from a template
        self.blocked = 0

    @staticmethod
    def _key(name: str, args: dict | None) -> str:
        return name + json.dumps(dict(args or {}), sort_keys=True, default=str)

    def _sync(self) -> None:
        from assistant.turn_metrics import METRICS

        if METRICS.current is not self._turn:
            self._turn, self._calls, self._spoken = METRICS.current, {}, set()

    def earlier(self, name: str, args: dict | None) -> str | None:
        once = V8_FIXES and str(dict(args or {}).get("action") or "").strip().lower() in _ONCE_ACTIONS
        if name in REPEATABLE and not once:
            return None
        self._sync()
        return self._calls.get(self._key(name, args))

    def mark_spoken(self, name: str, args: dict | None) -> None:
        self._sync()
        self._spoken.add(self._key(name, args))

    def was_spoken(self, name: str, args: dict | None) -> bool:
        self._sync()
        return self._key(name, args) in self._spoken

    def done(self, name: str, args: dict | None, result: Any) -> None:
        self._sync()
        self._calls[self._key(name, args)] = result if isinstance(result, str) else json.dumps(result, default=str)


TURN_CALLS = _TurnCalls()

# v4: v3 and the v4 smoke run answered "Yes, delete it" (said after the delete already ran) with "Which reminder?",
# even with a prompt rule. For DELETE_NOTE_SECS after a delete, the per-turn note says so.
DELETE_NOTE_SECS = 30.0
_last_delete: tuple[float, str] | None = None


def recent_delete_note() -> str | None:
    if _last_delete is None or time.time() - _last_delete[0] > DELETE_NOTE_SECS:
        return None
    return f"already done: {_last_delete[1]} was deleted just now; if the user now confirms deleting it, say it is already deleted"


# v6 (V6_FIXES): "Make it a bit warmer" -> the model set an absolute 21 without reading the thermostat (v5b t6).
# For a relative request the target becomes the current target plus or minus one degree, read from home_status.
THERMO_GUARD = True
_RELATIVE = re.compile(r"\b(warmer|hotter|heat (?:it |things )?up|cooler|colder|cool (?:it |things )?down)\b", re.I)


def _last_user(params) -> str:
    try:
        msgs = params.context.get_messages()
    except Exception:  # noqa: BLE001
        return ""
    for m in reversed(msgs):
        if isinstance(m, dict) and m.get("role") == "user" and isinstance(m.get("content"), str):
            return m["content"].split("\n\n[", 1)[0]
    return ""


async def relative_thermostat(params, client) -> None:
    m = _RELATIVE.search(_last_user(params))
    if not m:
        return
    up = m.group(1).lower().startswith(("warm", "hot", "heat"))
    a = params.arguments or {}
    try:
        change = float(a["change"]) if a.get("change") is not None and a.get("target") is None else None
    except (TypeError, ValueError):
        change = None
    if change is not None and (0 < change <= 3 if up else -3 <= change < 0):
        return  # v6: the relative `change` argument, right direction: the server applies it
    try:
        status = _parse(await client._call_tool_text("home_status", {})) or {}
        target = float((status.get("thermostat") or {}).get("target"))
    except Exception as e:  # noqa: BLE001
        logger.info(f"thermostat guard: could not read the target ({e!r}); left as the model set it")
        return
    args = dict(params.arguments or {})
    want = target + (1 if up else -1)
    got = args.get("target")
    try:
        ok = got is not None and (0 < (float(got) - target) <= 3 if up else -3 <= (float(got) - target) < 0)
    except (TypeError, ValueError):
        ok = False
    if not ok:
        logger.info(f"thermostat guard: '{m.group(1)}' with target {got} (now {target}); setting {want:g}")
        args["target"] = want
        args.pop("change", None)
        params.arguments = args


def _user_turn(params) -> int:
    """Index of the current user turn (number of user messages in the context)."""
    try:
        return sum(1 for m in params.context.get_messages() if isinstance(m, dict) and m.get("role") == "user")
    except Exception:  # noqa: BLE001
        return 0


async def _consent_gate(params) -> bool:
    """Hard-coded consent (message_consent.py): True = forward this send_message; False = read back, not sent."""
    from pipecat.frames.frames import FunctionCallResultProperties, LLMFullResponseEndFrame, LLMFullResponseStartFrame, LLMTextFrame

    from assistant.message_consent import CONSENT

    args = dict(params.arguments or {})
    verdict, value = CONSENT.decide(str(args.get("contact", "")), str(args.get("text", "")), _user_turn(params), _last_user(params))
    if verdict == "send":
        logger.info(f"message consent: user said yes; sending the confirmed text to {args.get('contact')!r}")
        args["text"] = value
        params.arguments = args
        return True
    logger.info(f"message consent: not sent; reading back: {value!r}")
    await params.result_callback(
        json.dumps({"needs_confirmation": True, "sent": False, "contact": args.get("contact"), "text": args.get("text"),
                    "note": "NOT sent. Donna read it back; send only if the user says yes next turn (call send_message again with the same contact and text)."}),
        properties=FunctionCallResultProperties(run_llm=False),
    )
    await params.llm.push_frame(LLMFullResponseStartFrame())
    await params.llm.push_frame(LLMTextFrame(value))
    await params.llm.push_frame(LLMFullResponseEndFrame())
    return False


def wrap_tools(standard_tools: list, client) -> list:
    """Return the MCP tool schemas with handlers that confirm action tools without a second LLM call."""
    from pipecat.adapters.schemas.function_schema import FunctionSchema
    from pipecat.frames.frames import LLMFullResponseEndFrame, LLMFullResponseStartFrame, LLMTextFrame
    from pipecat.frames.frames import FunctionCallResultProperties

    def guarded(name: str, original):
        async def handler(params):
            if name == "send_message" and not await _consent_gate(params):
                return
            if name == "set_thermostat" and THERMO_GUARD:
                await relative_thermostat(params, client)
            prev = TURN_CALLS.earlier(name, params.arguments)
            if prev is not None and V8_FIXES and TURN_CALLS.was_spoken(name, params.arguments):
                # v8 (V8_FIXES): its result was already spoken from a template ("Paris now: ..."); asking the model to
                # "answer from it" made her read the weather a second time (v8 s13 run). End the call silently.
                TURN_CALLS.blocked += 1
                logger.info(f"duplicate {name}({params.arguments}) in the same turn, already spoken; not run or said again")
                await params.result_callback(
                    json.dumps({"duplicate": True, "note": "already answered aloud"}),
                    properties=FunctionCallResultProperties(run_llm=False),
                )
                return
            if prev is not None:
                TURN_CALLS.blocked += 1
                logger.info(f"duplicate {name}({params.arguments}) in the same turn; not run again")
                await params.result_callback(json.dumps({"duplicate": True, "note": DUPLICATE_NOTE, "result": prev}))
                return
            callback = params.result_callback

            async def remember(result, *a, **kw):
                TURN_CALLS.done(name, params.arguments, result)
                await callback(result, *a, **kw)

            params.result_callback = remember
            slow = None
            if TOOL_SLOW_SECS > 0 and ON_SLOW is not None:
                import asyncio

                async def notice():
                    await asyncio.sleep(TOOL_SLOW_SECS)
                    logger.info(f"tool {name} still running after {TOOL_SLOW_SECS:g} s; saying one moment")
                    await ON_SLOW(name)

                slow = asyncio.get_running_loop().create_task(notice())
            try:
                await original(params)
            finally:
                if slow is not None:
                    slow.cancel()

        return handler

    def make(name: str, original):
        async def handler(params):
            text = await client._call_tool_text(name, params.arguments)
            TURN_CALLS.done(name, params.arguments, text)
            if name == "get_weather":
                say = weather_sentence(_last_user(params), text)
                if say is not None:
                    logger.info(f"weather direct: {say!r} (no second LLM call)")
            else:
                say = confirmation(name, params.arguments, text)
            if say is None:
                await params.result_callback(text)
                return
            logger.info(f"fast confirm {name}: {say!r} (no second LLM call)")
            if name in ("delete_reminder", "delete_alarm"):
                global _last_delete
                _last_delete = (time.time(), f"{name.split('_')[1]} {dict(params.arguments or {}).get('id', '')}".strip())
            TURN_CALLS.mark_spoken(name, params.arguments)
            await params.result_callback(text, properties=FunctionCallResultProperties(run_llm=False))
            await params.llm.push_frame(LLMFullResponseStartFrame())
            await params.llm.push_frame(LLMTextFrame(say))
            await params.llm.push_frame(LLMFullResponseEndFrame())

        return handler

    out = []
    for s in standard_tools:
        direct = s.name in TEMPLATES or (WEATHER_DIRECT and s.name == "get_weather")
        h = make(s.name, s.handler) if direct else s.handler
        h = guarded(s.name, h) if h is not None else None
        out.append(FunctionSchema(s.name, s.description, s.properties, s.required, handler=h))
    return out
