"""
Timers, alarms, and reminders.

The LLM service holds no clock and no node connections, so this skill only
*parses* — it queues ``set_schedule`` / ``cancel_schedule`` actions that the
server's scheduler actuates (persisted, fired via announce). Status/list/cancel
questions are answered from the asking node's active entries, which the server
injects into every /process request (read via ``get_request("schedules")``).

Two tiers, like home_assistant:
  * a fast intent handles the common phrasings instantly (no model call) —
    "set a timer for 10 minutes", "how much time is left", "wake me at 7",
    "remind me in 20 minutes to flip the bread", "cancel the timer";
  * @skill tools let the LLM handle everything fuzzier ("remind me next
    Tuesday evening…", compound phrasings, ambiguous times).
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from kenzy.llm.skills import (
    FastResult,
    add_action,
    fast_intent,
    get_request,
    request_channel,
    skill,
)

DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_DAY_FULL = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_DAY_ALIASES = {d.lower(): d[:3].lower() for d in _DAY_FULL}
_DAY_GROUPS = {
    "day": list(DAY_NAMES),
    "morning": list(DAY_NAMES),
    "daily": list(DAY_NAMES),
    "weekday": list(DAY_NAMES[:5]),
    "weekdays": list(DAY_NAMES[:5]),
    "weekend": list(DAY_NAMES[5:]),
    "weekends": list(DAY_NAMES[5:]),
}

_NUM_WORDS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12, "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40,
    "forty-five": 45, "fifty": 50, "sixty": 60, "ninety": 90,
}  # fmt: skip
_UNIT_S = {"second": 1, "minute": 60, "hour": 3600}


def _num_token(tok: str) -> float | None:
    try:
        return float(tok)
    except ValueError:
        return float(_NUM_WORDS[tok]) if tok in _NUM_WORDS else None


def parse_duration(text: str) -> int | None:
    """Parse a spoken duration ("10 minutes", "an hour and a half", "1 hour and
    20 minutes") into seconds. Returns None when it doesn't parse."""
    text = re.sub(r"\s+", " ", text.strip().lower().rstrip("."))
    if text in ("half an hour", "a half hour"):
        return 1800
    tokens = text.split(" ")
    total = 0.0
    i = 0
    while i < len(tokens):
        n = _num_token(tokens[i])
        if n is None:
            return None
        i += 1
        half = False
        if tokens[i : i + 3] == ["and", "a", "half"]:  # "one and a half minutes"
            half, i = True, i + 3
        if i >= len(tokens):
            return None
        unit = tokens[i].rstrip("s")
        if unit not in _UNIT_S:
            return None
        i += 1
        if tokens[i : i + 3] == ["and", "a", "half"]:  # "an hour and a half"
            half, i = True, i + 3
        total += (n + (0.5 if half else 0.0)) * _UNIT_S[unit]
        if i < len(tokens):  # "1 hour and 20 minutes" — expect a joiner
            if tokens[i] != "and":
                return None
            i += 1
    return int(total) if 0 < total <= 7 * 24 * 3600 else None


def parse_days(text: str) -> list[str] | None:
    """Parse a recurrence phrase ("every weekday", "every saturday") into
    canonical day tokens; [] = one-shot, None = didn't parse."""
    text = text.strip().lower()
    if not text:
        return []
    m = re.match(r"^(?:every|each|on) (?P<what>[a-z ]+?)s?$", text)
    if not m:
        return None
    what = m.group("what").strip()
    if what in _DAY_GROUPS:
        return list(_DAY_GROUPS[what])
    if what in _DAY_ALIASES:
        return [_DAY_ALIASES[what]]
    if what in DAY_NAMES:
        return [what]
    return None


_CLOCK_RE = re.compile(
    r"^(?:(?P<h>\d{1,2})(?::(?P<mn>\d{2}))?\s*(?P<mer>a\.?m\.?|p\.?m\.?)?|(?P<noon>noon)|(?P<mid>midnight))$"
)


def parse_clock(text: str, *, wake: bool = False) -> str | None:
    """Parse a spoken clock time into 24-hour ``HH:MM``.

    With no am/pm: a ``wake`` phrasing biases to morning; otherwise the nearest
    future occurrence of that hour (12-hour cycle) is chosen.
    """
    m = _CLOCK_RE.match(text.strip().lower().rstrip("."))
    if not m:
        return None
    if m.group("noon"):
        return "12:00"
    if m.group("mid"):
        return "00:00"
    h, mn = int(m.group("h")), int(m.group("mn") or 0)
    if h > 23 or mn > 59:
        return None
    mer = (m.group("mer") or "").replace(".", "")
    if mer == "am":
        h = h % 12
    elif mer == "pm":
        h = h % 12 + 12
    elif h <= 12:
        if wake:
            h = 12 if h == 12 else h % 12  # "wake me at 7" → 07:00
        else:
            # No meridiem: pick whichever of h / h+12 comes next on the clock.
            now = datetime.now().astimezone()
            am, pm = h % 12, h % 12 + 12
            minutes_now = now.hour * 60 + now.minute
            am_wait = (am * 60 + mn - minutes_now) % (24 * 60)
            pm_wait = (pm * 60 + mn - minutes_now) % (24 * 60)
            h = am if am_wait <= pm_wait else pm
    return f"{h:02d}:{mn:02d}"


# ---------------------------------------------------------------------------
# Humanizing (spoken confirmations)
# ---------------------------------------------------------------------------


def _fmt_duration(seconds: int) -> str:
    parts = []
    for unit, size in (("hour", 3600), ("minute", 60), ("second", 1)):
        n, seconds = divmod(seconds, size)
        if n:
            parts.append(f"{n} {unit}" + ("s" if n != 1 else ""))
    return " and ".join(parts) or "0 seconds"


def _fmt_clock(hhmm: str) -> str:
    h, m = int(hhmm[:2]), int(hhmm[3:])
    mer = "AM" if h < 12 else "PM"
    h12 = h % 12 or 12
    return f"{h12}:{m:02d} {mer}"


def _fmt_days(days: list[str]) -> str:
    if not days:
        return ""
    ordered = [d for d in DAY_NAMES if d in days]
    if ordered == list(DAY_NAMES):
        return "every day"
    if ordered == list(DAY_NAMES[:5]):
        return "every weekday"
    if ordered == list(DAY_NAMES[5:]):
        return "every weekend"
    names = [_DAY_FULL[DAY_NAMES.index(d)] for d in ordered]
    return "every " + (" and ".join(names) if len(names) <= 2 else ", ".join(names))


def _describe(entry: dict[str, Any]) -> str:
    text = _describe_bare(entry)
    # A nodeless channel (assist) lists the whole house's entries — the
    # listener isn't standing in any of the rooms, so say where each one is.
    if request_channel() != "voice" and entry.get("room"):
        text += f" in the {entry['room']}"
    return text


def _describe_bare(entry: dict[str, Any]) -> str:
    kind = entry.get("kind", "?")
    label = str(entry.get("label") or "")
    if entry.get("at"):
        when = f"at {_fmt_clock(str(entry['at']))}"
        days = _fmt_days(list(entry.get("days") or []))
        if days:
            when += f" {days}"
    else:
        when = f"with {_fmt_duration(int(entry.get('seconds_left', 0)))} left"
    if kind == "reminder":
        head = (
            f"a reminder {label}"
            if re.match(r"^(?:to|that|about)\b", label)
            else f"a reminder to {label}".strip()
            if label
            else "a reminder"
        )
        return f"{head} {when}"
    if kind == "alarm":
        return f"an alarm {when}"
    if kind == "command":
        return f"a scheduled command — {label} — {when}"
    name = f"{label} timer" if label else "timer"
    return f"the {name} {when}"


def _entries(kind: str | None = None) -> list[dict[str, Any]]:
    items = [dict(e) for e in (get_request("schedules") or [])]
    return [e for e in items if kind is None or e.get("kind") == kind]


def _alarm_blocked_room(room: str | None) -> str | None:
    """If the alarm's target room lacks AEC, return its display name, else None.

    An alarm's ring loop is stopped by the wake word heard OVER the ringing —
    impossible without echo cancellation — so such rooms refuse at set time
    (with a spoken alternative) rather than confirm something that can't work.
    Target = the explicit room, or the asking room when none was given.
    """
    target = (room or get_request("room_id") or "").strip().lower()
    if not target:
        return None
    for r in get_request("no_aec_rooms") or []:
        if str(r).strip().lower() == target:
            return str(r)
    return None


def _alarm_refusal(blocked: str, here: bool) -> str:
    where = "This room's speaker" if here else f"The {blocked} speaker"
    return (
        f"{where} can't run alarms — without echo cancellation it couldn't hear "
        "you stop the ringing. I can set a timer or a reminder instead."
    )


def _known_room(name: str) -> str | None:
    wanted = name.strip().lower()
    for r in get_request("rooms") or []:
        if str(r).strip().lower() == wanted:
            return str(r)
    return None


def _assist_roomless(action: dict[str, Any]) -> bool:
    """True when a set arrived from a nodeless channel (HA Assist, F3) with no
    explicit room — there is no asking node to deliver in, so the set must
    name a room. Fast handlers miss (the LLM converses to get the room);
    LLM tools return _NEEDS_ROOM so the model asks and retries."""
    return request_channel() != "voice" and not action.get("room")


_NEEDS_ROOM = (
    "Error: this request didn't come from a room speaker, so there is no "
    "asking room to deliver in. Ask the user which room it's for, then retry "
    "with `room` set to one of the connected rooms."
)


# ---------------------------------------------------------------------------
# LLM tools (the fuzzy tier)
# ---------------------------------------------------------------------------


@skill
async def set_timer(duration_seconds: int, label: str = "", room: str = "") -> str:
    """Start a countdown timer that announces itself in the asking room when done.

    Use for relative durations ("set a timer for 10 minutes", "a two hour
    timer"). Convert the spoken duration to seconds. `label` names the timer
    when the user gives one ("pizza timer").

    duration_seconds: total duration in seconds (must be positive)
    label: optional short name for the timer
    room: announce in this room instead of the asking room (required when the
        request doesn't come from a room speaker)
    """
    if duration_seconds <= 0:
        return "Error: duration_seconds must be positive."
    action: dict[str, Any] = {
        "type": "set_schedule",
        "kind": "timer",
        "seconds": int(duration_seconds),
        "label": label.strip(),
    }
    if room.strip():
        if _known_room(room) is None:
            return f"Error: no connected room named {room!r}."
        action["room"] = _known_room(room)
    if _assist_roomless(action):
        return _NEEDS_ROOM
    add_action(action)
    name = f"{label.strip()} timer" if label.strip() else "timer"
    return f"Scheduled: {name} for {_fmt_duration(int(duration_seconds))}."


@skill
async def set_alarm(time: str, days: str = "", room: str = "") -> str:
    """Set an alarm that rings in a room at a clock time, optionally recurring.

    Use for wake-ups and clock-time alerts ("wake me at 7", "alarm for 6:30 every
    weekday"). Resolve any ambiguity (am/pm) yourself before calling.

    time: 24-hour clock time as "HH:MM" (e.g. "07:00", "18:30")
    days: recurrence — comma-separated day names ("mon,tue"), or "weekdays",
        "weekends", "daily". Empty = one-shot (next occurrence of the time).
    room: room to ring in; empty = the room the request came from. Must be one
        of the connected rooms.
    """
    try:
        day_list = _normalize_day_arg(days)
    except ValueError as exc:
        return f"Error: {exc}"
    if not re.match(r"^\d{2}:\d{2}$", time.strip()):
        return 'Error: time must be 24-hour "HH:MM".'
    action: dict[str, Any] = {
        "type": "set_schedule",
        "kind": "alarm",
        "at": time.strip(),
        "days": day_list,
        "label": "",
    }
    if room.strip():
        if _known_room(room) is None:
            return f"Error: no connected room named {room!r}."
        action["room"] = _known_room(room)
    blocked = _alarm_blocked_room(action.get("room"))
    if blocked is not None:
        return _alarm_refusal(blocked, here=not room.strip())
    if _assist_roomless(action):
        return _NEEDS_ROOM
    add_action(action)
    suffix = f" {_fmt_days(day_list)}" if day_list else ""
    return f"Scheduled: alarm at {_fmt_clock(time.strip())}{suffix}."


@skill
async def set_reminder(
    text: str, in_seconds: int = 0, time: str = "", days: str = "", room: str = ""
) -> str:
    """Schedule a spoken reminder ("remind me to take out the trash at 6pm").

    Give either `in_seconds` (relative) or `time` (clock). Resolve ambiguity
    (am/pm, "tomorrow") yourself before calling.

    text: the reminder content, phrased to complete "You asked me to remind
        you …" — e.g. "to take out the trash" or "that the game starts at eight"
    in_seconds: fire this many seconds from now (relative reminders)
    time: 24-hour clock time as "HH:MM" (clock reminders)
    days: recurrence for clock reminders — day names, "weekdays", "weekends",
        "daily"; empty = one-shot
    room: room to speak in; empty = the asking room. Must be a connected room.
    """
    if not text.strip():
        return "Error: the reminder text is empty."
    if bool(in_seconds > 0) == bool(time.strip()):
        return "Error: give exactly one of in_seconds or time."
    try:
        day_list = _normalize_day_arg(days)
    except ValueError as exc:
        return f"Error: {exc}"
    action: dict[str, Any] = {
        "type": "set_schedule",
        "kind": "reminder",
        "label": text.strip(),
        "days": day_list,
    }
    if in_seconds > 0:
        if day_list:
            return "Error: a relative reminder cannot recur — use a clock time."
        action["seconds"] = int(in_seconds)
        when = f"in {_fmt_duration(int(in_seconds))}"
    else:
        if not re.match(r"^\d{2}:\d{2}$", time.strip()):
            return 'Error: time must be 24-hour "HH:MM".'
        action["at"] = time.strip()
        when = f"at {_fmt_clock(time.strip())}"
        if day_list:
            when += f" {_fmt_days(day_list)}"
    if room.strip():
        if _known_room(room) is None:
            return f"Error: no connected room named {room!r}."
        action["room"] = _known_room(room)
    if _assist_roomless(action):
        return _NEEDS_ROOM
    add_action(action)
    return f"Scheduled: reminder {when}."


@skill
async def list_schedules() -> str:
    """List the asking room's active timers, alarms, and reminders (with ids).

    Use to answer "what timers do I have?", "when is my alarm?", "how long is
    left?" and before cancelling anything with cancel_schedules.
    """
    items = _entries()
    if not items:
        where = " in this room" if request_channel() == "voice" else ""
        return f"No active timers, alarms, or reminders{where}."
    return "\n".join(f"[{e.get('id')}] {_describe(e)}" for e in items)


@skill
async def run_later(command: str, in_seconds: int = 0, time: str = "", room: str = "") -> str:
    """Defer a voice command to run later ("turn on the lights in 30 seconds").

    Use when the user wants an *action* performed after a delay or at a clock
    time and no more specific tool fits. The command replays at fire time
    exactly as if spoken then, in the same room. Give exactly one of
    `in_seconds` or `time`. **One-shot only** — for a recurring request
    ("every day at 8"), do not call this; suggest a Home Assistant automation
    instead.

    command: the command to run, phrased as a normal voice request
        (e.g. "turn on the porch light")
    in_seconds: run this many seconds from now
    time: 24-hour clock time as "HH:MM"
    room: run in this room's context instead of the asking room (required when
        the request doesn't come from a room speaker)
    """
    if not command.strip():
        return "Error: the command text is empty."
    if bool(in_seconds > 0) == bool(time.strip()):
        return "Error: give exactly one of in_seconds or time."
    action: dict[str, Any] = {"type": "set_schedule", "kind": "command",
                              "label": command.strip(), "days": []}  # fmt: skip
    if room.strip():
        if _known_room(room) is None:
            return f"Error: no connected room named {room!r}."
        action["room"] = _known_room(room)
    if in_seconds > 0:
        action["seconds"] = int(in_seconds)
        when = f"in {_fmt_duration(int(in_seconds))}"
    else:
        if not re.match(r"^\d{2}:\d{2}$", time.strip()):
            return 'Error: time must be 24-hour "HH:MM".'
        action["at"] = time.strip()
        when = f"at {_fmt_clock(time.strip())}"
    if _assist_roomless(action):
        return _NEEDS_ROOM
    add_action(action)
    return f"Scheduled: the command will run {when}."


@skill
async def cancel_schedules(ids: list[str]) -> str:
    """Cancel timers/alarms/reminders/scheduled commands by id (get ids from
    list_schedules).

    ids: the schedule ids to cancel
    """
    known = {str(e.get("id")) for e in _entries()}
    wanted = [str(i) for i in ids if str(i) in known]
    if not wanted:
        return "Error: no matching schedule ids — call list_schedules first."
    add_action({"type": "cancel_schedule", "ids": wanted})
    return f"Cancelled {len(wanted)} scheduled item" + ("s." if len(wanted) != 1 else ".")


def _normalize_day_arg(days: str) -> list[str]:
    out: set[str] = set()
    for tok in (t.strip().lower() for t in days.split(",")):
        if not tok:
            continue
        if tok in _DAY_GROUPS:
            out.update(_DAY_GROUPS[tok])
        elif tok in DAY_NAMES:
            out.add(tok)
        elif tok in _DAY_ALIASES:
            out.add(_DAY_ALIASES[tok])
        else:
            raise ValueError(f"unknown day {tok!r} (use mon..sun, weekdays, weekends, daily)")
    return [d for d in DAY_NAMES if d in out]


# ---------------------------------------------------------------------------
# Fast intents (the instant tier)
# ---------------------------------------------------------------------------

_ROOM_SUFFIX_RE = re.compile(r"^(?P<rest>.+?) in the (?P<room>[a-z' ]+)$")

_SET_TIMER_RES = (
    re.compile(
        r"^(?:set|start|create|make)(?: me)?(?: a| the| another)?"
        r"(?: (?P<label>[a-z]+))? timer for (?P<dur>.+)$"
    ),
    re.compile(r"^(?:set|start|create|make)(?: me)?(?: a| the| another)? (?P<dur>.+?) timer$"),
)
_TIMER_STATUS_RE = re.compile(
    r"^(?:how (?:much time|long)(?: is| do i have)?(?: left)?|how much longer|check|what's left)"
    r"(?: on| for)?(?: the| my)?(?: (?P<label>[a-z]+))? timers?$"
    r"|^how (?:much time|long) is left$|^how much time do i have left$"
)
_LIST_RE = re.compile(
    r"^(?:list|what) (?:my |the )?(?P<kind>timers?|alarms?|reminders?|commands?)"
    r"(?: do i have| are (?:set|running|active))?$"
)
_CANCEL_RE = re.compile(
    r"^(?:cancel|clear|delete|remove|stop)(?: the| my)?(?P<all> all(?: of)?(?: the| my)?)?"
    r"(?: (?P<label>[a-z]+))? (?P<kind>timers?|alarms?|reminders?|commands?)$"
)
# Deferred commands ("turn on the lights in 30 seconds"): a trailing duration or
# clock time turns any utterance into a scheduled replay. Greedy cmd → the LAST
# " in "/" at " splits, so "…the lights in the bedroom in 30 seconds" keeps its
# room phrase; the duration/clock parse is the gate that keeps "…in the bedroom"
# (not a duration) from matching. Runs after every other handler misses.
_DEFER_IN_RE = re.compile(r"^(?P<cmd>.+) in (?P<dur>[\w. -]+)$")
_DEFER_AT_RE = re.compile(r"^(?P<cmd>.+) at (?P<time>[\w:. ]+)$")
_SET_ALARM_RE = re.compile(
    r"^(?:(?P<wake>wake me(?: up)?)|set (?:an |the |a )?alarm)(?: for| at)? (?P<time>[\w:. ]+?)"
    r"(?: (?P<days>every [a-z ]+?|on weekdays|on weekends))?$"
)
# The to/that/about joiner is captured INTO the label so the fired announcement
# reads naturally ("You asked me to remind you to take the dog out." vs "…remind
# you that the game starts at eight."). Time-first orders require the joiner so
# the time part extends through "pm"/"and a half" instead of stopping early.
_REMIND_RES = (
    re.compile(r"^remind me (?P<what>(?:to |that |about )?.+) in (?P<dur>[\w. -]+)$"),
    re.compile(r"^remind me in (?P<dur>.+?) (?P<what>(?:to|that|about) .+)$"),
    re.compile(r"^remind me (?P<what>(?:to |that |about )?.+) at (?P<time>[\w:. ]+)$"),
    re.compile(r"^remind me at (?P<time>.+?) (?P<what>(?:to|that|about) .+)$"),
)

_VOICE = "Speak briefly and clearly, like a helpful assistant confirming a request."


def _normalize(text: str) -> str:
    text = text.strip().lower()
    text = re.sub(r"[!?.]+$", "", text).strip()
    text = re.sub(r"^(?:please|hey|ok|okay)[, ]+", "", text)
    text = re.sub(r"[, ]+please$", "", text)
    return re.sub(r"\s+", " ", text)


def _split_room(text: str) -> tuple[str, str | None]:
    """Split a trailing "… in the bedroom" off when it names a connected room."""
    m = _ROOM_SUFFIX_RE.match(text)
    if m:
        room = _known_room(m.group("room"))
        if room is not None:
            return m.group("rest").strip(), room
    return text, None


def _handle_set_timer(text: str) -> FastResult | None:
    for pattern in _SET_TIMER_RES:
        m = pattern.match(text)
        if not m:
            continue
        seconds = parse_duration(m.group("dur"))
        if seconds is None:
            return None  # "set a timer for my roast" → let the LLM ask
        label = (m.groupdict().get("label") or "").strip()
        if request_channel() != "voice":
            return FastResult.miss()  # no asking room — LLM tier asks for one
        add_action({"type": "set_schedule", "kind": "timer", "seconds": seconds, "label": label})
        name = f"{label.capitalize()} timer" if label else "Timer"
        return FastResult.handled(f"{name} set for {_fmt_duration(seconds)}.", _VOICE)
    return None


def _handle_timer_status(text: str) -> FastResult | None:
    m = _TIMER_STATUS_RE.match(text)
    if not m:
        return None
    label = (m.groupdict().get("label") or "").strip()
    timers = _entries("timer")
    if label:
        timers = [t for t in timers if (t.get("label") or "").lower() == label]
    if not timers:
        which = f"a {label} timer" if label else "any timers"
        return FastResult.handled(f"You don't have {which} running.", _VOICE)
    lines = []
    for t in timers:
        name = f"{t.get('label')} timer" if t.get("label") else "timer"
        lines.append(f"The {name} has {_fmt_duration(int(t.get('seconds_left', 0)))} left")
    return FastResult.handled(". ".join(lines) + ".", _VOICE)


def _handle_list(text: str) -> FastResult | None:
    m = _LIST_RE.match(text)
    if not m:
        return None
    kind = m.group("kind").rstrip("s")
    items = _entries(kind)
    if not items:
        return FastResult.handled(f"You don't have any {kind}s set.", _VOICE)
    return FastResult.handled(
        f"You have {len(items)}: " + "; ".join(_describe(e) for e in items) + ".", _VOICE
    )


def _handle_cancel(text: str) -> FastResult | None:
    m = _CANCEL_RE.match(text)
    if not m:
        return None
    kind = m.group("kind").rstrip("s")
    label = (m.groupdict().get("label") or "").strip()
    items = _entries(kind)
    if label:
        items = [e for e in items if (e.get("label") or "").lower() == label]
    if not items:
        which = f"a {label} {kind}" if label else f"any {kind}s"
        return FastResult.handled(f"You don't have {which} to cancel.", _VOICE)
    want_all = bool(m.group("all")) or m.group("kind").endswith("s")
    if len(items) > 1 and not want_all and not label:
        return FastResult.clarify(
            f"You have {len(items)} {kind}s: "
            + "; ".join(_describe(e) for e in items)
            + ". Which one should I cancel?"
        )
    ids = [str(e.get("id")) for e in items]
    add_action({"type": "cancel_schedule", "ids": ids})
    if len(ids) == 1:
        return FastResult.handled(f"Cancelled {_describe(items[0])}.", _VOICE)
    return FastResult.handled(f"Cancelled {len(ids)} {kind}s.", _VOICE)


def _handle_set_alarm(text: str) -> FastResult | None:
    text, room = _split_room(text)
    m = _SET_ALARM_RE.match(text)
    if not m:
        return None
    hhmm = parse_clock(m.group("time"), wake=bool(m.group("wake")))
    if hhmm is None:
        return None  # "wake me at sunrise" → LLM
    days = parse_days(m.group("days") or "")
    if days is None:
        return None  # unparsed recurrence phrase → LLM
    action: dict[str, Any] = {
        "type": "set_schedule",
        "kind": "alarm",
        "at": hhmm,
        "days": days,
        "label": "",
    }
    if room:
        action["room"] = room
    blocked = _alarm_blocked_room(room)
    if blocked is not None:
        return FastResult.handled(_alarm_refusal(blocked, here=not room), _VOICE)
    if _assist_roomless(action):
        return FastResult.miss()  # LLM tier asks for the room
    add_action(action)
    reply = f"Alarm set for {_fmt_clock(hhmm)}"
    if days:
        reply += f" {_fmt_days(days)}"
    if room:
        reply += f" in the {room}"
    return FastResult.handled(reply + ".", _VOICE)


def _handle_reminder(text: str) -> FastResult | None:
    if not text.startswith("remind me"):
        return None
    text, room = _split_room(text)
    for pattern in _REMIND_RES:
        m = pattern.match(text)
        if not m:
            continue
        gd = m.groupdict()
        what = gd["what"].strip()
        action: dict[str, Any] = {
            "type": "set_schedule",
            "kind": "reminder",
            "label": what,
            "days": [],
        }
        if "dur" in gd and gd.get("dur"):
            seconds = parse_duration(gd["dur"])
            if seconds is None:
                continue
            action["seconds"] = seconds
            when = f"in {_fmt_duration(seconds)}"
        else:
            hhmm = parse_clock(gd.get("time") or "")
            if hhmm is None:
                continue
            action["at"] = hhmm
            when = f"at {_fmt_clock(hhmm)}"
        if room:
            action["room"] = room
        if _assist_roomless(action):
            return FastResult.miss()  # LLM tier asks for the room
        add_action(action)
        reply = f"Okay — I'll remind you {when}"
        if room:
            reply += f" in the {room}"
        return FastResult.handled(reply + ".", _VOICE)
    return None


def _handle_deferred(text: str) -> FastResult | None:
    if request_channel() != "voice":
        return None  # no asking room for the replay — LLM tier (run_later) asks
    m = _DEFER_IN_RE.match(text)
    if m:
        seconds = parse_duration(m.group("dur"))
        if seconds is not None and len(m.group("cmd").strip()) >= 3:
            cmd = m.group("cmd").strip()
            add_action(
                {
                    "type": "set_schedule",
                    "kind": "command",
                    "label": cmd,
                    "seconds": seconds,
                    "days": [],
                }  # fmt: skip
            )
            return FastResult.handled(f"Okay — in {_fmt_duration(seconds)}: {cmd}.", _VOICE)
    m = _DEFER_AT_RE.match(text)
    if m:
        # A bare number is ambiguous here ("set the brightness at 5" is a level,
        # not 5 o'clock) — require a colon/meridiem/noon/midnight and let the
        # LLM disambiguate the rest. (Alarm phrasings keep bare hours: their
        # "wake me / set an alarm" prefix removes the ambiguity.)
        if re.fullmatch(r"\d{1,2}", m.group("time").strip()):
            return None
        hhmm = parse_clock(m.group("time"))
        if hhmm is not None and len(m.group("cmd").strip()) >= 3:
            cmd = m.group("cmd").strip()
            add_action(
                {
                    "type": "set_schedule",
                    "kind": "command",
                    "label": cmd,
                    "at": hhmm,
                    "days": [],
                }  # fmt: skip
            )
            return FastResult.handled(f"Okay — at {_fmt_clock(hhmm)}: {cmd}.", _VOICE)
    return None


@fast_intent(priority=95)
async def fast_schedule(utterance: str, room_id: str | None, speaker: str | None) -> FastResult:
    """Deterministic set/status/list/cancel for timers, alarms, reminders, and
    deferred commands ("turn on the lights in 30 seconds")."""
    text = _normalize(utterance)
    for handler in (
        _handle_set_timer,
        _handle_timer_status,
        _handle_list,
        _handle_cancel,
        _handle_set_alarm,
        _handle_reminder,
        _handle_deferred,  # last: only a trailing duration/clock time matches
    ):
        result = handler(text)
        if result is not None:
            return result
    return FastResult.miss()
