"""
Social niceties — instant greetings and a conversational bail-out.

Fast intents only (no LLM, no network): a "hello" or a "never mind" shouldn't
cost a model round-trip, and — like the rest of the fast path — they keep
working with no internet at all.

Deliberately NOT handling "thanks"/"thank you": Whisper tends to hallucinate
those from noise (they're very common in its training data), so a deterministic
"you're welcome" would fire on phantom transcriptions. Gratitude stays with the
LLM, which can shrug it off harmlessly.
"""

from __future__ import annotations

import datetime
import random
import re

from kenzy.llm.skills import FastResult, fast_intent, get_config

_VOICE = "Speak warmly and naturally."

# Response pool — from the prior version's greetings list; {day} → the current
# part of day. Varied so Kenzy isn't a recording.
_GREETINGS = [
    "Hello back.",
    "Hi there.",
    "Good {day}.",
    "Howdy.",
    "Long time no see.",
    "Greetings and salutations!",
    "Greetings.",
    "What's happening?",
    "What's new?",
    "Hi, how have you been?",
    "Look what the cat dragged in. Ha. Ha. Ha.",
    "What's going on?",
]
_GOODNIGHTS = ["Good night!", "Night!", "Sleep well.", "Sweet dreams."]
_ACK = ["Okay, no problem.", "No problem.", "Sure thing.", "Okay."]

# Anchored WHOLE-utterance matches — a greeting with a tail ("hello, turn on the
# lights") misses and goes to the LLM/action path. Short tokens Whisper commonly
# hallucinates (thanks, hey, yo) are deliberately excluded.
_GREETING_RE = re.compile(
    r"^(?:hello|hi|hi there|howdy|greetings|long time no see|what'?s up|"
    r"good (?:morning|afternoon|evening)|morning|afternoon|evening)$"
)
_NIGHT_RE = re.compile(r"^(?:good ?night|goodnight|night night|nighty night)$")
# Conversational bail-outs, anchored whole-utterance — these get a brief spoken
# ack. The quiet-DEMANDING family ("stop", "quiet", "hush", "shut up"…) is
# deliberately NOT here: the server's _STOP_PHRASES gate ends those sessions
# silently before the LLM service is even called (no ack — they asked for quiet).
_NEVERMIND_RE = re.compile(
    r"^(?:never ?mind|forget it|forget that|nvm|cancel|cancel that)$"
)


def _day_part() -> str:
    name = get_config("location", "timezone", "") or ""
    tz: datetime.tzinfo | None = None
    if name:
        try:
            import zoneinfo

            tz = zoneinfo.ZoneInfo(name)
        except Exception:
            tz = None
    hour = datetime.datetime.now(tz=tz).hour
    if 5 <= hour < 12:
        return "morning"
    if 12 <= hour < 17:
        return "afternoon"
    return "evening"


def _normalize(text: str) -> str:
    # Drop punctuation (keep apostrophes for "what's"), collapse whitespace.
    text = re.sub(r"[^\w\s']", "", text).strip().lower()
    return re.sub(r"\s+", " ", text)


@fast_intent(priority=98)
async def fast_greeting(utterance: str, room_id: str | None, speaker: str | None) -> FastResult:
    """Greetings and goodnights, answered instantly with no model call."""
    text = _normalize(utterance)
    if _NIGHT_RE.match(text):
        return FastResult.handled(random.choice(_GOODNIGHTS), _VOICE)
    if _GREETING_RE.match(text):
        # Time-specific greetings echo the right part of day; the rest pick a
        # varied response from the pool.
        if text in ("good morning", "morning"):
            return FastResult.handled("Good morning!", _VOICE)
        if text in ("good afternoon", "afternoon"):
            return FastResult.handled("Good afternoon!", _VOICE)
        if text in ("good evening", "evening"):
            return FastResult.handled("Good evening!", _VOICE)
        return FastResult.handled(random.choice(_GREETINGS).format(day=_day_part()), _VOICE)
    return FastResult.miss()


@fast_intent(priority=97)
async def fast_nevermind(utterance: str, room_id: str | None, speaker: str | None) -> FastResult:
    """Bail out of an exchange: "never mind" / "cancel" → a quick ack.

    Returns handled with ``expect_response`` unset, so when this lands during a
    held dialog the server's floor-hold logic ends the conversation cleanly — a
    fast reply that doesn't ask for a response closes the floor. No special
    plumbing needed. Anchored to the bare phrase so "forget the eggs on the
    list" still routes to the lists skill and "cancel the alarm" to schedules.
    ("stop"/"quiet"/"hush" never reach this service — the server's
    _STOP_PHRASES gate ends those sessions silently, which is the point.)
    """
    if _NEVERMIND_RE.match(_normalize(utterance)):
        return FastResult.handled(random.choice(_ACK), _VOICE)
    return FastResult.miss()
