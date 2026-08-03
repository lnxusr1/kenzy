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

from kenzy.llm.names import Resolution, resolve_person
from kenzy.llm.skills import (
    FastResult,
    ask,
    fast_intent,
    get_request,
    is_disabled,
    request_channel,
    skill,
)

log = logging.getLogger(__name__)

_VOICE = "Speak briefly and warmly."


def _configured() -> bool:
    return bool(os.environ.get("HA_API_KEY")) and not is_disabled("presence")


def _linked_people() -> list[dict[str, Any]]:
    return [p for p in (get_request("people") or []) if p.get("ha_user")]


def _resolve(name: str) -> Resolution:
    """Spoken name → person record(s), fuzzy (5.0.3 slice D).

    The transcriber spells names its own way per speaker ("Bobbie" arrives as
    "Bobby"), so an exact match here used to kill the query outright. The
    resolver handles spelling drift and explicit aliases; the SUBJECT of the
    query is all it ever selects — the asker stays voiceprint-resolved.
    """
    return resolve_person(name, get_request("people") or [])


def _find_person(name: str) -> dict[str, Any] | None:
    return _resolve(name).person


def _spoken_state(name: str, state: str) -> str:
    """HA person states: home / not_home / a zone name."""
    if state == "home":
        return f"{name} is home."
    if state in ("not_home", "away"):
        return f"{name} is away."
    if state in ("unknown", "unavailable", ""):
        return f"I can't tell where {name} is right now."
    return f"{name} is at {state.replace('_', ' ')}."


def _age_phrase(seconds: float) -> str:
    """A spoken age for a voice anchor. The tracker's decay bounds the range:
    an anchor older than ~6–7 minutes has already fallen out of the snapshot,
    so there is no long-tail phrasing to design — if we have one, it's fresh."""
    if seconds < 90:
        return "just now"
    if seconds < 180:
        return "a couple of minutes ago"
    return f"about {max(2, round(seconds / 60))} minutes ago"


def _voice_anchor(person_id: str) -> tuple[str, float] | None:
    """The freshest room this person was HEARD in, from the injected occupancy
    snapshot (5.0.3 slice E — the first reader of what 5.0.0 wired).

    Person-level evidence only: the anchor exists because *their recognized
    voice* spoke there. Anonymous room occupancy never reaches this function —
    a person-level claim may only rest on person-level evidence.
    """
    if not person_id:
        return None
    snap = get_request("occupancy") or {}
    best: tuple[str, float] | None = None
    for room in snap.get("rooms") or []:
        if str(room.get("person_id") or "") != person_id:
            continue
        age = float(room.get("identity_age") or 0.0)
        if best is None or age < best[1]:
            best = (str(room.get("room") or "").replace("_", " "), age)
    return best if best and best[0] else None


def _compose_location(
    name: str,
    state: str | None,
    anchor: tuple[str, float] | None,
    listening: bool = True,
) -> str:
    """(HA person state, freshest voice anchor) → the one spoken sentence.

    Pure — every honesty decision in slice E lives here, unit-tested branch by
    branch. ``state=None`` means the record has no HA link at all. The rules:

    * The age is never optional — "I last heard her in the office" without the
      "when" is a lie by omission; the anchor's honesty rests on its freshness
      riding along.
    * Away and zone answers deliberately IGNORE the anchor: the HA level is
      current by definition while the anchor is minutes old, so "away" beats
      "I heard them recently" — the person just left, and saying otherwise
      would be delivering stale evidence as fresh.
    * This returns the finished sentence, never data: handing the model raw
      fields invites it to smooth "I don't know" into confidence.
    * ``listening=False`` means occupancy isn't running at all — then "I
      haven't heard them recently" would claim a vigilance that doesn't exist,
      so the answer stays the plain pre-5.0.3 sentence. (Found by the old
      tests failing: absence of the snapshot is not absence of an anchor.)
    """
    if anchor is not None:
        room, age = anchor
        when = _age_phrase(age)
        if state == "home":
            return f"{name} is home — I last heard them in the {room} {when}."
        if state in ("unknown", "unavailable", ""):
            return f"Home Assistant can't tell me where {name} is, but I last heard them in the {room} {when}."
        if state is None:
            return f"I last heard {name} in the {room} {when}."
    if state is None:
        # No HA link. Spoken text — no parentheticals or arrows; the People-page
        # detail lives in the docs, not in her mouth.
        return (
            f"I can't see {name}'s presence — their person record "
            "isn't linked to a Home Assistant person yet."
        )
    if state == "home" and listening:
        return f"{name} is home, but I haven't heard them recently."
    return _spoken_state(name, state)


