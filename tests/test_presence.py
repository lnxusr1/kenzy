"""Presence read-on-demand (4.2): HA person states through the ha_user link,
name gating on the fast path, and honest unlinked/unconfigured answers."""

from __future__ import annotations

from contextlib import contextmanager

import pytest

from kenzy.llm import skills as sk
from kenzy.llm.builtin_skills import presence

PEOPLE = [
    {"id": "john", "name": "John", "voiceprints": ["john"], "ha_user": "person.john"},
    {"id": "nicki", "name": "Nicki", "voiceprints": ["nicki"], "ha_user": "person.nicki"},
    {"id": "guest", "name": "Guest", "voiceprints": [], "ha_user": None},
    {
        "id": "bobbie",
        "name": "Bobbie",
        "voiceprints": [],
        "aliases": ["JJ"],
        "ha_user": "person.bobbie",
    },  # fmt: skip
]

# Jhon scores an exact 0.925/0.925 tie against these two — the ambiguity cases
# swap them in via _override so the shared fixture stays unambiguous.
TWO_JOHNS = [
    {"id": "john", "name": "John", "ha_user": "person.john"},
    {"id": "jon", "name": "Jon", "ha_user": "person.jon"},
]


@pytest.fixture(autouse=True)
def _ctx(monkeypatch):
    monkeypatch.setenv("HA_API_KEY", "x")
    # Token-reset both contextvars: a SYNC fixture runs in the thread's base
    # context, so an unreset set() would leak into later test files (async
    # test bodies are insulated by task context-copies; fixtures aren't).
    tok_a = sk.begin_actions()
    tok_r = sk._request_ctx.set(
        {
            "channel": "voice",
            "person_id": "john",
            "speaker_tier": "recognized",
            "people": PEOPLE,
        }  # fmt: skip
    )
    yield
    sk._request_ctx.reset(tok_r)
    sk._actions.reset(tok_a)


def _states(monkeypatch, mapping):
    async def state_of(entity_id):
        return mapping.get(entity_id, "")

    monkeypatch.setattr(presence, "_state_of", state_of)


@contextmanager
def _override(**changes):
    ctx = dict(sk._request_ctx.get())
    ctx.update(changes)
    tok = sk._request_ctx.set(ctx)
    try:
        yield
    finally:
        sk._request_ctx.reset(tok)


async def test_is_home_and_zones(monkeypatch):
    _states(monkeypatch, {"person.nicki": "home", "person.john": "Work"})
    r = await presence.fast_presence("is Nicki home?", "office", None)
    assert r.is_handled and r.text == "Nicki is home."
    r = await presence.fast_presence("where is john", "office", None)
    assert r.text == "John is at Work."
    _states(monkeypatch, {"person.nicki": "not_home"})
    r = await presence.fast_presence("Is Nicki home", "office", None)
    assert r.text == "Nicki is away."


async def test_whos_home_summary(monkeypatch):
    _states(monkeypatch, {"person.john": "home", "person.nicki": "not_home"})
    r = await presence.fast_presence("who's home?", "office", None)
    assert r.is_handled and "John is home" in r.text and "Nicki" in r.text
    _states(
        monkeypatch,
        {"person.john": "home", "person.nicki": "home", "person.bobbie": "home"},
    )
    r = await presence.fast_presence("is anyone home", "office", None)
    assert "Everyone's home" in r.text


async def test_unknown_name_misses_to_llm(monkeypatch):
    # "where is my phone" must not be swallowed by the presence intent.
    _states(monkeypatch, {})
    r = await presence.fast_presence("where is my phone", "office", None)
    assert r.status == "miss"


async def test_unlinked_person_honest(monkeypatch):
    _states(monkeypatch, {})
    out = await presence.person_presence("Guest")
    assert "isn't linked" in out


