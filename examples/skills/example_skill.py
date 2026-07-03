"""A complete, runnable example skill — copy me and make me yours.

This one file demonstrates every mechanism a Kenzy skill can use:

  * ``@skill``        — a tool the LLM calls, schema generated from the signature
  * ``@fast_intent``  — a deterministic matcher that answers *instantly*, no model
  * ``get_config``    — per-skill settings from llm.yaml (``skills.example_skill``)
  * ``get_request``   — server-injected context (asking room, connected rooms)
  * ``add_action``    — asking the *server* to do something (here: announce)

Try it:
  1. Copy this file into your skills overlay:  ~/.config/kenzy/skills/
  2. Restart the LLM service:  systemctl --user restart kenzy-llm
     (or the Restart button on the dashboard's Services → llm page)
  3. Check the dashboard's Skills tab — get_fortune, share_fortune, and
     fast_fortune should be listed.
  4. Say:  "hey Kenzie… give me a fortune"          (the fast path — instant)
           "hey Kenzie… what does my future hold?"  (the LLM path → get_fortune)
           "hey Kenzie… share a fortune with the kitchen"  (a server action)

Optional config, in the dashboard under Services → llm (or llm.yaml):

    skills:
      example_skill:
        fortunes:
          - "Your compile will succeed on the first try."
          - "A forgotten chore will reveal itself at bedtime."

Full authoring guide: https://docs.kenzy.dev/skills/writing-skills/
"""

from __future__ import annotations

import random
import re

from kenzy.llm.skills import FastResult, add_action, fast_intent, get_config, get_request, skill

# A skill can ship sensible defaults and let llm.yaml override them (get_config).
_DEFAULT_FORTUNES = [
    "Good news will find you before the kettle boils.",
    "The device you are about to reboot did not need rebooting.",
    "An unexpected guest brings expected chaos.",
    "Today favors the well-rested and the well-caffeinated.",
    "Something you lost is exactly where you left it.",
]


def _pick_fortune() -> str:
    fortunes = get_config("example_skill", "fortunes") or _DEFAULT_FORTUNES
    return str(random.choice(list(fortunes)))


# ---------------------------------------------------------------------------
# Tier 1: the LLM tool. The docstring is the API — the model reads it to decide
# WHEN to call the skill and how to fill the arguments, so spell both out.
# ---------------------------------------------------------------------------


@skill
async def get_fortune(topic: str = "") -> str:
    """Tell the user a lighthearted fortune-cookie style fortune.

    Use when the user asks for a fortune, a prediction, or what their future
    holds — e.g. "give me a fortune", "what does my future hold?", "read my
    fortune". This is entertainment: never present it as a real prediction.

    topic: optional subject the user asked about (e.g. "work", "dinner") —
        mention it when weaving the fortune into your reply.
    """
    fortune = _pick_fortune()
    # Returning a plain string hands the result back to the model, which writes
    # the final spoken reply. On failure, return a human-readable sentence
    # instead of raising — the model will relay it gracefully.
    return f"Fortune (topic: {topic or 'general'}): {fortune}"


# ---------------------------------------------------------------------------
# Tier 2: a server action. A skill runs inside kenzy-llm, which holds no node
# connections — to make sound in ANOTHER room, queue an action the server
# actuates after the reply is spoken. Validate targets against the injected
# request context (get_request) so the model can't invent rooms.
# ---------------------------------------------------------------------------


@skill
async def share_fortune(room: str) -> str:
    """Speak a fortune aloud in another room of the house.

    Use when the user asks to send/share a fortune with a different room —
    e.g. "share a fortune with the kitchen".

    room: the target room's name — must be one of the currently connected rooms.
    """
    rooms = [str(r) for r in (get_request("rooms") or [])]
    match = next((r for r in rooms if r.lower() == room.strip().lower()), None)
    if match is None:
        connected = ", ".join(rooms) if rooms else "none"
        return f"Error: no connected room called {room!r} (connected: {connected})."
    add_action({"type": "announce", "text": f"A fortune for you: {_pick_fortune()}",
                "rooms": [match]})  # fmt: skip
    return f"Queued a fortune announcement for the {match}."


# ---------------------------------------------------------------------------
# Tier 3: the fast intent — instant, deterministic, no model call. Keep it
# HIGH-PRECISION: match only what you're sure about and miss() everything else;
# the LLM (and get_fortune above) is the safety net for fuzzy phrasings.
# ---------------------------------------------------------------------------

_FORTUNE_RE = re.compile(
    r"^(?:give me|tell me|read me|i want)(?: a| my)? fortune(?: cookie)?$|^fortune cookie$"
)


@fast_intent(priority=10)  # low: let the built-in intents (time, HA…) go first
async def fast_fortune(utterance: str, room_id: str | None, speaker: str | None) -> FastResult:
    """Instant fortunes for the common phrasings; anything fuzzier → the LLM."""
    text = re.sub(r"[!?.]+$", "", utterance.strip().lower()).strip()
    if not _FORTUNE_RE.match(text):
        return FastResult.miss()
    where = f" here in the {room_id}" if room_id else ""
    return FastResult.handled(
        f"Your fortune{where}: {_pick_fortune()}",
        # The optional voice_prompt steers the TTS delivery for this reply.
        "Speak like a mysterious but friendly fortune teller.",
    )
