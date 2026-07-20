"""Intercom — a live two-way call, with consent as a cross-room ask() (4.2).

The consent conversation belongs to the skill now: ``ask(room=…)`` speaks the
question in the TARGET room (the asker hears "Calling the kitchen." plus
ringback meanwhile) and resumes with whatever the people there said — the
answerer's own words, judged here. Only after a clear spoken yes does the
skill queue the ``connect_call`` action; the audio bridge itself (peer
relay, chimes, hangup-on-wake) stays server-owned, exactly as before.

Outcome semantics (mirrors the old server flow): a clear yes connects; any
other answer is a decline ("The kitchen declined."); no answer — silence,
the target's wake word, or the room going away — comes back as an EMPTY
reply ("No answer from the kitchen."). Default-deny throughout: nothing
bridges without the explicit yes.
"""

from __future__ import annotations

import re

from kenzy.llm.skills import (  # type: ignore[import]
    add_action,
    ask,
    get_request,
    request_channel,
    skill,
)

#: Deliberately mirrors the server's _AFFIRM_WORDS (wire contract — no server
#: import): a clear yes and nothing else.
_AFFIRM_WORDS = frozenset(
    {"yes", "yeah", "yep", "yup", "sure", "ok", "okay", "accept", "accepted", "affirmative"}
)


def _is_affirmative(text: str) -> bool:
    norm = re.sub(r"[^\w\s]", "", text or "").strip().lower()
    if not norm:
        return False
    return bool(set(norm.split()) & _AFFIRM_WORDS) or "go ahead" in norm


def _no_aec(room: str) -> bool:
    return room.strip().lower() in {
        str(r).strip().lower() for r in (get_request("no_aec_rooms") or [])
    }


@skill
async def connect_room(room: str) -> str:
    """Start a live two-way voice call (intercom) to another room in the home.

    Use this when the user wants to talk to someone in another room — e.g. "call
    the living room" or "connect me to the office". This skill asks the other
    room for consent itself and reports the outcome (connected, declined, or no
    answer) — relay its return value to the user.

    :param room: The name of the room to call.
    """
    room = room.strip()
    if not room:
        return "No room was given to call."
    if request_channel() != "voice":  # F3: a call needs a node at BOTH ends
        return "Intercom calls connect two room speakers — I can't place one from here."
    here = str(get_request("room_id") or "")
    if room.lower() == here.lower():
        return "You're already in that room."
    rooms = {str(r).strip().lower() for r in (get_request("rooms") or [])}
    if rooms and room.lower() not in rooms:
        return f"I couldn't reach the {room}."
    # Two-way live audio needs echo cancellation at BOTH ends — refuse in the
    # reply itself rather than confirm a call the server would have to reject.
    for end, label in ((room, f"the {room}"), (here, "this room")):
        if end and _no_aec(end):
            return f"Live calls need an echo-cancelling speaker, and {label} doesn't have one."

    answer = await ask(
        f"The {here or 'other room'} would like to start a voice chat. "
        "Say yes to accept, or no to decline.",
        room=room,
        announce=f"Calling the {room}.",
    )
    if answer is None:
        return "Never mind."  # asker-side cancel — this text is discarded
    if _is_affirmative(answer):
        add_action({"type": "connect_call", "room": room})
        return "Connecting you now."
    if not answer.strip():
        return f"No answer from the {room}."
    return f"The {room} declined."
