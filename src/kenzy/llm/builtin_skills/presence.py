"""Presence read-on-demand (4.2 treat): "is Mom home?" answered from Home
Assistant's person entities — one live state read, zero new configuration.
The ``ha_user`` link on a person record (People page) is the whole setup.

Deliberately read-on-demand, not ambient: Kenzy fetches presence only when
someone asks (the v5 "she notices" era owns the ambient half). Answers are
gated to recognized voices — who's home is household information.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

from kenzy.llm.skills import FastResult, fast_intent, get_request, is_disabled, skill

log = logging.getLogger(__name__)

_VOICE = "Speak briefly and warmly."


def _configured() -> bool:
    return bool(os.environ.get("HA_API_KEY")) and not is_disabled("presence")


def _linked_people() -> list[dict[str, Any]]:
    return [p for p in (get_request("people") or []) if p.get("ha_user")]


def _find_person(name: str) -> dict[str, Any] | None:
    low = name.strip().lower()
    for p in get_request("people") or []:
        if str(p.get("name", "")).lower() == low or str(p.get("id", "")).lower() == low:
            return dict(p)
    return None


def _spoken_state(name: str, state: str) -> str:
    """HA person states: home / not_home / a zone name."""
    if state == "home":
        return f"{name} is home."
    if state in ("not_home", "away"):
        return f"{name} is away."
    if state in ("unknown", "unavailable", ""):
        return f"I can't tell where {name} is right now."
    return f"{name} is at {state.replace('_', ' ')}."


async def _state_of(entity_id: str) -> str:
    from kenzy.llm.builtin_skills.home_assistant import _ha_state

    try:
        return str((await _ha_state(entity_id)).get("state", ""))
    except Exception as exc:
        log.warning("presence: state read for %s failed: %s", entity_id, exc)
        return ""


async def _presence_of(name: str) -> str:
    person = _find_person(name)
    if person is None:
        return f"I don't know anyone called {name}."
    ha_user = str(person.get("ha_user") or "")
    if not ha_user:
        return (
            f"I can't see {person.get('name', name)}'s presence — their person record "
            "isn't linked to a Home Assistant person (People page → HA person)."
        )
    state = await _state_of(ha_user)
    return _spoken_state(str(person.get("name", name)), state)


async def _whos_home() -> str:
    linked = _linked_people()
    if not linked:
        return (
            "No one's presence is linked yet — connect people to their Home Assistant "
            "person on the People page."
        )
    home: list[str] = []
    away: list[str] = []
    for p in linked:
        state = await _state_of(str(p["ha_user"]))
        (home if state == "home" else away).append(str(p.get("name") or p["id"]))
    if not home:
        return "Nobody seems to be home right now."
    if not away:
        return f"Everyone's home: {', '.join(home)}."
    return f"{', '.join(home)} {'is' if len(home) == 1 else 'are'} home; {', '.join(away)} not."


@skill(min_tier="recognized")
async def person_presence(name: str = "") -> str:
    """Where a household member is right now (home, away, or a zone), read
    live from Home Assistant's presence tracking. Empty name = summarize who
    is home. Use for questions like "is Mom home?", "where is Dad?",
    "who's home right now?".

    :param name: The person's name, or empty for the whole-house summary.
    """
    if not _configured():
        return "Presence needs the Home Assistant connection (HA_API_KEY)."
    return await (_presence_of(name) if name.strip() else _whos_home())


_IS_HOME_RE = re.compile(r"^(?:is|are)\s+(?P<name>[\w .'-]+?)\s+(?:at\s+)?home\??$", re.I)
_WHERE_RE = re.compile(r"^where(?:'s| is| are)\s+(?P<name>[\w .'-]+?)\??$", re.I)
_WHOS_HOME_RE = re.compile(
    r"^(?:who(?:'s| is| all is)\s+(?:at\s+)?home|is\s+(?:anyone|anybody)\s+(?:at\s+)?home)\??$",
    re.I,
)


@fast_intent(priority=88, min_tier="recognized")
async def fast_presence(utterance: str, room_id: str | None, speaker: str | None) -> FastResult:
    if not _configured():
        return FastResult.miss()
    text = utterance.strip().rstrip(".!")
    m = _WHOS_HOME_RE.match(text)
    if m:
        return FastResult.handled(await _whos_home(), _VOICE)
    m = _IS_HOME_RE.match(text) or _WHERE_RE.match(text)
    if m:
        # Only claim the utterance when the name is actually a known person —
        # "where is my phone" belongs to the LLM, not a presence miss-message.
        if _find_person(m.group("name")) is None:
            return FastResult.miss()
        return FastResult.handled(await _presence_of(m.group("name")), _VOICE)
    return FastResult.miss()
