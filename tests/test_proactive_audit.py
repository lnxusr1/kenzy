"""The proactive audit trail (5.0.6).

"Why didn't she say anything?" is as important as "why did she just say that?",
and only one of those is answerable from a log that keeps successes only. This
record therefore keeps the refusals — and it lives on the server, so it survives
`dashboard.logs: false`.
"""

from __future__ import annotations

from kenzy.server.proactive import ProactiveGate
from kenzy.server.safety import SafetyWatcher
from kenzy.server.server import TranscribingServer

MAP = {
    "binary_sensor.hall_smoke": {
        "room": "hallway",
        "room_name": "Hallway",
        "hazard": "smoke",
        "asserted": "on",
    }
}


def _server(tmp_path, monkeypatch, **gatecfg) -> TranscribingServer:
    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    s = TranscribingServer({})
    cfg = {"enabled": True, "safety": {"enabled": True, "repeat_after": 300}}
    cfg.update(gatecfg)
    gate = ProactiveGate.from_config(cfg)
    watcher = SafetyWatcher(gate, on_decision=s._record_proactive)
    watcher.set_map(MAP)
    s._proactive, s._safety = gate, watcher
    return s


def test_a_spoken_alert_is_recorded(tmp_path, monkeypatch):
    s = _server(tmp_path, monkeypatch)
    s._safety.consider("binary_sensor.hall_smoke", "on")
    (rec,) = s.proactive_log()
    assert rec["allowed"] is True
    assert rec["key"] == "binary_sensor.hall_smoke"
    assert rec["text"] == "There's smoke in the hallway."
    assert rec["ts"] > 0


def test_a_refusal_is_recorded_with_its_reason(tmp_path, monkeypatch):
    """The whole point: silence is explainable after the fact."""
    s = _server(tmp_path, monkeypatch, **{"safety": {"enabled": False}})
    s._safety.consider("binary_sensor.hall_smoke", "on")
    (rec,) = s.proactive_log()
    assert rec["allowed"] is False
    assert rec["reason"] == "category not enabled"
    # It still records WHAT she would have said, so you can see what you missed.
    assert rec["text"] == "There's smoke in the hallway."


def test_suppressed_repeats_are_explainable(tmp_path, monkeypatch):
    s = _server(tmp_path, monkeypatch)
    s._safety.consider("binary_sensor.hall_smoke", "on")
    s._safety.spoken("binary_sensor.hall_smoke")
    s._safety.consider("binary_sensor.hall_smoke", "on")
    newest, older = s.proactive_log()
    assert older["allowed"] is True
    assert newest["allowed"] is False and newest["reason"] == "already announced recently"


def test_the_log_is_newest_first_and_bounded(tmp_path, monkeypatch):
    s = _server(tmp_path, monkeypatch, **{"safety": {"enabled": False}})
    for _ in range(250):
        s._safety.consider("binary_sensor.hall_smoke", "on")
    log = s.proactive_log()
    assert len(log) == 200  # bounded ring, like the job history
    assert log[0]["ts"] >= log[-1]["ts"]  # newest first


def test_state_surfaces_the_posture(tmp_path, monkeypatch):
    s = _server(tmp_path, monkeypatch)
    st = s.proactive_state()
    assert st == {
        "available": True,
        "enabled": True,
        "categories": ["safety"],
        "silenced": [],
        "watching": 1,
    }


def test_state_surfaces_a_voice_disabled_feature(tmp_path, monkeypatch):
    """The one failure mode worth guarding: off for months, nobody aware."""
    s = _server(tmp_path, monkeypatch)
    s._proactive.enabled = False
    assert s.proactive_state()["enabled"] is False


def test_state_surfaces_silenced_alerts(tmp_path, monkeypatch):
    s = _server(tmp_path, monkeypatch)
    s._safety.consider("binary_sensor.hall_smoke", "on")
    s._safety.spoken("binary_sensor.hall_smoke")
    s._proactive.acknowledge()
    assert s.proactive_state()["silenced"] == ["binary_sensor.hall_smoke"]


def test_no_spine_reports_unavailable_rather_than_pretending(tmp_path, monkeypatch):
    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    s = TranscribingServer({})
    assert s.proactive_state() == {"available": False}
    assert s.proactive_log() == []


def test_a_broken_recorder_never_breaks_the_alert(tmp_path, monkeypatch):
    """The audit trail is important, but not more important than the fire."""
    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    gate = ProactiveGate.from_config({"enabled": True, "safety": {"enabled": True}})

    def _boom(key, decision, text):  # noqa: ANN001
        raise RuntimeError("disk full")

    w = SafetyWatcher(gate, on_decision=_boom)
    w.set_map(MAP)
    assert w.consider("binary_sensor.hall_smoke", "on") is not None


# --- the test trigger --------------------------------------------------------


async def test_the_test_alert_goes_through_the_real_gate(tmp_path, monkeypatch):
    """A test that bypassed the gate would verify the half nobody doubted, and
    report success on a house that would stay silent in a fire."""
    s = _server(tmp_path, monkeypatch, **{"safety": {"enabled": False}})
    res = await s.test_proactive_alert()
    assert res["ok"] is False
    assert res["reason"] == "category not enabled"
    # ...and it lands in the audit trail like any other refusal.
    assert s.proactive_log()[0]["key"] == "kenzy.test_alert"


async def test_the_test_alert_reports_when_nothing_could_play_it(tmp_path, monkeypatch):
    """The gate said yes and no room made a sound. Saying "ok" would be the
    worst possible answer from a test whose job is proving the house can shout."""
    s = _server(tmp_path, monkeypatch)  # safety on, but no nodes connected
    res = await s.test_proactive_alert()
    assert res["ok"] is False
    assert res["reason"] == "no room could play it"


async def test_a_successful_test_does_not_block_the_next_one(tmp_path, monkeypatch):
    """It clears its own key — otherwise the second test is refused as a repeat."""
    s = _server(tmp_path, monkeypatch)
    spoke = []

    async def _fake_speak(a):  # noqa: ANN001
        spoke.append(a.text)
        return True

    monkeypatch.setattr(s, "_speak_safety", _fake_speak)
    assert (await s.test_proactive_alert())["ok"] is True
    assert (await s.test_proactive_alert())["ok"] is True
    assert len(spoke) == 2


async def test_no_gate_means_the_test_says_so(tmp_path, monkeypatch):
    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    s = TranscribingServer({})
    res = await s.test_proactive_alert()
    assert res["ok"] is False and "not available" in res["reason"]