async def _state_of(entity_id: str) -> str:
    from kenzy.llm.builtin_skills.home_assistant import _ha_state

    try:
        return str((await _ha_state(entity_id)).get("state", ""))
    except Exception as exc:
        log.warning("presence: state read for %s failed: %s", entity_id, exc)
        return ""


async def _located(person: dict[str, Any]) -> str:
    """The answer for one resolved person — always their RECORD name, so a
    fuzzy resolution self-confirms ("where is Bobby" → "Bobbie is home.").

    Slice E: composes the HA person state with the voice anchor from the
    occupancy snapshot, so "home" gains a room and a freshness — and a person
    with no HA link at all can still be answered from their voice alone.
    """
    name = str(person.get("name") or person.get("id") or "")
    anchor = _voice_anchor(str(person.get("id") or ""))
    # An empty payload means occupancy is disabled — distinct from "running
    # but nobody heard": only the latter may say "I haven't heard them".
    listening = bool((get_request("occupancy") or {}).get("rooms"))
    ha_user = str(person.get("ha_user") or "")
    state = await _state_of(ha_user) if ha_user else None
    return _compose_location(name, state, anchor, listening=listening)


def _clarify_text(candidates: tuple[dict[str, Any], ...]) -> str:
    names = [str(c.get("name") or c["id"]) for c in candidates[:3]]
    return f"Did you mean {' or '.join(names)}?"


async def _presence_of(name: str) -> str:
    res = _resolve(name)
    if res.is_ambiguous:
        # Ambiguity asks, never guesses (slice D trap): a near-tie between two
        # household names must not be settled by iteration order. On voice we
        # hold the floor and ask; a typed channel gets the question as the
        # reply and the answer arrives as the next turn.
        question = _clarify_text(res.candidates)
        if request_channel() != "voice":
            return question
        answer = await ask(question)
        if answer is None or not answer.strip():
            return "Never mind."  # cancelled/expired — the server discards this
        res = _resolve(answer)
        if res.person is None or res.is_ambiguous:
            return "I still couldn't tell who you meant — sorry."
    if res.person is None:
        return f"I don't know anyone called {name}."
    return await _located(res.person)


def _ago_phrase(seconds: float) -> str:
    """A spoken age for occupancy evidence — unlike voice anchors, this can be
    hours old (a held level releases, then the residue decays slowly)."""
    if seconds < 90:
        return "just now"
    if seconds < 3600:
        return f"about {max(2, round(seconds / 60))} minutes ago"
    hours = max(1, round(seconds / 3600))
    return f"about {hours} hour{'s' if hours != 1 else ''} ago"


def _room_slug(text: str) -> str:
    """Byte-compatible with ``server/occupancy.py::room_slug`` (wire contract,
    no server import — the tier-constant precedent): the snapshot keys rooms
    by this slug, and the join has to happen on the reader's side too."""
    cleaned = re.sub(r"[^\w\s]", "", text or "").strip().lower()
    return re.sub(r"\s+", "_", cleaned)


def _room_entry(room: str) -> dict[str, Any] | None:
    slug = _room_slug(room)
    for entry in (get_request("occupancy") or {}).get("rooms") or []:
        if str(entry.get("room") or "") == slug:
            return dict(entry)
    return None


