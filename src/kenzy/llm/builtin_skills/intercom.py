"""
Intercom — start a live two-way voice call to another room.

When the user asks to call/connect to a room, the LLM calls this skill. The LLM
service can't open the audio bridge itself, so the skill queues a server-side
**action**; the server rings the target room and (only after the people there
verbally accept) connects the two rooms. The skill returns a short confirmation
so the model can say something like "Calling the living room…".
"""

from __future__ import annotations

from kenzy.llm.skills import add_action, skill  # type: ignore[import]


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
    add_action({"type": "start_intercom", "room": room})
    return f"Calling {room}."
