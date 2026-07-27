"""v5 occupancy spine — the tracker's math and semantics (Slice B).

The decay curves are the whole design: everything downstream is a threshold on
two numbers, so they are tested exhaustively here (the calibration.py precedent —
pure math, no I/O, no clock). The behaviors that earn their own tests are the
three the design exists to protect:

* absence is *unknown*, never *empty*;
* a LEVEL that is still asserting must not decay, but a PULSE must;
* a dropped socket makes held levels stale — it does not make rooms empty.
"""

from __future__ import annotations

from kenzy.server.ha_events import Evidence
from kenzy.server.occupancy import (
    _OCCUPIED_THRESHOLD,
    _PULSE_HALFLIFE_S,
    _VOICE_HALFLIFE_S,
    OccupancyTracker,
    decay,
    room_slug,
)

T0 = 1_000.0


def _ev(entity: str, room: str, kind: str, present: bool, ts: float, scope: str = "room"):
    return Evidence(entity, room, kind, scope, present, ts)


# ---------------------------------------------------------------------------
# The curve
# ---------------------------------------------------------------------------


def test_decay_halves_at_the_halflife():
    assert decay(1.0, 0.0, 100.0) == 1.0
    assert decay(1.0, 100.0, 100.0) == 0.5
    assert decay(1.0, 200.0, 100.0) == 0.25
    assert decay(1.0, 300.0, 100.0) == 0.125


def test_decay_is_monotonic_and_bounded():
    prev = 1.0
    for elapsed in range(0, 1200, 30):
        cur = decay(1.0, float(elapsed), _PULSE_HALFLIFE_S)
        assert 0.0 <= cur <= 1.0
        assert cur <= prev
        prev = cur


def test_decay_degenerate_inputs():
    assert decay(0.0, 10.0, 100.0) == 0.0
    assert decay(1.0, -5.0, 100.0) == 1.0  # clock skew must not amplify belief
    assert decay(1.0, 10.0, 0.0) == 0.0  # a zero half-life means "worthless now"


def test_room_slug_matches_ha_area_slugging():
    """The join key. Must stay byte-compatible with ha_model._slug."""
    from kenzy.llm.builtin_skills.ha_model import _slug

    for name in ("Office", "Living Room", "Kid's Room", "  Loft  ", "Master Bedroom"):
        assert room_slug(name) == _slug(name), name


# ---------------------------------------------------------------------------
# Absence is not a value
# ---------------------------------------------------------------------------


def test_unseen_room_is_unknown_not_empty():
    t = OccupancyTracker()
    state = t.room_state("cellar", now=T0)
    assert state["state"] == "unknown"
    assert state["confidence"] == 0.0


def test_a_faded_pulse_ends_at_unknown_never_empty():
    t = OccupancyTracker()
    t.on_evidence(_ev("binary_sensor.m", "office", "pulse", True, T0))
    far_future = t.room_state("office", now=T0 + 10_000)
    assert far_future["state"] == "unknown"  # not "empty" — we simply don't know


def test_connected_rooms_appear_even_with_no_evidence():
    """A room with a node but no sensor must show up honestly, not vanish."""
    t = OccupancyTracker()
    snap = t.snapshot(["kitchen"], now=T0)
    assert [r["room"] for r in snap["rooms"]] == ["kitchen"]
    assert snap["rooms"][0]["state"] == "unknown"


# ---------------------------------------------------------------------------
# Pulse vs level — the distinction the whole model rests on
# ---------------------------------------------------------------------------


def test_pulse_decays_over_time():
    t = OccupancyTracker()
    t.on_evidence(_ev("binary_sensor.motion", "office", "pulse", True, T0))
    assert t.room_state("office", now=T0)["confidence"] == 1.0
    half = t.room_state("office", now=T0 + _PULSE_HALFLIFE_S)["confidence"]
    assert abs(half - 0.5) < 1e-6


def test_asserting_level_never_decays():
    """A healthy mmWave still saying 'occupied' must not fade just because time
    passed — that was the failure mode of modelling both kinds as decay."""
    t = OccupancyTracker()
    t.on_evidence(_ev("binary_sensor.mmwave", "loft", "level", True, T0))
    for elapsed in (0, 600, 3600, 86_400):
        state = t.room_state("loft", now=T0 + elapsed)
        assert state["state"] == "occupied", elapsed
        assert state["confidence"] == 1.0, elapsed


