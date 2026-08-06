"""The proactive policy gate (5.0.6) — default-deny, and honest about why.

The gate is the only thing standing between a flapping sensor and a house that
shouts at 3am, so these tests care most about the REFUSALS.
"""

from __future__ import annotations

from kenzy.server.proactive import (
    DENY_ACKNOWLEDGED,
    DENY_CATEGORY_OFF,
    DENY_DISABLED,
    DENY_NO_ROOMS,
    DENY_QUIET_HOURS,
    DENY_RATE,
    DENY_REPEAT,
    SAFETY,
    Category,
    ProactiveGate,
    parse_quiet_hours,
)

T0 = 1_700_000_000.0


def _gate(**kw) -> ProactiveGate:
    kw.setdefault("enabled", True)
    kw.setdefault("categories_enabled", frozenset({"safety"}))
    return ProactiveGate(**kw)


# --- default-deny ------------------------------------------------------------


def test_nothing_speaks_out_of_the_box():
    """A fresh config must not talk. Every switch is opt-in."""
    gate = ProactiveGate.from_config({})
    d = gate.evaluate(SAFETY, "smoke", now=T0)
    assert not d.allowed and d.reason == DENY_CATEGORY_OFF


def test_master_switch_beats_an_enabled_category():
    gate = _gate(enabled=False)
    assert gate.evaluate(SAFETY, "smoke", now=T0).reason == DENY_DISABLED


def test_enabled_safety_speaks():
    d = _gate().evaluate(SAFETY, "smoke", now=T0)
    assert d.allowed and d.alert is True  # muted rooms still hear safety


# --- Tier A's exemptions are the point --------------------------------------


def test_safety_ignores_quiet_hours():
    """A smoke alert that respects quiet hours is not a safety feature."""
    gate = _gate(quiet_hours=(22 * 60, 7 * 60))
    d = gate.evaluate(SAFETY, "smoke", now=T0, local_minutes=3 * 60)  # 03:00
    assert d.allowed


def test_a_non_exempt_category_is_silenced_in_quiet_hours():
    chatty = Category("chatty")
    gate = _gate(
        quiet_hours=(22 * 60, 7 * 60),
        categories_enabled=frozenset({"chatty"}),
        repeat_after={"chatty": 0},
    )
    assert gate.evaluate(chatty, "x", now=T0, local_minutes=3 * 60).reason == DENY_QUIET_HOURS
    # ...and speaks fine outside the window.
    assert gate.evaluate(chatty, "x", now=T0, local_minutes=12 * 60).allowed


def test_quiet_hours_wrapping_midnight_is_the_normal_case():
    assert parse_quiet_hours("22:00-07:00") == (1320, 420)
    chatty = Category("chatty")
    gate = _gate(
        quiet_hours=(1320, 420),
        categories_enabled=frozenset({"chatty"}),
        repeat_after={"chatty": 0},
    )
    for minute, quiet in ((23 * 60, True), (2 * 60, True), (7 * 60, False), (21 * 60, False)):
        d = gate.evaluate(chatty, "x", now=T0, local_minutes=minute)
        assert d.allowed is (not quiet), f"{minute // 60}:00 wrong"


def test_safety_ignores_do_not_disturb_rooms():
    gate = _gate(dnd_rooms=frozenset({"bedroom"}))
    d = gate.evaluate(SAFETY, "smoke", ("bedroom",), now=T0)
    assert d.allowed and d.rooms == ("bedroom",)


def test_dnd_narrows_rooms_for_a_non_exempt_category():
    chatty = Category("chatty")
    gate = _gate(
        dnd_rooms=frozenset({"bedroom"}),
        categories_enabled=frozenset({"chatty"}),
        repeat_after={"chatty": 0},
    )
    d = gate.evaluate(chatty, "x", ("bedroom", "kitchen"), now=T0)
    assert d.allowed and d.rooms == ("kitchen",)
    # Every named room excluded ⇒ refuse, rather than quietly broadcasting.
    assert gate.evaluate(chatty, "y", ("bedroom",), now=T0).reason == DENY_NO_ROOMS


# --- repetition and rate -----------------------------------------------------


def test_the_same_condition_is_not_re_announced():
    gate = _gate(repeat_after={"safety": 300})
    assert gate.evaluate(SAFETY, "smoke.kitchen", now=T0).allowed
    gate.commit("smoke.kitchen", now=T0)
    assert gate.evaluate(SAFETY, "smoke.kitchen", now=T0 + 60).reason == DENY_REPEAT
    assert gate.evaluate(SAFETY, "smoke.kitchen", now=T0 + 301).allowed


def test_a_different_condition_is_not_suppressed_by_another():
    gate = _gate(repeat_after={"safety": 300})
    gate.commit("smoke.kitchen", now=T0)
    assert gate.evaluate(SAFETY, "leak.basement", now=T0 + 1).allowed


def test_repeat_is_checked_before_rate_so_a_flapping_sensor_cannot_starve_others():
    """A sensor chattering on one entity must not eat the hourly budget and
    silence a genuinely different emergency behind it."""
    gate = _gate(rate_limit=3, repeat_after={"safety": 300})
    for i in range(10):
        d = gate.evaluate(SAFETY, "smoke.kitchen", now=T0 + i)
        if d.allowed:
            gate.commit("smoke.kitchen", now=T0 + i)
    assert gate.evaluate(SAFETY, "leak.basement", now=T0 + 20).allowed


