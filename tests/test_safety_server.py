"""Tier A wired into the server (5.0.6).

The 5.0.5 alarm bug was a correct helper that nothing reachable ever called, so
these tests drive the PROTOCOL EVENTS a real node produces — never the private
acknowledgement helper.
"""

from __future__ import annotations

from kenzy.server.proactive import ProactiveGate
from kenzy.server.safety import SafetyWatcher
from kenzy.server.server import NodeSession, TranscribingServer


class _WS:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, data):  # noqa: ANN001
        self.sent.append(data)


MAP = {
    "binary_sensor.hall_smoke": {
        "room": "hallway",
        "room_name": "Hallway",
        "hazard": "smoke",
        "asserted": "on",
    }
}


def _server(tmp_path, monkeypatch, *, safety_on=True) -> TranscribingServer:
    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    s = TranscribingServer({})
    s._nodes["n-off"] = NodeSession(ws=_WS(), node_id="n-off", room_id="office")
    gate = ProactiveGate.from_config(
        {"enabled": True, "safety": {"enabled": safety_on, "repeat_after": 300}}
    )
    watcher = SafetyWatcher(gate)
    watcher.set_map(MAP)
    s._proactive = gate
    s._safety = watcher
    return s


async def test_speaking_to_kenzy_silences_a_live_alert(tmp_path, monkeypatch):
    """The 5.0.5 lesson: acknowledgement must be reachable from the event that
    a real node actually sends. A wake word over playing audio sends NO
    `wakeword` frame — only `audio_start` → on_session_start."""
    s = _server(tmp_path, monkeypatch)
    assert s._safety.consider("binary_sensor.hall_smoke", "on") is not None
    s._safety.spoken("binary_sensor.hall_smoke")

    await s.on_session_start(s._nodes["n-off"])  # the real path

    assert s._proactive.silenced() == ["binary_sensor.hall_smoke"]
    assert s._safety.consider("binary_sensor.hall_smoke", "on") is None


async def test_a_wake_word_mid_capture_also_silences(tmp_path, monkeypatch):
    """The other entry point — a wake word arriving while already streaming."""
    s = _server(tmp_path, monkeypatch)
    s._safety.consider("binary_sensor.hall_smoke", "on")
    s._safety.spoken("binary_sensor.hall_smoke")

    async def _noop(node_id):  # noqa: ANN001
        return True

    monkeypatch.setattr(s, "stop_node", _noop)
    await s.on_wakeword(s._nodes["n-off"], "hey_kenzy", 0.9)
    assert s._proactive.silenced() == ["binary_sensor.hall_smoke"]


async def test_silencing_lasts_until_the_sensor_cycles(tmp_path, monkeypatch):
    s = _server(tmp_path, monkeypatch)
    s._safety.consider("binary_sensor.hall_smoke", "on")
    s._safety.spoken("binary_sensor.hall_smoke")
    await s.on_session_start(s._nodes["n-off"])

    # A day later, still asserting, still silent.
    assert s._safety.consider("binary_sensor.hall_smoke", "on") is None
    # Off and on again is a NEW event.
    s._safety.consider("binary_sensor.hall_smoke", "off")
    assert s._safety.consider("binary_sensor.hall_smoke", "on") is not None


async def test_acknowledging_with_no_alerts_is_harmless(tmp_path, monkeypatch):
    s = _server(tmp_path, monkeypatch)
    await s.on_session_start(s._nodes["n-off"])
    assert s._proactive.silenced() == []


async def test_a_server_without_the_spine_ignores_acknowledgement(tmp_path, monkeypatch):
    """No HA ⇒ no gate. The hook must be a no-op, not an AttributeError on the
    speech hot path."""
    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    s = TranscribingServer({})
    s._nodes["n-off"] = NodeSession(ws=_WS(), node_id="n-off", room_id="office")
    await s.on_session_start(s._nodes["n-off"])  # must not raise


def test_raw_state_changes_are_ignored_when_safety_is_off(tmp_path, monkeypatch):
    s = _server(tmp_path, monkeypatch, safety_on=False)
    s._on_safety_state("binary_sensor.hall_smoke", "on")  # must not raise or schedule
    assert s._proactive.silenced() == []


async def test_silencing_needs_no_phrase_at_all(tmp_path, monkeypatch):
    """A bare "stop", "cancel", "nevermind" — or just the wake word with nothing
    after it — quiets a live alert until the sensor retriggers.

    Silencing hangs off the SESSION opening, before transcription or any intent
    matching, so it cannot depend on getting the words right. Someone woken at
    3am by a shrieking speaker should not have to remember a phrase, and the
    instant-stop path ("Hey Kenzy, stop.") never reaches a skill anyway.
    """
    s = _server(tmp_path, monkeypatch)
    s._safety.consider("binary_sensor.hall_smoke", "on")
    s._safety.spoken("binary_sensor.hall_smoke")

    # No utterance is transcribed here at all — just the session opening.
    await s.on_session_start(s._nodes["n-off"])

    assert s._proactive.silenced() == ["binary_sensor.hall_smoke"]
    assert s._safety.consider("binary_sensor.hall_smoke", "on") is None
    # ...and it is only silence, never a disable: future hazards still speak.
    assert s._proactive.enabled is True
    s._safety.consider("binary_sensor.hall_smoke", "off")  # cycles
    assert s._safety.consider("binary_sensor.hall_smoke", "on") is not None