def test_released_level_decays_from_full():
    t = OccupancyTracker()
    t.on_evidence(_ev("binary_sensor.mmwave", "loft", "level", True, T0))
    t.on_evidence(_ev("binary_sensor.mmwave", "loft", "level", False, T0 + 100))
    assert t.room_state("loft", now=T0 + 100)["confidence"] == 1.0  # just left
    later = t.room_state("loft", now=T0 + 400)["confidence"]
    assert 0.0 < later < 1.0  # fading, not slammed to unknown


def test_idle_level_at_seed_is_not_a_release():
    """Found on the first real-HA run: every room whose motion sensor seeded
    'off' read OCCUPIED for minutes, because an idle sensor was treated as
    'someone just left'. An entity that never asserted is evidence of absence."""
    t = OccupancyTracker()
    t.on_evidence(_ev("binary_sensor.garage", "garage", "level", False, T0))
    assert t.room_state("garage", now=T0)["state"] == "unknown"
    assert t.room_state("garage", now=T0)["confidence"] == 0.0


def test_one_released_sensor_does_not_clear_a_room_another_still_holds():
    t = OccupancyTracker()
    t.on_evidence(_ev("binary_sensor.a", "loft", "level", True, T0))
    t.on_evidence(_ev("binary_sensor.b", "loft", "level", True, T0))
    t.on_evidence(_ev("binary_sensor.a", "loft", "level", False, T0 + 10))
    state = t.room_state("loft", now=T0 + 5_000)
    assert state["state"] == "occupied"  # b is still asserting
    assert state["held"] == ["binary_sensor.b"]


def test_pulse_off_edge_is_not_evidence():
    """A PIR going quiet means nothing — you may simply be sitting still."""
    t = OccupancyTracker()
    t.on_evidence(_ev("binary_sensor.motion", "office", "pulse", False, T0))
    assert t.room_state("office", now=T0)["state"] == "unknown"


# ---------------------------------------------------------------------------
# Staleness is not absence
# ---------------------------------------------------------------------------


def test_stale_socket_stops_trusting_held_levels():
    t = OccupancyTracker()
    t.on_evidence(_ev("binary_sensor.mmwave", "loft", "level", True, T0))
    assert t.room_state("loft", now=T0 + 3600)["state"] == "occupied"
    t.set_stale(True)
    state = t.room_state("loft", now=T0 + 3600)
    assert state["state"] == "unknown"  # can't vouch for it — but not "empty"
    assert state["stale"] is True


# ---------------------------------------------------------------------------
# The voice half
# ---------------------------------------------------------------------------


def test_voice_makes_a_room_occupied_even_for_an_unknown_speaker():
    """Someone spoke, so someone is there — knowing WHO is a separate question."""
    t = OccupancyTracker()
    t.on_voice("office", recognized=False, ts=T0)
    state = t.room_state("office", now=T0)
    assert state["state"] == "occupied"
    assert "person_id" not in state  # no identity claim without recognition


def test_recognized_voice_sets_a_decaying_identity_anchor():
    t = OccupancyTracker()
    t.on_voice("office", person_id="p1", person_name="Alex", recognized=True, ts=T0)
    now = t.room_state("office", now=T0)
    assert now["person_name"] == "Alex"
    assert now["identity_confidence"] == 1.0
    # Identity fades faster than occupancy: who was here is a shorter-lived claim.
    later = t.room_state("office", now=T0 + _VOICE_HALFLIFE_S)
    assert abs(later["identity_confidence"] - 0.5) < 1e-6
    assert later["confidence"] > later["identity_confidence"]


def test_identity_drops_off_once_it_is_meaningless():
    t = OccupancyTracker()
    t.on_voice("office", person_id="p1", person_name="Alex", recognized=True, ts=T0)
    assert "person_id" not in t.room_state("office", now=T0 + 3600)


# ---------------------------------------------------------------------------
# House scope
# ---------------------------------------------------------------------------


def test_person_entities_are_house_scope_not_rooms():
    t = OccupancyTracker()
    t.on_evidence(Evidence("person.alex", "", "level", "house", True, T0, name="Alex"))
    snap = t.snapshot(now=T0)
    assert snap["rooms"] == []  # a person being home credits no room
    assert snap["people"] == [
        {"entity_id": "person.alex", "name": "Alex", "home": True, "age": 0.0}
    ]


def test_snapshot_reports_threshold_bands_honestly():
    t = OccupancyTracker()
    t.on_evidence(_ev("binary_sensor.m", "office", "pulse", True, T0))
    # Just past the half-life the confidence sits under the occupied threshold
    # but well above the unknown floor: "maybe" is a real answer, not a rounding.
    state = t.room_state("office", now=T0 + _PULSE_HALFLIFE_S + 30)
    assert state["confidence"] < _OCCUPIED_THRESHOLD
    assert state["state"] == "maybe"
