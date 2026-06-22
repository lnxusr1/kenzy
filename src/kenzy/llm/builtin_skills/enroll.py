"""
Enroll speaker — start voice enrollment for a named person.

When the user asks Kenzy to remember/enroll their voice, the LLM calls this skill.
The LLM service can't capture audio itself, so it queues a server-side **action**;
the server then prompts the user, captures a few samples, and POSTs them to the
kenzy-speaker service. Enrollment is gated server-side by
``speaker.allow_voice_enroll`` (off by default), so this skill just requests it.
"""

from __future__ import annotations

from kenzy.llm.skills import add_action, skill  # type: ignore[import]


@skill
async def enroll_speaker(name: str) -> str:
    """Enroll (register) a person's voice so Kenzy can recognize them later.

    Use when the user asks to be remembered or enrolled — e.g. "enroll me as
    Alice", "remember my voice as Bob".

    :param name: The name to enroll the speaker under.
    """
    name = name.strip()
    if not name:
        return "I need a name to enroll the voice under — ask the user who this is."
    add_action({"type": "start_enrollment", "name": name})
    return f"Starting voice enrollment for {name}."