async def test_fuzzy_name_reaches_the_record(monkeypatch):
    # Slice D: the record says Bobbie, STT says Bobby (the …ie/…y drift).
    # The answer self-confirms by using the RECORD's spelling.
    _states(monkeypatch, {"person.bobbie": "home"})
    r = await presence.fast_presence("where is Bobby", "office", None)
    assert r.is_handled and r.text == "Bobbie is home."
    out = await presence.person_presence("Bobby")
    assert out == "Bobbie is home."


async def test_alias_resolves(monkeypatch):
    _states(monkeypatch, {"person.bobbie": "Work"})
    r = await presence.fast_presence("where is JJ", "office", None)
    assert r.is_handled and r.text == "Bobbie is at Work."


async def test_both_johns_make_johnny_a_question_not_a_guess(monkeypatch):
    # A household holding BOTH John and Jon: a drifted rendering scores a tie,
    # and a human would ask too. The deterministic path must MISS
    # (clarification is a conversation), and the skill must ASK, never guess.
    _states(monkeypatch, {"person.john": "home", "person.jon": "not_home"})
    with _override(people=PEOPLE + TWO_JOHNS[1:]):
        r = await presence.fast_presence("where is Jhon", "office", None)
        assert r.status == "miss"

        asked: list[str] = []

        async def fake_ask(question, **kwargs):
            asked.append(question)
            return "Jon"

        monkeypatch.setattr(presence, "ask", fake_ask)
        out = await presence.person_presence("Jhon")
    assert asked and "Did you mean" in asked[0]
    assert out == "Jon is away."


async def test_ambiguity_on_typed_channel_returns_the_question(monkeypatch):
    _states(monkeypatch, {})
    with _override(people=TWO_JOHNS, channel="assist"):
        out = await presence.person_presence("Jhon")
    assert out.startswith("Did you mean")
    assert "John" in out and "Jon" in out


async def test_cancelled_clarification_never_answers(monkeypatch):
    _states(monkeypatch, {"person.john": "home", "person.jon": "home"})

    async def fake_ask(question, **kwargs):
        return None  # wake word / window expiry

    monkeypatch.setattr(presence, "ask", fake_ask)
    with _override(people=TWO_JOHNS):
        out = await presence.person_presence("Jhon")
    assert "home" not in out  # no location may ride a cancelled clarification


# ---------------------------------------------------------------------------
# Slice E — honest person location (the composer is pure; every branch here)
# ---------------------------------------------------------------------------

OCC = {
    "rooms": [
        {
            "room": "office",
            "state": "occupied",
            "person_id": "john",
            "person_name": "John",
            "identity_age": 41.0,
        },  # fmt: skip
        {
            "room": "master_bedroom",
            "state": "occupied",
            "person_id": "john",
            "person_name": "John",
            "identity_age": 300.0,
        },  # older echo of the same voice
        {
            "room": "kitchen",
            "state": "occupied",
            "person_id": "nicki",
            "person_name": "Nicki",
            "identity_age": 10.0,
        },  # someone else
    ],
    "people": [],
    "stale": False,
}


def test_compose_home_with_anchor_carries_room_and_age():
    out = presence._compose_location("John", "home", ("office", 41.0))
    assert out == "John is home — I last heard them in the office just now."


def test_compose_home_without_anchor_is_honest_about_silence():
    out = presence._compose_location("John", "home", None)
    assert out == "John is home, but I haven't heard them recently."


def test_compose_away_and_zone_ignore_the_anchor():
    # The HA level is current by definition; the anchor is minutes old. The
    # person just left — repeating the anchor would deliver stale evidence.
    assert presence._compose_location("John", "not_home", ("office", 41.0)) == "John is away."
    assert presence._compose_location("John", "Work", ("office", 41.0)) == "John is at Work."


def test_compose_unknown_state_rescued_by_anchor():
    out = presence._compose_location("John", "unknown", ("office", 130.0))
    assert out == (
        "Home Assistant can't tell me where John is, "
        "but I last heard them in the office a couple of minutes ago."
    )


