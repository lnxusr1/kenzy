"""Which entities count as Tier A safety (5.0.6).

The governing rule: every entry is a hazard a DEVICE asserted. Kenzy relays it
and infers nothing — no "a door opened and everyone's out, so that's an
intruder". That boundary is what makes the tier safe to speak unprompted.
"""

from __future__ import annotations

from kenzy.llm.builtin_skills.ha_model import build_safety_map


def _row(eid, dev_class="", area="Kitchen"):
    return {"entity_id": eid, "device_class": dev_class, "area": area}


def test_the_hazard_classes_are_picked_up():
    rows = [
        _row("binary_sensor.hall_smoke", "smoke", "Hallway"),
        _row("binary_sensor.garage_co", "carbon_monoxide", "Garage"),
        _row("binary_sensor.boiler_gas", "gas", "Utility"),
        _row("binary_sensor.basement_water", "moisture", "Basement"),
    ]
    out = build_safety_map(rows, {})
    assert set(out) == {r["entity_id"] for r in rows}
    assert out["binary_sensor.hall_smoke"]["hazard"] == "smoke"
    assert out["binary_sensor.basement_water"]["hazard"] == "a water leak"
    assert all(e["asserted"] == "on" for e in out.values())


def test_the_alarm_panel_asserts_on_triggered_not_armed():
    """'Armed' is not an emergency. Only 'triggered' is."""
    out = build_safety_map([_row("alarm_control_panel.house", area="")], {})
    entry = out["alarm_control_panel.house"]
    assert entry["asserted"] == "triggered"
    # Phrased to fit the same "There's ___" frame as every other hazard, so the
    # server composes one sentence and never branches on hazard type.
    assert entry["hazard"] == "an alarm going off"


def test_ordinary_sensors_are_not_safety():
    rows = [
        _row("binary_sensor.hall_motion", "motion", "Hallway"),
        _row("binary_sensor.front_door", "door", "Porch"),
        _row("light.kitchen", "", "Kitchen"),
    ]
    assert build_safety_map(rows, {}) == {}


def test_an_unplaced_hazard_is_kept_with_no_room():
    """A smoke alarm with no area is still worth shouting about — failing to
    name the room is a much smaller error than failing to mention the fire."""
    out = build_safety_map([_row("binary_sensor.smoke", "smoke", area="")], {})
    assert out["binary_sensor.smoke"]["room"] == ""


def test_curation_can_exclude_a_lying_sensor():
    rows = [_row("binary_sensor.test_smoke", "smoke", "Workshop")]
    out = build_safety_map(rows, {"safety": {"exclude": ["binary_sensor.test_smoke"]}})
    assert out == {}


def test_curation_can_include_something_the_classes_missed():
    rows = [_row("binary_sensor.sump_high", "", "Basement")]
    out = build_safety_map(rows, {"safety": {"include": ["binary_sensor.sump_high"]}})
    entry = out["binary_sensor.sump_high"]
    assert entry["asserted"] == "on"
    # Honest about not knowing what kind of hazard it is.
    assert entry["hazard"] == "an alert"


def test_kenzys_own_entities_are_never_safety_sources():
    """Same never-configurable rule the rest of the resolver uses — she must not
    be able to alarm herself."""
    rows = [_row("binary_sensor.kenzy_office_something", "smoke", "Office")]
    assert build_safety_map(rows, {}) == {}


def test_rooms_are_slugged_like_the_occupancy_map():
    out = build_safety_map([_row("binary_sensor.s", "smoke", "Living Room")], {})
    assert out["binary_sensor.s"]["room"] == "living_room"
    assert out["binary_sensor.s"]["room_name"] == "Living Room"


# --- the curation editor's candidate list ------------------------------------


def test_candidates_tag_what_counts_and_what_does_not():
    from kenzy.llm.builtin_skills.ha_model import classify_safety

    rows = [
        _row("binary_sensor.hall_smoke", "smoke", "Hallway"),
        _row("binary_sensor.hall_motion", "motion", "Hallway"),
        _row("alarm_control_panel.house", "", ""),
    ]
    out = {c.entity_id: c for c in classify_safety(rows, {})}

    assert out["binary_sensor.hall_smoke"].hazard == "smoke"
    assert out["binary_sensor.hall_smoke"].reason == ""
    # The editor must show the ones that DON'T count, with why.
    assert out["binary_sensor.hall_motion"].hazard == ""
    assert out["binary_sensor.hall_motion"].reason == "not a hazard sensor"
    assert out["alarm_control_panel.house"].hazard == "an alarm going off"


def test_an_excluded_sensor_says_so_rather_than_vanishing():
    """You can't un-exclude what the editor doesn't show you."""
    from kenzy.llm.builtin_skills.ha_model import classify_safety

    rows = [_row("binary_sensor.workshop_smoke", "smoke", "Workshop")]
    (cand,) = classify_safety(rows, {"safety": {"exclude": ["binary_sensor.workshop_smoke"]}})
    assert cand.hazard == "" and cand.reason == "excluded"


def test_an_included_oddity_shows_as_counting():
    from kenzy.llm.builtin_skills.ha_model import classify_safety

    rows = [_row("binary_sensor.sump_high", "", "Basement")]
    (cand,) = classify_safety(rows, {"safety": {"include": ["binary_sensor.sump_high"]}})
    assert cand.hazard == "an alert" and cand.reason == ""


def test_kenzys_own_entities_are_not_even_offered():
    from kenzy.llm.builtin_skills.ha_model import classify_safety

    assert classify_safety([_row("binary_sensor.kenzy_office_x", "smoke", "Office")], {}) == []


def test_the_hazard_template_reads_device_class_safely():
    """`e.attributes.device_class` returns LoggingUndefined for a sensor that
    has none — not JSON-serializable, so HA 400s the WHOLE render and every
    hazard sensor disappears at once. Most binary_sensors have no device_class,
    so that is the common case, not an edge one. Found live against real HA.
    """
    from kenzy.llm.builtin_skills.ha_model import _SAFETY_TEMPLATE

    assert "state_attr(e.entity_id, 'device_class')" in _SAFETY_TEMPLATE
    assert "e.attributes.device_class" not in _SAFETY_TEMPLATE
