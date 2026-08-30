"""
Volume — deterministic fast-path control of the asking node's playback volume.

Handles "turn it up / down", "louder / quieter", "mute / unmute", and "set the
volume to 40" instantly, with no LLM call. The LLM service can't change a node's
volume itself, so the matcher queues a server-side **action** targeting the asking
node; the server applies it (volume persists via config-pull, mute is a transient
runtime toggle). While muted the wake-word ready chime stays audible so the user
can tell the device is listening and knowingly unmute.

Ships both forms (the datetime pattern): the fast_intent for the classic
pipeline, and the ``set_speaker_volume`` skill — the tool twin the v6
conversation path uses (that path has no fast intents; the model adjusts the
volume as a tool call, through the same server action).
"""

from __future__ import annotations

import re

from kenzy.llm.skills import (  # type: ignore[import]
    FastResult,
    add_action,
    fast_intent,
    is_node_bound_refused,
    skill,
)

_VOICE_PROMPT = "Speak naturally at a conversational pace."

# Relative step for a bare "turn it up / down" (percentage points).
_STEP = 15

# Naming a media thing ("mute the TV", "turn the music up") means the room's
# media player, not the Kenzy node — miss so the home_assistant skill handles it.
_MEDIA_RE = re.compile(r"\b(tv|television|music|movie|show|media|stereo|speakers?)\b")
_UNMUTE_RE = re.compile(r"\bunmute\b|\bturn (the )?(sound|volume|audio) back on\b")
_MUTE_RE = re.compile(r"\b(mute|be quiet|silence yourself|shut up)\b")
# A number for "set the volume to N" / "volume at N percent".
_SET_RE = re.compile(r"\b(?:set\s+)?(?:the\s+)?(?:volume|sound)\s+(?:to|at)?\s*(\d{1,3})\b")
_PCT_RE = re.compile(r"\b(\d{1,3})\s*(?:percent|%)\b")
_UP_RE = re.compile(
    r"\b(volume up|turn (it|the volume|the sound|up the volume) up|louder|speak up|"
    r"turn it up)\b"
)
_DOWN_RE = re.compile(
    r"\b(volume down|turn (it|the volume|the sound|down the volume) down|quieter|"
    r"softer|turn it down|lower the volume)\b"
)


def classify(utterance: str) -> tuple[str, int | None] | None:
    """Return ``(kind, value)`` for a volume command, or None for a miss.

    ``kind`` is one of ``mute``/``unmute``/``set``/``up``/``down``; ``value`` is the
    target level for ``set`` (else None). Pure + side-effect free for unit testing.
    """
    text = utterance.strip().lower()
    if not text:
        return None
    if _MEDIA_RE.search(text):
        return None
    if _UNMUTE_RE.search(text):
        return ("unmute", None)
    if _MUTE_RE.search(text):
        return ("mute", None)
    # An explicit level ("set the volume to 60", "volume 75") wins over up/down.
    m = _SET_RE.search(text) or (
        _PCT_RE.search(text) if "volume" in text or "sound" in text else None
    )
    if m:
        return ("set", int(m.group(1)))
    if _UP_RE.search(text):
        return ("up", None)
    if _DOWN_RE.search(text):
        return ("down", None)
    return None


@skill
async def set_speaker_volume(action: str, level: int | None = None) -> str:
    """Adjust Kenzy's own speaker volume on the device being spoken to, or
    mute/unmute it.

    Use when the user asks Kenzy herself to be louder, quieter, muted, or set
    to a level — "turn it up", "quieter", "mute yourself", "set the volume to
    40". NOT for TVs, music, or media players — use the home control tools
    for those.

    action: one of "up", "down", "set", "mute", "unmute".
    level: target volume percent (0-100); required when action is "set",
           ignored otherwise.
    """
    refused = is_node_bound_refused()  # no asking node on the assist channel
    if refused:
        return refused
    kind = (action or "").strip().lower()
    if kind == "mute":
        add_action({"type": "set_volume", "muted": True})
        return "Muted. The wake word still works and unmutes."
    if kind == "unmute":
        add_action({"type": "set_volume", "muted": False})
        return "Unmuted."
    if kind == "set":
        if level is None:
            return "A target level (0-100) is required to set the volume."
        clamped = max(0, min(100, int(level)))
        add_action({"type": "set_volume", "level": clamped})
        return f"Volume set to {clamped} percent."
    if kind == "up":
        add_action({"type": "set_volume", "delta": _STEP})
        return "Volume raised."
    if kind == "down":
        add_action({"type": "set_volume", "delta": -_STEP})
        return "Volume lowered."
    return 'Unknown action — use "up", "down", "set", "mute", or "unmute".'


@fast_intent(priority=90)
async def fast_volume(utterance: str, room_id: str | None, speaker: str | None) -> FastResult:
    """Adjust the asking node's volume / mute without the LLM."""
    result = classify(utterance)
    if result is None:
        return FastResult.miss()
    refused = is_node_bound_refused()  # F3: no asking node on the assist channel
    if refused:
        return FastResult.handled(refused, _VOICE_PROMPT)
    kind, value = result

    if kind == "mute":
        add_action({"type": "set_volume", "muted": True})
        return FastResult.handled("Muting. Say the wake word to unmute.", _VOICE_PROMPT)
    if kind == "unmute":
        add_action({"type": "set_volume", "muted": False})
        return FastResult.handled("Unmuted.", _VOICE_PROMPT)
    if kind == "set":
        level = max(0, min(100, int(value or 0)))
        add_action({"type": "set_volume", "level": level})
        return FastResult.handled(f"Volume set to {level} percent.", _VOICE_PROMPT)
    if kind == "up":
        add_action({"type": "set_volume", "delta": _STEP})
        return FastResult.handled("Turning it up.", _VOICE_PROMPT)
    # down
    add_action({"type": "set_volume", "delta": -_STEP})
    return FastResult.handled("Turning it down.", _VOICE_PROMPT)
