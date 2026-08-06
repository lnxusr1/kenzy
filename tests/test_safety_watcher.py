"""Tier A watcher (5.0.6): hazard state changes → gated announcements."""

from __future__ import annotations

from kenzy.server.proactive import ProactiveGate
from kenzy.server.safety import SafetyWatcher, compose

T0 = 1_700_000_000.0

MAP = {
    "binary_sensor.hall_smoke": {
        "room": "hallway",
        "room_name": "Hallway",
        "hazard": "smoke",
        "asserted": "on",
    },
    "alarm_control_panel.house": {
        "room": "",
        "room_name": "",
        "hazard": "an alarm going off",
        "asserted": "triggered",
    },
}


def _watcher(**kw) -> SafetyWatcher:
    kw.setdefault("enabled", True)
    kw.setdefault("categories_enabled", frozenset({"safety"}))
    kw.setdefault("repeat_after", {"safety": 300})
    w = SafetyWatcher(ProactiveGate(**kw))
    w.set_map(MAP)
    return w


def test_an_asserting_hazard_announces():
    a = _watcher().consider("binary_sensor.hall_smoke", "on", now=T0)
    assert a is not None
    assert a.text == "There's smoke in the hallway."
    assert a.alert is True  # muted rooms still hear it
    assert a.rooms == ()  # everywhere


def test_the_alarm_panel_only_fires_on_triggered():
    w = _watcher()
    for quiet in ("armed_away", "armed_home", "disarmed", "arming", "pending"):
        assert w.consider("alarm_control_panel.house", quiet, now=T0) is None
    a = w.consider("alarm_control_panel.house", "triggered", now=T0)
    assert a is not None and a.text == "There's an alarm going off."


def test_entities_outside_the_map_are_ignored():
    assert _watcher().consider("binary_sensor.front_door", "on", now=T0) is None


def test_default_deny_means_silence_until_switched_on():
    w = SafetyWatcher(ProactiveGate.from_config({}))
    w.set_map(MAP)
    assert w.consider("binary_sensor.hall_smoke", "on", now=T0) is None


# --- the release rule, which the silence model depends on --------------------


def test_going_back_to_normal_re_arms_the_next_assertion():
    w = _watcher(repeat_after={"safety": 3600})
    assert w.consider("binary_sensor.hall_smoke", "on", now=T0) is not None
    w.spoken("binary_sensor.hall_smoke", now=T0)
    # Still asserting: suppressed by the repeat window.
    assert w.consider("binary_sensor.hall_smoke", "on", now=T0 + 60) is None
    # Released, then a NEW fire — speaks at once, not in an hour.
    assert w.consider("binary_sensor.hall_smoke", "off", now=T0 + 61) is None
    assert w.consider("binary_sensor.hall_smoke", "on", now=T0 + 62) is not None


def test_silence_survives_until_the_sensor_cycles():
    w = _watcher()
    w.consider("binary_sensor.hall_smoke", "on", now=T0)
    w.spoken("binary_sensor.hall_smoke", now=T0)
    w._gate.acknowledge(now=T0)  # someone said "that's enough"

    assert w.consider("binary_sensor.hall_smoke", "on", now=T0 + 86_400) is None
    w.consider("binary_sensor.hall_smoke", "off", now=T0 + 86_401)
    assert w.consider("binary_sensor.hall_smoke", "on", now=T0 + 86_402) is not None


def test_unavailable_is_not_a_release():
    """A flat battery is not a fire going out — and treating it as a release
    would let a flapping sensor defeat silence entirely."""
    w = _watcher()
    w.consider("binary_sensor.hall_smoke", "on", now=T0)
    w.spoken("binary_sensor.hall_smoke", now=T0)
    w._gate.acknowledge(now=T0)

    for noise in ("unavailable", "unknown", ""):
        assert w.consider("binary_sensor.hall_smoke", noise, now=T0 + 1) is None
    # Still silenced — the cycle never happened.
    assert w.consider("binary_sensor.hall_smoke", "on", now=T0 + 2) is None


def test_dropping_an_entity_from_the_map_does_not_strand_its_silence():
    """Excluding a lying sensor must not leave a silence nothing can clear."""
    w = _watcher()
    w.consider("binary_sensor.hall_smoke", "on", now=T0)
    w.spoken("binary_sensor.hall_smoke", now=T0)
    w._gate.acknowledge(now=T0)
    assert w._gate.silenced() == ["binary_sensor.hall_smoke"]

    w.set_map({k: v for k, v in MAP.items() if k != "binary_sensor.hall_smoke"})
    assert w._gate.silenced() == []
    assert w.known() == 1


# --- delivery accounting -----------------------------------------------------


def test_an_undelivered_announcement_does_not_suppress_the_retry():
    w = _watcher()
    assert w.consider("binary_sensor.hall_smoke", "on", now=T0) is not None  # never spoken
    assert w.consider("binary_sensor.hall_smoke", "on", now=T0 + 1) is not None


def test_compose_handles_a_hazard_with_no_room():
    assert compose("smoke", "") == "There's smoke."
    assert compose("a water leak", "Basement") == "There's a water leak in the basement."
