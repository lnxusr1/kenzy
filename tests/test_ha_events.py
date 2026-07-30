"""v5 occupancy spine — the HA event client (Slice A).

A knows about entities and rooms; only the tracker believes anything about the
world. These tests hold that boundary, plus the two things the design says the
real work lives in: the connection lifecycle (seed, staleness, refusing to
subscribe without a map) and the edge filter (map membership, self-echo).

The socket is exercised against a scripted fake HA — the ``_RecordingWS``
pattern inverted: instead of capturing what we send, it replays what HA would.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from kenzy.llm.builtin_skills.ha_model import (
    _OCCUPANCY_TEMPLATE,
    build_occupancy_map,
    classify_occupancy,
)
from kenzy.server.ha_events import (
    _STALE_AFTER_S,
    Evidence,
    HaEventClient,
    HaEventStats,
    IntegrationDisabled,
    normalize,
)

MAP: dict[str, dict[str, Any]] = {
    "binary_sensor.office_motion": {
        "room": "office", "room_name": "Office", "kind": "pulse", "scope": "room"
    },
    "binary_sensor.loft_presence": {
        "room": "loft", "room_name": "Loft", "kind": "level", "scope": "room"
    },
    "person.alex": {"room": "", "room_name": "", "kind": "level", "scope": "house", "name": "Alex"},
}


# ---------------------------------------------------------------------------
# Normalization — pure
# ---------------------------------------------------------------------------


def test_room_scope_reads_on_off():
    entry = MAP["binary_sensor.office_motion"]
    ev = normalize("binary_sensor.office_motion", "on", entry, ts=5.0)
    assert ev == Evidence("binary_sensor.office_motion", "office", "pulse", "room", True, 5.0)
    off = normalize("binary_sensor.office_motion", "off", entry, ts=5.0)
    assert off is not None and off.present is False


def test_house_scope_reads_home_not_on():
    """person.* uses home/not_home — reading it as on/off would make everyone away."""
    ev = normalize("person.alex", "home", MAP["person.alex"], ts=1.0)
    assert ev is not None and ev.present is True and ev.scope == "house"
    assert ev.name == "Alex"
    away = normalize("person.alex", "not_home", MAP["person.alex"], ts=1.0)
    assert away is not None and away.present is False


@pytest.mark.parametrize("state", ["unavailable", "unknown", ""])
def test_a_dropped_pulse_is_simply_ignored(state):
    """A PIR going unavailable tells us nothing about the room, and its belief
    already decays on its own — so there is nothing to say."""
    entry = MAP["binary_sensor.office_motion"]
    assert normalize("binary_sensor.office_motion", state, entry) is None


@pytest.mark.parametrize("state", ["unavailable", "unknown", ""])
def test_a_dropped_level_says_so_instead_of_vanishing(state):
    """Still not evidence of absence — but a LEVEL may be mid-assertion, and this
    is the ONLY signal that it stopped talking (a flat battery reads
    `unavailable`; a deleted entity arrives as a null new_state, i.e. ""). Return
    None here and the tracker holds the room at confidence 1.0 forever."""
    entry = MAP["binary_sensor.loft_presence"]
    ev = normalize("binary_sensor.loft_presence", state, entry, ts=5.0)
    assert ev is not None
    assert ev.available is False
    assert ev.present is False  # not a claim about the room; see occupancy tests
    assert ev.room == "loft" and ev.kind == "level"


def test_a_live_reading_is_always_available():
    entry = MAP["binary_sensor.loft_presence"]
    for state in ("on", "off"):
        ev = normalize("binary_sensor.loft_presence", state, entry, ts=1.0)
        assert ev is not None and ev.available is True


def test_unknown_kind_is_refused():
    bad = {"room": "x", "kind": "weird", "scope": "room"}
    assert normalize("binary_sensor.x", "on", bad) is None


# ---------------------------------------------------------------------------
# The filter — map membership is the whole gate
# ---------------------------------------------------------------------------


def _client(mapping: dict[str, Any] | None = None) -> HaEventClient:
    async def fetch() -> dict[str, Any]:
        # The fetcher returns kenzy-llm's whole /ha/map envelope, so "switched
        # off" is distinguishable from "broken".
        return {"ok": True, "entities": dict(MAP if mapping is None else mapping)}

    c = HaEventClient("http://ha.local:8123", "tok", fetch)
    c._map = dict(MAP if mapping is None else mapping)
    return c


def test_entities_outside_the_map_are_dropped():
    c = _client()
    assert c._evidence_from("binary_sensor.office_motion", "on") is not None
    # The firehose: a busy home emits hundreds of these a minute.
    assert c._evidence_from("sensor.living_room_temperature", "21.5") is None
    assert c._evidence_from("light.kitchen", "on") is None


def test_self_echo_never_reaches_the_map():
    """Kenzy's own MQTT entities must never be evidence — she would believe every
    room was occupied the moment she spoke. Enforced where the map is built, and
    deliberately not overridable by curation."""
    rows = [
        {"entity_id": "binary_sensor.kenzy_office_x", "area": "Office", "device_class": "motion"}
    ]
    built = build_occupancy_map(rows, [], {})
    assert built == {}
    # And even if it somehow appeared, it is not in the client's map.
    assert _client()._evidence_from("binary_sensor.kenzy_office_x", "on") is None


def test_curation_can_exclude_a_lying_sensor():
    """'Pets exist': the cat-crossed hallway PIR has to be removable."""
    rows = [{"entity_id": "binary_sensor.hall_motion", "area": "Hall", "device_class": "motion"}]
    assert "binary_sensor.hall_motion" in build_occupancy_map(rows, [], {})
    cur = {"occupancy": {"exclude": ["binary_sensor.hall_motion"]}}
    assert build_occupancy_map(rows, [], cur) == {}


def test_the_editor_shows_has_friendly_name():
    """The candidate list read row["name"], but the template never fetched
    friendly_name — so every sensor was labelled by its entity_id while the
    person rows (named from /api/states) were not. Both halves must read alike."""
    assert "friendly_name" in _OCCUPANCY_TEMPLATE
    rows = [
        {
            "entity_id": "binary_sensor.hall_fp2_presence",
            "name": "Hallway Presence (FP2)",
            "area": "Hall",
            "device_class": "occupancy",
        }
    ]
    got = classify_occupancy(rows, [{"entity_id": "person.alex", "name": "Alex"}], {})
    assert [c.name for c in got] == ["Hallway Presence (FP2)", "Alex"]


def test_a_nameless_row_still_falls_back_to_the_entity_id():
    rows = [{"entity_id": "binary_sensor.hall_motion", "area": "Hall", "device_class": "motion"}]
    assert classify_occupancy(rows, [], {})[0].name == "hall motion"


def test_ws_url_derivation():
    assert _client()._ws_url() == "ws://ha.local:8123/api/websocket"
    c = HaEventClient("https://ha.local:8123", "t", lambda: {})
    assert c._ws_url() == "wss://ha.local:8123/api/websocket"


# ---------------------------------------------------------------------------
# Staleness — the tracker must be able to say "I don't know"
# ---------------------------------------------------------------------------


def test_disconnected_is_stale():
    stats = HaEventStats()
    assert stats.is_stale(now=100.0) is True


def test_connected_and_recent_is_not_stale():
    stats = HaEventStats(connected=True, last_event_at=100.0)
    assert stats.is_stale(now=100.0 + _STALE_AFTER_S - 1) is False


def test_connected_but_silent_goes_stale():
    """A socket that is 'up' but has produced nothing is not trustworthy — the
    frozen-picture failure the design calls out."""
    stats = HaEventStats(connected=True, last_event_at=100.0)
    assert stats.is_stale(now=100.0 + _STALE_AFTER_S + 1) is True


# ---------------------------------------------------------------------------
# The socket, against a scripted fake HA
# ---------------------------------------------------------------------------


class _FakeHaWs:
    """Replays HA's side: auth handshake, get_states result, then events."""

    def __init__(self, states: list[dict], events: list[dict]) -> None:
        self.sent: list[dict] = []
        self._states = states
        self._events = events
        self._phase = "greet"

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    async def recv(self) -> str:
        if self._phase == "greet":
            self._phase = "auth"
            return json.dumps({"type": "auth_required", "ha_version": "2026.7"})
        if self._phase == "auth":
            self._phase = "seed"
            return json.dumps({"type": "auth_ok"})
        if self._phase == "seed":
            self._phase = "events"
            req = next(m["id"] for m in self.sent if m.get("type") == "get_states")
            return json.dumps(
                {"id": req, "type": "result", "success": True, "result": self._states}
            )
        raise AssertionError("recv past the scripted phases")

    def __aiter__(self):
        async def gen():
            for ev in self._events:
                yield json.dumps(ev)

        return gen()


