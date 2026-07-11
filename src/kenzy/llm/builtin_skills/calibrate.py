"""Voice-initiated audio calibration — "Hey Kenzy, calibrate".

The skill only queues a ``start_calibration`` action; the server runs the
guided flow (spoken prompts → quiet phase → wake-word phase → apply, the same
math as the dashboard wizard, from ``kenzy.calibration``) on the asking node.
Audio-device selection deliberately stays dashboard-only: a wrong device can't
hear voice commands in the first place.
"""

from __future__ import annotations

import re

from kenzy.llm.skills import FastResult, add_action, fast_intent, skill  # type: ignore[import]

_VOICE_PROMPT = "Calm, brief acknowledgement."

# Bare calibration requests only — "calibrate the thermostat" or other objects
# must fall through to the LLM (which can explain or refuse sensibly).
_CALIBRATE_RE = re.compile(
    r"^(?:please )?(?:re)?calibrate"
    r"(?: (?:yourself|your (?:hearing|ears|audio|mic|microphone)"
    r"|the (?:audio|mic|microphone)|audio|this room))?(?: please)?$"
)
_RUN_RE = re.compile(r"^(?:please )?run (?:audio |mic |microphone )?calibration(?: please)?$")


def _normalize(utterance: str) -> str:
    return re.sub(r"[^\w\s]", "", utterance).strip().lower()


@skill
async def calibrate_audio() -> str:
    """Calibrate Kenzy's hearing (microphone thresholds) for the room the user is
    speaking in. Use when the user asks to calibrate the audio, microphone, or
    Kenzy's hearing/listening — e.g. "calibrate", "recalibrate your hearing",
    "this room is noisy, recalibrate". Keep your reply very short: the guided
    flow speaks its own instructions right after.
    """
    add_action({"type": "start_calibration"})
    return "Starting calibration — follow the spoken instructions."


@fast_intent(priority=90)
async def fast_calibrate(utterance: str, room_id: str | None, speaker: str | None) -> FastResult:
    """Deterministic trigger for the exact calibration phrasings (no LLM)."""
    text = _normalize(utterance)
    if not (_CALIBRATE_RE.match(text) or _RUN_RE.match(text)):
        return FastResult.miss()
    add_action({"type": "start_calibration"})
    return FastResult.handled("Okay, let's calibrate.", _VOICE_PROMPT)
