"""
Time and date — deterministic fast-path intent (Stage 0).

Answers "what time is it" / "what's the date" instantly with no LLM and no
network.  Uses the home timezone from llm.yaml (location.timezone) when set,
otherwise the system local time.

Ships both forms: the fast_intent for the classic pipeline (fully supported,
first-class — if it misses, the LLM still answers because the current
date/time is injected into its context), and the ``get_datetime`` skill — the
tool twin the v6 conversation path uses (that path has no fast intents; the
model asks for the live clock as a tool call instead).
"""

from __future__ import annotations

import datetime
import random
import re

from kenzy.llm.skills import FastResult, fast_intent, get_config, skill  # type: ignore[import]

_VOICE_PROMPT = "Speak naturally at a conversational pace."

_TIME_TEMPLATES = [
    "It's {time}.",
    "It's currently {time}.",
    "The time is {time}.",
]
_DATE_TEMPLATES = [
    "Today is {date}.",
    "It's {date}.",
]

# Anchored whole-utterance patterns — high precision or miss. A bag-of-words
# gate ("tell"/"what" + "time" anywhere) hijacked commands that merely *mention*
# time ("tell the house it's time for dinner" answered the clock); anchoring
# means "time" must BE the question, not the object of a larger command. A
# trailing qualifier also misses ("what time is it in london" → LLM, which can
# actually answer it). Courtesy wrappers are stripped first.
_COURTESY_PREFIX = re.compile(
    r"^(?:(?:hey |ok )?kenzy[,\s]+|please\s+|can you\s+|could you\s+|would you\s+|will you\s+)+"
)
_BOTH_RE = re.compile(
    r"^(?:whats?|what is) (?:the )?(?:time and date|date and time)(?: is it)?(?: today)?$"
)
_TIME_RE = re.compile(
    r"^(?:"
    r"(?:whats?|what is)(?: the)?(?: current)? time(?: is it)?(?: right now| now| today)?"
    r"|tell me (?:the time|what time it is)"
    r"|(?:do you have|have you got|got) the time"
    r"|current time"
    r"|time"
    r")$"
)
_DATE_RE = re.compile(
    r"^(?:"
    r"(?:whats?|what is)(?: the)?(?: todays)? date(?: today)?"
    r"|whats todays date"
    r"|what day is it(?: today)?"
    r"|what day of the week is it"
    r"|tell me (?:the date|todays date)"
    r"|todays date"
    r"|date"
    r")$"
)


def _now() -> datetime.datetime:
    name = get_config("location", "timezone", "") or ""
    tz: datetime.tzinfo | None = None
    if name:
        try:
            import zoneinfo

            tz = zoneinfo.ZoneInfo(name)
        except Exception:
            tz = None
    return datetime.datetime.now(tz=tz)


def _normalize(text: str) -> str:
    # Drop punctuation including apostrophes so "what's" → "whats" and
    # "o'clock" → "oclock", matching the keyword sets below.
    return re.sub(r"[^\w\s]", "", text).strip().lower()


def classify(utterance: str) -> str | None:
    """Return "time", "date", "both", or None for a normalized utterance.

    Pure and side-effect free so it can be unit-tested without config or clocks.
    """
    text = _normalize(utterance)
    text = _COURTESY_PREFIX.sub("", text).removesuffix(" please").strip()

    if _BOTH_RE.match(text):
        return "both"
    if _TIME_RE.match(text):
        return "time"
    if _DATE_RE.match(text):
        return "date"
    return None


@skill
async def get_datetime() -> str:
    """Report the current date and time in the home's timezone.

    Use whenever the user asks the time, the date, or the day of the week —
    "what time is it", "what's the date today", "what day is it". Returns the
    live clock; always call this rather than guessing from context.
    """
    now = _now()
    return now.strftime("%A, %B %-d, %Y, %-I:%M %p").strip()


@fast_intent(priority=100)
async def fast_datetime(utterance: str, room_id: str | None, speaker: str | None) -> FastResult:
    """Answer time/date questions instantly without the LLM."""
    kind = classify(utterance)
    if kind is None:
        return FastResult.miss()

    now = _now()
    time_str = now.strftime("%-I:%M %p").strip()
    date_str = now.strftime("%A, %B %-d").strip()

    if kind == "both":
        msg = f"It's {time_str} on {date_str}."
    elif kind == "time":
        msg = random.choice(_TIME_TEMPLATES).format(time=time_str)
    else:
        msg = random.choice(_DATE_TEMPLATES).format(date=date_str)

    return FastResult.handled(msg, _VOICE_PROMPT)