def test_compose_unlinked_with_anchor_answers_from_voice_alone():
    out = presence._compose_location("Adam", None, ("office", 41.0))
    assert out == "I last heard Adam in the office just now."


def test_compose_unlinked_without_anchor_keeps_the_linking_note():
    out = presence._compose_location("Guest", None, None)
    assert "isn't linked" in out


def test_age_phrasing_is_bounded_by_the_decay_window():
    assert presence._age_phrase(30) == "just now"
    assert presence._age_phrase(130) == "a couple of minutes ago"
    assert presence._age_phrase(310) == "about 5 minutes ago"


def test_voice_anchor_picks_freshest_and_never_someone_else():
    with _override(occupancy=OCC):
        room, age = presence._voice_anchor("john")
        assert room == "office" and age == 41.0  # not the older bedroom echo
        assert presence._voice_anchor("guest") is None
        assert presence._voice_anchor("") is None
    # No snapshot injected (occupancy off / direct POST) → honestly nothing.
    assert presence._voice_anchor("john") is None


async def test_fast_path_speaks_the_composed_answer(monkeypatch):
    _states(monkeypatch, {"person.john": "home"})
    with _override(occupancy=OCC):
        r = await presence.fast_presence("where is John", "office", None)
    assert r.is_handled
    assert r.text == "John is home — I last heard them in the office just now."


async def test_unconfigured_misses(monkeypatch):
    monkeypatch.delenv("HA_API_KEY", raising=False)
    r = await presence.fast_presence("is Nicki home?", "office", None)
    assert r.status == "miss"
    out = await presence.person_presence("Nicki")
    assert "Home Assistant" in out


def test_occupancy_rides_process_request_into_the_skills_context(monkeypatch):
    """The wire that was MISSING until 5.0.3: the server injected `occupancy`
    into ProcessRequest since 5.0.0, but llm.py's begin_request dict never
    passed it through — so get_request("occupancy") was always empty while the
    server-side snapshot was full. Found live (the composed answer silently
    degraded to the plain one); this pins the whole plumbing run."""
    from fastapi.testclient import TestClient

    from kenzy.llm import llm as llm_app

    seen: dict = {}

    async def probe(utterance, room_id, speaker):
        seen["occupancy"] = sk.get_request("occupancy")
        return presence.FastResult.handled("pinned")

    monkeypatch.setattr(
        llm_app.skill_registry,
        "dispatch_fast",
        lambda text, room, spk: probe(text, room, spk),
    )
    client = TestClient(llm_app.app)
    r = client.post(
        "/process",
        json={
            "text": "ping",
            "room_id": "office",
            "session_id": "s1",
            "occupancy": {"rooms": [{"room": "office", "state": "occupied"}]},
        },
    )
    assert r.status_code == 200
    assert seen["occupancy"] == {"rooms": [{"room": "office", "state": "occupied"}]}


# ---------------------------------------------------------------------------
# Slice F — room queries and the household composite
# ---------------------------------------------------------------------------


def test_room_occupied_by_sensor_is_anonymous():
    entry = {"room": "loft", "state": "occupied", "held": ["binary_sensor.x"], "age": 4.0}
    out = presence._compose_room("loft", entry, True)
    assert out == "Looks like it — the presence sensor shows someone in the loft right now."
    assert "binary_sensor" not in out  # entity ids never reach speech


def test_room_occupied_with_anchor_names_the_person():
    # Person-level evidence exists, so attribution is legitimate — the one case
    # where "who's in the loft" gets a name.
    entry = {"room": "loft", "state": "occupied", "held": [], "age": 50.0,
             "person_name": "Nicki", "identity_age": 50.0}  # fmt: skip
    assert presence._compose_room("loft", entry, True) == "Nicki was in there just now."


def test_room_recent_activity_carries_age():
    entry = {"room": "loft", "state": "occupied", "held": [], "age": 240.0}
    out = presence._compose_room("loft", entry, True)
    assert out == "Probably — there was activity in the loft about 4 minutes ago."


