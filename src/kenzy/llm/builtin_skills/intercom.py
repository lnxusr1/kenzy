"""
Intercom — start a live two-way voice call to another room.

When the user asks to call/connect to a room, the LLM calls this skill. The LLM
service can't open the audio bridge itself, so the skill queues a server-side
**action**; the server rings the target room and (only after the people there
verbally accept) connects the two rooms. The skill returns a short confirmation
so the model can say something like "Calling the living room…".
"""

from __future__ import annotations

from kenzy.llm.skills import add_action, get_request, request_channel, skill  # type: ignore[import]


def _no_aec(room: str) -> bool:
    return room.strip().lower() in {
        str(r).strip().lower() for r in (get_request("no_aec_rooms") or [])
    }


@skill
async def connect_room(room: str) -> str:
    """Start a live two-way voice call (intercom) to another room in the home.

    Use this when the user wants to talk to someone in another room — e.g. "call
    the living room" or "connect me to the office". The other room must accept the
    call before any audio is connected.

    :param room: The name of the room to call.
    """
    room = room.strip()
    if not room:
        return "No room was given to call."
    if request_channel() != "voice":  # F3: a call needs a node at BOTH ends
        return "Intercom calls connect two room speakers — I can't place one from here."
    # Two-way live audio needs echo cancellation at BOTH ends — refuse in the
    # reply itself rather than confirm a call the server would have to reject.
    here = str(get_request("room_id") or "")
    for end, label in ((room, f"the {room}"), (here, "this room")):
        if end and _no_aec(end):
            return f"Live calls need an echo-cancelling speaker, and {label} doesn't have one."
    add_action({"type": "start_intercom", "room": room})
    return f"Calling {room}."