def test_rate_limit_caps_distinct_alerts():
    gate = _gate(rate_limit=2, rate_window=3600, repeat_after={"safety": 0})
    for i in range(2):
        assert gate.evaluate(SAFETY, f"k{i}", now=T0).allowed
        gate.commit(f"k{i}", now=T0)
    assert gate.evaluate(SAFETY, "k9", now=T0).reason == DENY_RATE
    # The window rolls.
    assert gate.evaluate(SAFETY, "k9", now=T0 + 3601).allowed


def test_an_undelivered_decision_costs_nothing():
    """evaluate() must not consume budget — a dead TTS service would otherwise
    silently suppress the NEXT genuine alert."""
    gate = _gate(rate_limit=1, repeat_after={"safety": 300})
    assert gate.evaluate(SAFETY, "smoke", now=T0).allowed  # never committed
    d = gate.evaluate(SAFETY, "smoke", now=T0 + 1)
    assert d.allowed, "an unspoken decision burned the budget"


# --- config ------------------------------------------------------------------


def test_from_config_reads_the_server_block():
    gate = ProactiveGate.from_config(
        {
            "enabled": True,
            "quiet_hours": "22:00-07:00",
            "dnd_rooms": ["bedroom"],
            "rate_limit": 4,
            "safety": {"enabled": True, "repeat_after": 120},
        }
    )
    assert gate.quiet_hours == (1320, 420)
    assert gate.dnd_rooms == frozenset({"bedroom"})
    assert gate.rate_limit == 4
    assert "safety" in gate.categories_enabled
    assert gate.repeat_after["safety"] == 120


def test_malformed_quiet_hours_disables_the_window_rather_than_crashing():
    for bad in ("", "nonsense", "22:00", "25:00-07:00", "22:00-22:00", "22-7"):
        assert parse_quiet_hours(bad) is None


def test_every_decision_carries_a_reason_for_the_audit_trail():
    gate = ProactiveGate.from_config({})
    rec = gate.evaluate(SAFETY, "smoke", now=T0).as_record()
    assert rec["allowed"] is False and rec["reason"]
    allowed = _gate().evaluate(SAFETY, "smoke", now=T0).as_record()
    assert allowed["allowed"] is True and allowed["reason"]


# --- cancelling a repeating announcement -------------------------------------
# The 5.0.5 lesson, applied forward: anything that repeats needs a stop, and the
# stop must hang off an event that actually fires.


def test_acknowledging_quiets_a_still_asserted_condition():
    gate = _gate(repeat_after={"safety": 60})
    assert gate.evaluate(SAFETY, "smoke.hall", now=T0).allowed
    gate.commit("smoke.hall", now=T0)

    gate.acknowledge(now=T0 + 10)  # someone spoke to Kenzy
    # Past the normal repeat gap, but acknowledged — stays quiet.
    assert gate.evaluate(SAFETY, "smoke.hall", now=T0 + 120).reason == DENY_ACKNOWLEDGED


def test_silence_lasts_until_the_condition_cycles_not_a_timer():
    """Silenced means silenced. No snooze — only the sensor releasing re-arms it."""
    gate = _gate(repeat_after={"safety": 60})
    gate.commit("smoke.hall", now=T0)
    gate.acknowledge(now=T0)
    # Hours later, still asserted, still silent.
    assert gate.evaluate(SAFETY, "smoke.hall", now=T0 + 86_400).reason == DENY_ACKNOWLEDGED
    assert gate.silenced() == ["smoke.hall"]  # visible, not forgotten


def test_a_bare_acknowledge_covers_every_live_condition():
    gate = _gate(repeat_after={"safety": 0})
    for k in ("smoke.hall", "leak.basement"):
        gate.commit(k, now=T0)
    assert sorted(gate.live()) == ["leak.basement", "smoke.hall"]
    acked = gate.acknowledge(now=T0)
    assert sorted(acked) == ["leak.basement", "smoke.hall"]
    for k in ("smoke.hall", "leak.basement"):
        assert gate.evaluate(SAFETY, k, now=T0 + 1).reason == DENY_ACKNOWLEDGED


def test_acknowledging_one_condition_leaves_the_others_alone():
    gate = _gate(repeat_after={"safety": 0})
    gate.commit("smoke.hall", now=T0)
    gate.commit("leak.basement", now=T0)
    gate.acknowledge("smoke.hall", now=T0)
    assert gate.evaluate(SAFETY, "smoke.hall", now=T0 + 1).reason == DENY_ACKNOWLEDGED
    assert gate.evaluate(SAFETY, "leak.basement", now=T0 + 1).allowed


def test_clearing_lets_a_genuine_re_assertion_speak_at_once():
    """A second fire is not a repeat of the first."""
    gate = _gate(repeat_after={"safety": 3600})
    gate.commit("smoke.hall", now=T0)
    gate.acknowledge(now=T0)
    assert not gate.evaluate(SAFETY, "smoke.hall", now=T0 + 60).allowed

    gate.clear("smoke.hall")  # sensor went back to normal
    assert gate.live() == []
    assert gate.evaluate(SAFETY, "smoke.hall", now=T0 + 61).allowed


def test_acknowledging_nothing_is_harmless():
    gate = _gate()
    assert gate.acknowledge(now=T0) == []
    assert gate.evaluate(SAFETY, "smoke", now=T0).allowed


def test_clearing_is_the_only_thing_that_undoes_a_silence():
    gate = _gate(repeat_after={"safety": 0})
    gate.commit("smoke.hall", now=T0)
    gate.acknowledge(now=T0)
    assert gate.silenced() == ["smoke.hall"]
    gate.clear("smoke.hall")
    assert gate.silenced() == []
    assert gate.evaluate(SAFETY, "smoke.hall", now=T0 + 1).allowed
