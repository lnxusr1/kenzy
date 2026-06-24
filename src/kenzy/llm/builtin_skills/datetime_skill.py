"""
Time and date — deterministic fast-path intent (Stage 0).

Answers "what time is it" / "what's the date" instantly with no LLM and no
network.  Uses the home timezone from llm.yaml (location.timezone) when set,
otherwise the system local time.

This is a fast_intent only: if it misses, the LLM still answers because the
current date/time is already injected into the LLM's context.
"""

from __future__ import annotations

import datetime
import random
import re

from kenzy.llm.skills import FastResult, fast_intent, get_config  # type: ignore[import]

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

# An interrogative must be present so we never hijack commands like
# "set a timer" or statements that merely mention the time.
_INTERROGATIVES = {"what", "whats", "tell", "current", "currently"}
_TIME_WORDS = {"time", "oclock"}
_DATE_WORDS = {"date"}


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
    words = set(text.split())

    is_time = bool(words & _TIME_WORDS)
    is_date = bool(words & _DATE_WORDS) or "what day" in text or "day is it" in text
    if not (is_time or is_date):
        return None

    # Gate on an interrogative (or a bare "time"/"date") to avoid false hits.
    if not (words & _INTERROGATIVES) and text not in ("time", "date"):
        return None

    if is_time and is_date:
        return "both"
    return "time" if is_time else "date"


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