def _state_changed(entity_id: str, state: str) -> dict:
    return {
        "type": "event",
        "event": {"data": {"entity_id": entity_id, "new_state": {"state": state}}},
    }


async def test_auth_seed_and_consume():
    got: list[Evidence] = []
    c = _client()
    c.subscribe(got.append)
    ws = _FakeHaWs(
        states=[
            {"entity_id": "person.alex", "state": "home"},
            {"entity_id": "binary_sensor.loft_presence", "state": "on"},
            {"entity_id": "sensor.irrelevant", "state": "42"},
        ],
        events=[
            _state_changed("binary_sensor.office_motion", "on"),
            _state_changed("light.kitchen", "on"),  # firehose noise
        ],
    )
    await c._authenticate(ws)
    await c._seed(ws)
    await c._subscribe(ws)
    await c._consume(ws)

    assert ws.sent[0]["type"] == "auth" and ws.sent[0]["access_token"] == "tok"
    assert any(m.get("type") == "subscribe_events" for m in ws.sent)
    # Seed re-established the two mapped entities; the third is not evidence.
    seeded = [e.entity_id for e in got[:2]]
    assert sorted(seeded) == ["binary_sensor.loft_presence", "person.alex"]
    # Then one event survived the filter and one was dropped.
    assert got[-1].entity_id == "binary_sensor.office_motion"
    assert c.stats.received == 2 and c.stats.emitted == 1 and c.stats.dropped == 1