def _compose_room(room: str, entry: dict[str, Any] | None, listening: bool) -> str:
    """"Is anyone in the loft?" → one honest sentence. Pure.

    The tracker never claims *empty* — occupied / maybe / unknown is the whole
    vocabulary (Slice B: absence of evidence is not evidence of absence, and
    pets exist). So the strongest "no" this function may utter is "no sign of
    anyone for a while" — never "the room is empty". A person is NAMED only
    when the identity anchor carries them (person-level evidence); everything
    else stays anonymous ("someone").
    """
    spoken = _room_slug(room).replace("_", " ")
    if not listening:
        return "I don't keep track of room presence here."
    if entry is None:
        # Not a room the tracker knows at all ("the car") — say that, not
        # something that implies it's a tracked place with no readings.
        return f"I don't have a picture of the {spoken}."
    if entry.get("state") == "unknown" and entry.get("age") is None:
        # A known room with NO evidence yet. Not "I don't get readings" — the
        # room may be fully sensored and simply quiet since startup (the seed
        # deliberately doesn't invent belief for idle sensors, Build finding 1).
        return f"I haven't seen any sign of anyone in the {spoken} yet."
    if entry.get("stale"):
        return "My sensor picture is out of date right now, so I can't say for sure."
    state = str(entry.get("state") or "unknown")
    age = entry.get("age")
    when = _ago_phrase(float(age)) if age is not None else "recently"
    if state == "occupied":
        who = str(entry.get("person_name") or "")
        if who and entry.get("identity_age") is not None:
            return f"{who} was in there {_age_phrase(float(entry['identity_age']))}."
        if entry.get("held"):
            return f"Looks like it — the presence sensor shows someone in the {spoken} right now."
        return f"Probably — there was activity in the {spoken} {when}."
    if state == "maybe":
        return f"There was some activity {when}, but I'm not sure anyone's still there."
    return f"I haven't noticed anyone in the {spoken} for a while — but I can't be sure it's empty."


def _compose_household(entries: list[dict[str, Any]], listening: bool) -> str:
    """"Where is everyone" → sentences, never a fabricated "nobody". Pure.

    The load-bearing branch is *unsure*: a person entity reading unknown or
    unreachable is said out loud as "I'm not sure about X" — it is never
    counted as away and never silently dropped. "Nobody seems to be home" may
    only be uttered when every single person actually read away (Slice B's
    absence-is-not-a-value rule, finally as language).
    """
    anchored: list[str] = []
    home: list[str] = []
    zones: list[str] = []
    away: list[str] = []
    unsure: list[str] = []
    for e in entries:
        name = str(e.get("name") or "")
        state = e.get("state")
        anchor = e.get("anchor")
        if anchor and (state == "home" or state in ("unknown", "unavailable", "", None)):
            room, age = anchor
            anchored.append(f"{name} was in the {room} {_age_phrase(age)}")
        elif state == "home":
            home.append(name)
        elif state in ("not_home", "away"):
            away.append(name)
        elif state in ("unknown", "unavailable", "", None):
            unsure.append(name)
        else:
            zones.append(f"{name} is at {str(state).replace('_', ' ')}")

    def _join(names: list[str]) -> str:
        return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"

    if not anchored and not unsure and not zones:
        # The two legacy shortcuts survive, but only when they're fully true.
        if home and not away:
            return f"Everyone's home: {', '.join(home)}."
        if away and not home:
            return "Nobody seems to be home right now."
    parts: list[str] = []
    parts.extend(f"{a}." for a in anchored)
    if home:
        parts.append(f"{_join(home)} {'is' if len(home) == 1 else 'are'} home.")
    parts.extend(f"{z}." for z in zones)
    if away:
        parts.append(f"{_join(away)} {'is' if len(away) == 1 else 'are'} away.")
    if unsure:
        parts.append(f"I'm not sure about {_join(unsure)}.")
    return " ".join(parts) if parts else "I don't have anyone's presence to report."


async def _whos_home() -> str:
    people = get_request("people") or []
    linked = _linked_people()
    listening = bool((get_request("occupancy") or {}).get("rooms"))
    entries: list[dict[str, Any]] = []
    covered: set[str] = set()
    for p in linked:
        pid = str(p.get("id") or "")
        covered.add(pid)
        entries.append(
            {
                "name": str(p.get("name") or p["id"]),
                "state": await _state_of(str(p["ha_user"])),
                "anchor": _voice_anchor(pid),
            }
        )
    # People with no HA link but a live voice anchor (heard, just not tracked):
    # they exist in the answer too — being unlinked must not make them invisible.
    for p in people:
        pid = str(p.get("id") or "")
        if pid in covered or p.get("ha_user"):
            continue
        anchor = _voice_anchor(pid)
        if anchor:
            entries.append({"name": str(p.get("name") or pid), "state": None, "anchor": anchor})
    if not entries:
        return (
            "No one's presence is linked yet — connect people to their Home Assistant "
            "person on the People page."
        )
    return _compose_household(entries, listening)


