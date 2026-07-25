"""
Knock-knock jokes — both directions, entirely on the fast path.

Kenzy TELLS one ("tell me a knock knock joke") and PLAYS ALONG when you start
one ("Knock knock." → "Who's there?"). Beyond the fun, this skill is the
canonical consumer of ``ask(busy_cues=False)``: a knock-knock exchange is pure
conversational rhythm — three rapid turns where an interjected "Working on
it." would flatten the joke — so every question here opts out of the
processing-cue ladder. Voice-channel only (the typed Assist lane can't hold a
floor; the LLM can type out a joke there instead).

The wake word bails out mid-joke like any ask; a lapsed reply window just ends
the exchange quietly (the node's own end-of-dialog cue plays).
"""

from __future__ import annotations

import random
import re

from kenzy.llm.asking import ask
from kenzy.llm.skills import FastResult, fast_intent, request_channel, skill

_VOICE = "Playful and warm — you are trading knock-knock jokes."

# (setup, punchline) — clean classics; short setups survive STT well.
_JOKES: list[tuple[str, str]] = [
    ("Boo", "Don't cry — it's just a joke!"),
    ("Lettuce", "Lettuce in, it's cold out here!"),
    ("Olive", "Olive you, and now you know it!"),
    ("Tank", "You're welcome!"),
    ("Harry", "Harry up and open the door!"),
    ("Honeydew", "Honeydew you want to hear another one?"),
    ("Cow says", "No, silly — a cow says moo!"),
    ("Annie", "Annie body going to open this door?"),
]
_last_joke: int | None = None  # no-immediate-repeat guard (module-level, like cue pools)

_REACTIONS = [
    "Ha! Good one.",
    "Heh — I walked right into that.",
    "Nice one!",
    "Ha ha! I'm keeping that one.",
]

# Anchored whole-utterance patterns (the "time for dinner" discipline): the
# telling intent requires the word "joke"; the responding intent is exactly a
# spoken "knock knock" — so neither can shadow the other or fire mid-sentence.
_TELL_RE = re.compile(
    r"^(?:hey\s+)?(?:please\s+)?(?:can you\s+|could you\s+|will you\s+)?(?:please\s+)?"
    r"(?:tell (?:me|us) |do you know )?(?:a|another|any) knock[\s,.-]*knock jokes?[.!?]*$",
    re.IGNORECASE,
)
_KNOCK_RE = re.compile(r"^knock[\s,.-]*knock[.!?\s]*$", re.IGNORECASE)


def _pick_joke() -> tuple[str, str]:
    global _last_joke
    idx = random.choice([i for i in range(len(_JOKES)) if i != _last_joke])
    _last_joke = idx
    return _JOKES[idx]


def _clean(answer: str) -> str:
    """Trim STT punctuation from a short answer ("Boo." → "Boo")."""
    return answer.strip().strip(".!?,;: ").strip()


async def _tell_joke() -> str:
    setup, punchline = _pick_joke()
    # Whatever they answer to "Knock knock!" — invariably "Who's there?" — the
    # joke proceeds; validating the ritual would only add failure modes.
    if await ask("Knock knock!", busy_cues=False) is None:
        return ""  # wake word / lapsed window — the room has moved on
    if await ask(f"{setup}.", busy_cues=False) is None:
        return ""
    return punchline


async def _play_along() -> str:
    who = await ask("Who's there?", busy_cues=False)
    if who is None or not _clean(who):
        return ""
    name = _clean(who)
    punchline = await ask(f"{name} who?", busy_cues=False)
    if punchline is None:
        return ""
    return random.choice(_REACTIONS)


@fast_intent(priority=60)
async def fast_knock_knock(
    utterance: str, room_id: str | None, speaker: str | None
) -> FastResult:
    if request_channel() != "voice":
        return FastResult.miss()  # a typed lane can't hold the floor for turns
    text = utterance.strip()
    if _TELL_RE.match(text):
        return FastResult.handled(await _tell_joke(), _VOICE)
    if _KNOCK_RE.match(text):
        return FastResult.handled(await _play_along(), _VOICE)
    return FastResult.miss()


@skill
async def knock_knock_joke() -> str:
    """Tell the user a knock-knock joke, performing the whole call-and-response
    out loud (use for any request for a knock-knock joke specifically; ordinary
    joke requests you can answer yourself)."""
    if request_channel() != "voice":
        # No floor to hold on a typed channel — type one out flat instead.
        setup, punchline = _pick_joke()
        return f"Knock knock. (Who's there?) {setup}. ({setup} who?) {punchline}"
    return await _tell_joke()