async def test_a_dying_sensor_releases_its_room_end_to_end():
    """The whole path, client → filter → tracker, for the failure that pinned a
    room forever: a level asserts, then the device dies. HA never says "clear",
    so the release has to come from the `unavailable` reading itself."""
    from kenzy.server.occupancy import OccupancyTracker

    tracker = OccupancyTracker()
    c = _client()
    c.subscribe(tracker.on_evidence)
    ws = _FakeHaWs(
        states=[{"entity_id": "binary_sensor.loft_presence", "state": "on"}],
        events=[_state_changed("binary_sensor.loft_presence", "unavailable")],
    )
    await c._authenticate(ws)
    await c._seed(ws)
    assert tracker.room_state("loft")["state"] == "occupied"
    assert tracker.room_state("loft")["held"] == ["binary_sensor.loft_presence"]

    await c._consume(ws)
    state = tracker.room_state("loft")
    assert state["held"] == []  # the hold is gone, not waiting on a release
    assert state["source"] == "dropout"
    # And it counts as an event that was USED, not one the filter threw away —
    # the Presence tab's "events used / seen" should not under-report it.
    assert c.stats.emitted == 1 and c.stats.dropped == 0


async def test_a_deleted_entity_also_releases_its_room():
    """HA reports a removal as `new_state: null`, which reaches us as "" — the
    same stuck-hold if it is treated as "no information"."""
    from kenzy.server.occupancy import OccupancyTracker

    tracker = OccupancyTracker()
    c = _client()
    c.subscribe(tracker.on_evidence)
    ws = _FakeHaWs(
        states=[{"entity_id": "binary_sensor.loft_presence", "state": "on"}],
        events=[{"type": "event", "event": {"data": {
            "entity_id": "binary_sensor.loft_presence", "new_state": None}}}],
    )
    await c._authenticate(ws)
    await c._seed(ws)
    await c._consume(ws)
    assert tracker.room_state("loft")["held"] == []


async def test_seed_is_what_makes_persistence_unnecessary():
    """person.* only emits on CHANGE, so without the seed an all-day-home
    household would read 'unknown' all day after a restart."""
    got: list[Evidence] = []
    c = _client()
    c.subscribe(got.append)
    ws = _FakeHaWs(states=[{"entity_id": "person.alex", "state": "home"}], events=[])
    await c._authenticate(ws)
    await c._seed(ws)
    assert [(e.entity_id, e.present) for e in got] == [("person.alex", True)]


async def test_auth_failure_is_raised_not_swallowed():
    class _Reject(_FakeHaWs):
        async def recv(self) -> str:
            if self._phase == "greet":
                self._phase = "auth"
                return json.dumps({"type": "auth_required"})
            return json.dumps({"type": "auth_invalid", "message": "bad token"})

    with pytest.raises(RuntimeError, match="bad token"):
        await _client()._authenticate(_Reject([], []))


async def test_a_broken_subscriber_cannot_break_the_socket():
    got: list[Evidence] = []
    c = _client()
    c.subscribe(lambda ev: (_ for _ in ()).throw(ValueError("boom")))
    c.subscribe(got.append)
    ws = _FakeHaWs(states=[], events=[_state_changed("binary_sensor.office_motion", "on")])
    await c._consume(ws)
    assert len(got) == 1  # the healthy consumer still got it


