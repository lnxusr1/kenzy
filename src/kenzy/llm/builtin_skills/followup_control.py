"""Spoken control of follow-up mode (v6.0).

"Turn off follow-up mode" sets the server's ``s2s.mode`` to off (on ⇒ always) — live for the
next wake word, persisted across restarts (the ``set_proactive`` pattern: a
feature switched off because it was misbehaving must not come back after an
upgrade or a power cut). The dashboard shows the state either way.

A SKILL, deliberately, not a fast intent (founder ruling 2026-08-28): one
skill serves both pipelines. On the classic path it is an ordinary tool —
and the classic path is the only place ENABLING can happen, since s2s isn't
routing anything while off. Inside a live conversation the skill-host doors
expose the same tool, so "stop doing follow-ups" works mid-conversation.

Disabling is a settings change: gated to a recognized voice, and confirmed
first — the model relays the question and calls again with ``confirm=true``
only on a clear yes (standing decision 8: consequential actions confirm).
"""

from __future__ import annotations

from kenzy.llm.skills import add_action, skill  # type: ignore[import]


@skill(min_tier="recognized")
async def set_followup_mode(enabled: bool, confirm: bool = False) -> str:
    """Turn Kenzy's follow-up conversation mode on or off, house-wide.

    Use when the user asks to enable or disable follow-up mode or
    conversation mode — "turn off follow-up mode", "stop listening for
    follow-ups", "turn conversations back on". This is a persistent,
    house-wide setting. It is NOT for ending the current conversation (use
    end_conversation) and NOT for muting a speaker (use set_speaker_volume).

    enabled: true to turn follow-up mode on, false to turn it off.
    confirm: disabling requires confirmation — first call with confirm=false,
             ask the user the exact question the result gives you, and call
             again with confirm=true only after a clear yes. Enabling needs
             no confirmation.
    """
    if enabled:
        add_action({"type": "set_s2s", "enabled": True})
        return (
            "Follow-up mode is enabled. From the next wake word, rooms with "
            "echo-cancelling speakers hold conversations."
        )
    if not confirm:
        return (
            "CONFIRMATION REQUIRED — ask the user: 'Follow-up mode will stay "
            "off until someone turns it back on. Are you sure?' Only a clear "
            "yes counts; then call this again with confirm=true."
        )
    add_action({"type": "set_s2s", "enabled": False})
    return (
        "Follow-up mode is disabled and stays off across restarts. The "
        "current conversation finishes normally; after that, the wake word "
        "uses the classic one-shot pipeline. It can be re-enabled by voice "
        "or from the dashboard."
    )
