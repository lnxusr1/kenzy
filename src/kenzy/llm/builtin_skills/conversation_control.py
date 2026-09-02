"""On-demand conversation entry (6.0.x) — "start a conversation".

The classic pipeline's spoken door into conversation mode. In ``on_demand``
mode the house rests on the classic pipeline; saying "start a conversation"
(or "let's talk", "continue a conversation") turns *this* one session
conversational: the reply holds the floor (``expect_response``) and a server
action arms the s2s bridge, so the next utterance opens an engine conversation
instead of a classic continuation.

Fast intent, not the model: entering conversation mode is a control command,
so it stays deterministic and instant — and it lives on the classic path, which
is the only path running when a conversation is NOT already open (in ``always``
mode the wake already opened one; in ``off`` there is nothing to escalate). The
deterministic *exit* ("stop the conversation") is the server's stop gate, not
this skill — a hot mic must not depend on the model to close.

Design: kenzy-design/app/s2s-design.md, "On-demand conversation mode".
"""

from __future__ import annotations

import re

from kenzy.llm.skills import (  # type: ignore[import]
    FastResult,
    add_action,
    fast_intent,
    get_request,
)

_VOICE = "Speak warmly and naturally."

# Anchored to the whole utterance so "let's talk about dinner" still routes to
# the model as a request — only the bare conversation-entry phrases match.
_CONV_RE = re.compile(
    r"""
    ^(?:
        (?:let'?s|can\ we|could\ we|i(?:'d\ like|\ want)\ to)\s
            (?:have\s)?(?:a\s|another\s|our\s)?(?:conversation|chat)
      | (?:start|begin|open|continue|resume|have)\s
            (?:a\s|the\s|another\s|our\s|my\s|this\s|that\s)?(?:conversation|chat)
      | let'?s\s(?:chat|talk)
    )$
    """,
    re.IGNORECASE | re.VERBOSE,
)


def _norm(text: str) -> str:
    return re.sub(r"[\s]+", " ", text.strip().rstrip(".!?,").lower())


@fast_intent(priority=94)
async def fast_start_conversation(
    utterance: str, room_id: str | None, speaker: str | None
) -> FastResult:
    """"Start a conversation" — escalate this session into conversation mode."""
    if not _CONV_RE.fullmatch(_norm(utterance)):
        return FastResult.miss()

    mode = str(get_request("s2s_mode", "off") or "off")
    if mode == "always":
        # A conversation already opens on every wake — nothing to escalate;
        # let the model answer the phrase naturally.
        return FastResult.miss()
    if mode != "on_demand":
        return FastResult.handled(
            "Conversations aren't turned on right now — you can enable them in settings.",
            _VOICE,
        )
    if room_id and room_id in (get_request("no_aec_rooms", []) or []):
        # Half-duplex node: say why (the 5.0.4 rule) and stay classic.
        return FastResult.handled(
            "This room can't do conversations — it needs a speakerphone "
            "that can hear while I talk.",
            _VOICE,
        )

    # Good to go — and deliberately SILENT here: the server action opens the
    # conversation immediately and speaks the entry cue through the s2s
    # bridge's own delivery path, so entry rides the same hardened machinery
    # as every later turn. (The first design spoke the cue from this reply
    # with expect_response and armed the bridge for the NEXT capture — the
    # held-floor hand-off collided live with the classic cue ladder, and the
    # deferred arm went stale. Option-2 redesign, 2026-09-01.)
    add_action({"type": "start_conversation"})
    return FastResult.handled("", _VOICE)