async def test_session_refuses_to_subscribe_without_a_map():
    """Found on the first real run: with no map every event is filtered, so the
    socket looks 'connected' while being blind. Better to fail and back off."""

    async def empty() -> dict[str, Any]:
        return {"ok": True, "entities": {}}

    c = HaEventClient("http://ha.local:8123", "tok", empty)
    with pytest.raises(RuntimeError, match="map unavailable"):
        await c._session()


async def test_stop_is_idempotent_and_clears_connected():
    c = _client()
    c.stats.connected = True
    await c.stop()
    await c.stop()
    assert c.stats.connected is False
    assert isinstance(asyncio.get_running_loop(), asyncio.AbstractEventLoop)


# ---------------------------------------------------------------------------
# Switched off vs broken — a disabled integration must not retry forever
# ---------------------------------------------------------------------------


async def test_disabled_integration_raises_its_own_signal():
    """Founder call: if home_assistant is disabled, stop trying. A transient
    failure still deserves backoff — these must not look the same."""

    async def disabled() -> dict[str, Any]:
        return {"ok": False, "disabled": True, "error": "integration disabled", "entities": {}}

    c = HaEventClient("http://ha.local:8123", "tok", disabled)
    with pytest.raises(IntegrationDisabled):
        await c.refresh_map()
    assert c.stats.disabled is True


async def test_a_broken_map_is_not_disabled():
    """An unreachable llm must back off and retry, not park."""

    async def broken() -> dict[str, Any]:
        raise RuntimeError("connection refused")

    c = HaEventClient("http://ha.local:8123", "tok", broken)
    assert await c.refresh_map() is False
    assert c.stats.disabled is False  # retryable, so the socket keeps trying


async def test_reenabling_clears_the_disabled_flag():
    state = {"off": True}

    async def toggling() -> dict[str, Any]:
        if state["off"]:
            return {"ok": False, "disabled": True, "entities": {}}
        return {"ok": True, "entities": dict(MAP)}

    c = HaEventClient("http://ha.local:8123", "tok", toggling)
    with pytest.raises(IntegrationDisabled):
        await c.refresh_map()
    state["off"] = False
    assert await c.refresh_map() is True
    assert c.stats.disabled is False


async def test_a_refetched_map_reports_its_entities_to_the_tracker():
    """The seam that lets B fade out holds for entities the map dropped (curation
    excluded them, or they left HA). Without it, an assertion left behind by an
    entity that can never emit again is held forever."""
    seen: list[set[str]] = []

    async def fetch() -> dict[str, Any]:
        return {"ok": True, "entities": dict(MAP)}

    c = HaEventClient("http://ha.local:8123", "tok", fetch, on_map=seen.append)
    assert await c.refresh_map() is True
    assert seen == [set(MAP)]


async def test_a_broken_map_listener_cannot_break_the_socket():
    async def fetch() -> dict[str, Any]:
        return {"ok": True, "entities": dict(MAP)}

    def boom(_ids: set[str]) -> None:
        raise RuntimeError("consumer bug")

    c = HaEventClient("http://ha.local:8123", "tok", fetch, on_map=boom)
    assert await c.refresh_map() is True  # refresh still succeeded
    assert c.stats.map_entities == len(MAP)


async def test_wake_cuts_a_wait_short():
    """The dashboard pokes on the skill toggle, so re-enabling recovers at once
    rather than waiting out the slow disabled re-check."""
    c = _client()
    c.wake()
    await asyncio.wait_for(c._sleep(30.0), timeout=2.0)  # returns immediately


async def test_wake_interrupts_a_live_session_and_restales_the_map():
    """Found live: disabling the integration did nothing while the socket was
    CONNECTED — a live session only refetches the map on reconnect or after the
    15-minute TTL, so it kept streaming. wake() must break the consume loop AND
    mark the map stale, or the toggle takes effect up to 15 minutes late."""
    c = _client()
    c.stats.map_fetched_at = 12345.0
    got: list[Evidence] = []
    c.subscribe(got.append)
    ws = _FakeHaWs(
        states=[],
        events=[
            _state_changed("binary_sensor.office_motion", "on"),
            _state_changed("binary_sensor.office_motion", "off"),
        ],
    )
    c.wake()
    await c._consume(ws)
    assert got == []  # bailed before consuming anything
    assert c.stats.map_fetched_at == 0.0  # next session re-asks for the map
