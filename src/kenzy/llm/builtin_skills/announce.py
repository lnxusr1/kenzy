"""
Announce — broadcast a spoken message to other rooms.

When the user asks Kenzy to tell/announce something to everyone (or to specific
rooms), the LLM calls this skill. The LLM service can't speak in other rooms
itself, so the skill queues a server-side **action**; the server actuates it by
synthesizing the message once and streaming it to every (or the named) connected
node. The skill returns a short confirmation so the model can phrase its reply.
"""

from __future__ import annotations

from kenzy.llm.skills import add_action, skill  # type: ignore[import]


@skill
async def announce(message: str, rooms: str = "") -> str:
    """Broadcast a spoken announcement to other rooms in the home.

    Use this when the user wants something said aloud elsewhere — e.g. "tell
    everyone dinner's ready" or "let the office know I'm leaving".

    :param message: Exactly what should be spoken aloud in the other rooms.
    :param rooms: Comma-separated room names to target, or empty for every room.
    """
    message = message.strip()
    if not message:
        return "No announcement text was provided."
    targets = [r.strip() for r in rooms.split(",") if r.strip()]
    add_action({"type": "announce", "text": message, "rooms": targets or None})
    where = ", ".join(targets) if targets else "every room"
    return f"Announcement queued for {where}."
