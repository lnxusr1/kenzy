"""Spoken control of proactive speech (5.0.6).

Two different things people say to a house that is making noise at them, and
conflating them is dangerous:

* **"Stop the alerts" / "turn off the alerts"** — *make this noise stop*. It
  silences whatever is currently sounding, until that sensor goes off and trips
  again. It changes nothing about the future.
* **"Disable the alerts"** — *stop doing this at all*. The whole feature off,
  persisted across restarts.

The first draft matched both with one pattern, so the phrase you would most
naturally shout at a blaring smoke alarm — "stop the alerts" — turned off every
future safety announcement, permanently and silently. The ordering made it worse:
the session opening already silenced the alarm, so it did exactly what you wanted
in the moment and the second half went unnoticed until something later went
unannounced. Keeping "stop" and "disable" apart is the whole point of this
module.

**Deliberately fast intents, never the model.** If unprompted speech is
misbehaving, the language model is a suspect — it may be slow, wrong, or the
reason she is talking. An off-switch that needs the model fails exactly when you
need it. Same reasoning that puts "Hey Kenzy, stop." on the instant path.

**Trust split.** Silencing is open to anyone: a guest or a child standing in
front of a shrieking speaker should be able to make it stop. Disabling is a
settings change, so it is gated to a recognized voice.
"""

from __future__ import annotations

import re

from kenzy.llm.skills import (  # type: ignore[import]
    FastResult,
    add_action,
    ask,
    fast_intent,
)

_VOICE_PROMPT = "Speak naturally at a conversational pace."

#: Confirmation vocabulary, deliberately STRICT: the whole utterance must be a
#: plain yes. Turning off smoke and leak announcements is not something to infer
#: from an "alright then" that happened to contain an affirmative word — and
#: unlike consenting to an intercom call, guessing wrong here is silent and
#: lasts until somebody notices months later. Anything unrecognized, and any
#: timeout, leaves the alerts ON.
_YES = frozenset({"yes", "yeah", "yep", "yes please", "do it", "confirm", "confirmed", "correct"})


def _is_yes(answer: str | None) -> bool:
    if answer is None:
        return False
    return " ".join(re.sub(r"[^\w\s]", "", answer).lower().split()) in _YES

#: What she may speak up about, in the words people actually use for it.
_SUBJECT = r"(?:alerts?|alarms?|announcements?|notifications?|warnings?)"
_THE = r"(?:the\s+|all\s+(?:the\s+)?|your\s+|that\s+|this\s+)?"

#: "Make it stop" — the noise happening NOW. Never touches future alerts.
_SILENCE_RE = re.compile(
    rf"\b(?:stop|turn off|switch off|shut off|shut up|quiet|silence|cancel|"
    rf"enough of)\s+{_THE}{_SUBJECT}\b",
    re.I,
)
#: "Stop doing this at all" — the capability. Deliberately needs the word
#: "disable" (or an explicit "permanently"), which nobody shouts by reflex.
_DISABLE_RE = re.compile(
    rf"\b(?:disable|deactivate)\s+{_THE}{_SUBJECT}\b"
    rf"|\b(?:stop|turn off|switch off)\s+{_THE}{_SUBJECT}\s+(?:permanently|for good|entirely)\b",
    re.I,
)
_ENABLE_RE = re.compile(
    rf"\b(?:enable|reenable|re-enable|reactivate|start|turn on|switch on|resume)\s+"
    rf"{_THE}{_SUBJECT}\b",
    re.I,
)


def classify(utterance: str) -> str | None:
    """``"silence"`` / ``"disable"`` / ``"enable"`` / None. Pure, so the
    phrasing is testable on its own."""
    text = (utterance or "").strip()
    if not text:
        return None
    # Order matters: "stop the alerts permanently" is a disable, and it also
    # matches the silence pattern's "stop the alerts" prefix. The more specific
    # intent has to win.
    if _DISABLE_RE.search(text):
        return "disable"
    if _ENABLE_RE.search(text):
        return "enable"
    if _SILENCE_RE.search(text):
        return "silence"
    return None


@fast_intent(priority=93)
async def fast_proactive_silence(
    utterance: str, room_id: str | None, speaker: str | None
) -> FastResult:
    """"Stop the alerts" — quiet what's sounding now. Open to any voice: whoever
    is standing in front of a shrieking speaker gets to make it stop."""
    if classify(utterance) != "silence":
        return FastResult.miss()
    # Belt and braces. Opening a session already silences live alerts, but that
    # is a side effect of session start — an explicit request should not depend
    # on it staying that way.
    add_action({"type": "silence_proactive"})
    return FastResult.handled(
        "Okay, I've quieted that. I'll speak up again if it clears and comes back.",
        _VOICE_PROMPT,
    )


@fast_intent(priority=92, min_tier="recognized")
async def fast_proactive_control(
    utterance: str, room_id: str | None, speaker: str | None
) -> FastResult:
    """"Disable the alerts" — the whole feature off, and back on again."""
    verdict = classify(utterance)
    if verdict == "disable":
        # Consequential actions confirm (standing decision 8), and this is the
        # most consequential thing anyone can say to Kenzy in one breath. The
        # friction is affordable here precisely BECAUSE the urgent path —
        # "stop the alerts" — has none: nobody has to answer a question to make
        # a shrieking speaker be quiet.
        answer = await ask(
            "Are you sure you want to permanently disable the safety alerts? "
            "That includes smoke and water leaks."
        )
        if not _is_yes(answer):
            # Covers "no", anything ambiguous, and the window expiring. The
            # current alert was already silenced when this session opened, so
            # declining still leaves the house quiet — it just stays armed.
            return FastResult.handled("Okay, I'll leave the alerts on.", _VOICE_PROMPT)
        add_action({"type": "set_proactive", "enabled": False})
        return FastResult.handled(
            "Alerts have been disabled. The dashboard shows they're off, and you can turn "
            'them back on any time by saying "enable the alerts".',
            _VOICE_PROMPT,
        )
    if verdict == "enable":
        add_action({"type": "set_proactive", "enabled": True})
        return FastResult.handled("Okay, I'll speak up about things again.", _VOICE_PROMPT)
    return FastResult.miss()