@skill(min_tier="recognized")
async def person_presence(name: str = "") -> str:
    """Where a household member is right now (home, away, or a zone), read
    live from Home Assistant's presence tracking. Empty name = summarize who
    is home. Use for questions like "is Mom home?", "where is Dad?",
    "who's home right now?".

    :param name: The person's name, or empty for the whole-house summary.
    """
    if not _configured():
        return "Presence needs the Home Assistant connection set up first."
    return await (_presence_of(name) if name.strip() else _whos_home())


@skill(min_tier="recognized")
async def room_presence(room: str) -> str:
    """Whether anyone seems to be in a specific room right now, from the home's
    presence sensors and recently heard voices. Use for questions like "is
    anyone in the loft?", "who's in the office?", "is the kitchen occupied?".

    :param room: The room's name, e.g. "loft" or "master bedroom".
    """
    room = room.strip()
    if not room:
        return "Which room do you mean?"
    # No HA_API_KEY gate here: the honest no-occupancy answer ("I don't keep
    # track of room presence") is better than a setup lecture, and the
    # tier gate already keeps this household information from unknown voices.
    listening = bool((get_request("occupancy") or {}).get("rooms"))
    return _compose_room(room, _room_entry(room), listening)


_IS_HOME_RE = re.compile(r"^(?:is|are)\s+(?P<name>[\w .'-]+?)\s+(?:at\s+)?home\??$", re.I)
_WHERE_RE = re.compile(r"^where(?:'s| is| are)\s+(?P<name>[\w .'-]+?)\??$", re.I)
_WHOS_HOME_RE = re.compile(
    r"^(?:who(?:'s| is| all is)\s+(?:at\s+)?home|is\s+(?:anyone|anybody)\s+(?:at\s+)?home)\??$",
    re.I,
)
# Slice F: anonymous room queries. Checked AFTER _WHOS_HOME_RE, so "is anyone
# home" keeps its meaning and "is anyone in the loft" gets a room answer.
_ROOM_ANY_RE = re.compile(
    r"^(?:is|are)\s+(?:there\s+)?(?:any(?:one|body)|someone)\s+in\s+the\s+(?P<room>[\w .'-]+?)\??$",
    re.I,
)
_WHOS_IN_RE = re.compile(r"^who(?:'s| is)\s+in\s+the\s+(?P<room>[\w .'-]+?)\??$", re.I)


@fast_intent(priority=88, min_tier="recognized")
async def fast_presence(utterance: str, room_id: str | None, speaker: str | None) -> FastResult:
    if not _configured():
        return FastResult.miss()
    text = utterance.strip().rstrip(".!")
    m = _WHOS_HOME_RE.match(text)
    if m:
        return FastResult.handled(await _whos_home(), _VOICE)
    m = _ROOM_ANY_RE.match(text) or _WHOS_IN_RE.match(text)
    if m:
        # Deterministic only for rooms we actually know (snapshot or server
        # room list) with occupancy running — "is anyone in the car" and every
        # not-listening install belong to the LLM tier, not a canned miss.
        room = m.group("room")
        snap = get_request("occupancy") or {}
        known = {str(r.get("room") or "") for r in snap.get("rooms") or []}
        known |= {_room_slug(str(r)) for r in get_request("rooms") or []}
        if not snap.get("rooms") or _room_slug(room) not in known:
            return FastResult.miss()
        return FastResult.handled(_compose_room(room, _room_entry(room), True), _VOICE)
    m = _IS_HOME_RE.match(text) or _WHERE_RE.match(text)
    if m:
        # Only claim the utterance when the name resolves to a known person —
        # "where is my phone" belongs to the LLM, not a presence miss-message.
        # Resolution is fuzzy (slice D), so "where is Bobby" reaches the
        # Bobbie record; an AMBIGUOUS name also misses — clarification is a
        # conversation, and the skill tier owns those (ask() must never park
        # the deterministic path).
        res = _resolve(m.group("name"))
        if res.person is None:
            return FastResult.miss()
        return FastResult.handled(await _located(res.person), _VOICE)
    return FastResult.miss()