def test_room_maybe_is_hedged():
    entry = {"room": "loft", "state": "maybe", "held": [], "age": 900.0}
    out = presence._compose_room("loft", entry, True)
    assert out == (
        "There was some activity about 15 minutes ago, but I'm not sure anyone's still there."
    )


def test_room_never_says_empty():
    # The tracker has no "empty" state on purpose (pets exist; sensors go
    # blind). The strongest no is "no sign for a while" — with the caveat.
    entry = {"room": "loft", "state": "unknown", "held": [], "age": 7200.0}
    out = presence._compose_room("loft", entry, True)
    assert (
        out == "I haven't noticed anyone in the loft for a while — but I can't be sure it's empty."
    )
    assert "empty" not in out.replace("can't be sure it's empty", "")


def test_room_without_readings_and_stale_and_off():
    assert presence._compose_room("garage", None, True) == (
        "I don't have a picture of the garage."
    )
    entry = {"room": "loft", "state": "unknown", "held": [], "age": None}
    assert presence._compose_room("loft", entry, True) == (
        "I haven't seen any sign of anyone in the loft yet."
    )
    stale = {"room": "loft", "state": "occupied", "held": ["x"], "age": 3.0, "stale": True}
    assert "out of date" in presence._compose_room("loft", stale, True)
    assert presence._compose_room("loft", None, False) == (
        "I don't keep track of room presence here."
    )


async def test_room_fast_path_known_answers_unknown_misses(monkeypatch):
    _states(monkeypatch, {})
    with _override(occupancy=OCC, rooms=["Office"]):
        r = await presence.fast_presence("is anyone in the kitchen?", "office", None)
        assert r.is_handled and "Nicki was in there" in r.text
        r = await presence.fast_presence("who's in the office?", "office", None)
        assert r.is_handled
        r = await presence.fast_presence("is anyone in the car?", "office", None)
        assert r.status == "miss"
    # Occupancy off → the LLM tier owns it, not a canned answer.
    r = await presence.fast_presence("is anyone in the kitchen?", "office", None)
    assert r.status == "miss"


def test_household_unknown_is_never_away():
    entries = [
        {"name": "John", "state": "home", "anchor": None},
        {"name": "Guest", "state": "unknown", "anchor": None},
    ]
    out = presence._compose_household(entries, True)
    assert out == "John is home. I'm not sure about Guest."
    # And the "nobody home" shortcut is unreachable while anyone is unsure.
    entries = [
        {"name": "John", "state": "not_home", "anchor": None},
        {"name": "Guest", "state": "unknown", "anchor": None},
    ]
    out = presence._compose_household(entries, True)
    assert "Nobody" not in out
    assert out == "John is away. I'm not sure about Guest."


def test_household_anchors_lead_and_everyone_home_survives():
    entries = [
        {"name": "John", "state": "home", "anchor": ("office", 41.0)},
        {"name": "Nicki", "state": "home", "anchor": None},
    ]
    out = presence._compose_household(entries, True)
    assert out == "John was in the office just now. Nicki is home."
    plain = [
        {"name": "John", "state": "home", "anchor": None},
        {"name": "Nicki", "state": "home", "anchor": None},
    ]
    assert presence._compose_household(plain, True) == "Everyone's home: John, Nicki."


async def test_household_unlinked_but_heard_appears(monkeypatch):
    adam_occ = {"rooms": [{"room": "office", "state": "occupied", "person_id": "adam",
                           "person_name": "Adam", "identity_age": 30.0}]}  # fmt: skip
    people = PEOPLE + [{"id": "adam", "name": "Adam", "ha_user": None}]
    _states(monkeypatch, {"person.john": "home", "person.nicki": "home", "person.bobbie": "home"})
    with _override(people=people, occupancy=adam_occ):
        out = await presence._whos_home()
    assert "Adam was in the office just now." in out
    assert "John, Nicki and Bobbie are home." in out
