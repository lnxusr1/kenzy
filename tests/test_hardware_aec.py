"""Stage 0 of conversational-flow: declare and honor the AEC assumption.

`hardware_aec: false` (per-node) ⇒ half-duplex room: wake hits are ignored
while the node emits audio; intercom is refused (both in-reply by the skill and
server-side as backstop); alarms refuse at set time and degrade to a single
timer-style delivery at fire time. Policy: absent beats broken.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from kenzy.llm import skills as sk
from kenzy.llm.builtin_skills import intercom as ic
from kenzy.server.server import _ALLOWED_OVERRIDE_KEYS, NodeSession, TranscribingServer


class _WS:
    def __init__(self) -> None:
        self.sent: list[Any] = []

    async def send(self, data: Any) -> None:
        self.sent.append(data)


def _srv(tmp_path, monkeypatch, node_defaults=None):
    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    s = TranscribingServer({"node_defaults": node_defaults or {}})
    s._nodes["quiet"] = NodeSession(ws=_WS(), node_id="quiet", room_id="office")
    s._nodes["deaf"] = NodeSession(ws=_WS(), node_id="deaf", room_id="garage")
    # Per-node override: the garage speaker has no AEC.
    s.write_node_override("deaf", {"hardware_aec": False})
    return s


def test_key_is_allowed_and_default_true(tmp_path, monkeypatch):
    assert "hardware_aec" in _ALLOWED_OVERRIDE_KEYS
    s = _srv(tmp_path, monkeypatch)
    assert s._node_aec("quiet") is True  # absent ⇒ true
    assert s._node_aec("deaf") is False
    assert s._no_aec_rooms() == ["garage"]


async def test_intercom_refused_when_either_end_lacks_aec(tmp_path, monkeypatch):
    s = _srv(tmp_path, monkeypatch)
    said: list[tuple[str, str]] = []

    async def fake_say(node_id, room, text):
        said.append((node_id, text))

    monkeypatch.setattr(s, "_say", fake_say)

    await s._action_connect_call("quiet", "office", "garage")  # AEC caller → non-AEC target
    assert "echo-cancelling" in said[-1][1] and "garage" in said[-1][1]
    assert s._nodes["quiet"].intercom_peer is None

    await s._action_connect_call("deaf", "garage", "office")  # non-AEC caller
    assert "echo-cancelling" in said[-1][1]
    assert s._nodes["deaf"].intercom_peer is None


async def test_alarm_fire_degrades_to_single_delivery(tmp_path, monkeypatch):
    s = _srv(tmp_path, monkeypatch)
    delivered: list[tuple[str, str, str]] = []

    async def fake_deliver(node_id, room, text, kind):
        delivered.append((node_id, kind, text))

    monkeypatch.setattr(s, "_deliver_schedule", fake_deliver)

    class _E:  # minimal scheduler entry
        kind = "alarm"
        at = "07:00"
        room = "garage"
        node_id = "deaf"
        label = ""
        speaker = ""

    monkeypatch.setattr(s, "_schedule_target", lambda e: "deaf")
    await s._fire_schedule(_E())
    await asyncio.sleep(0.05)
    # One timer-style delivery, no ring loop registered.
    assert delivered == [("deaf", "alarm", "It's 7:00 AM. This is your alarm.")]
    assert "deaf" not in s._ring_tasks


async def test_alarm_ring_loop_unchanged_for_aec_rooms(tmp_path, monkeypatch):
    s = _srv(tmp_path, monkeypatch)

    class _E:
        kind = "alarm"
        at = "07:00"
        room = "office"
        node_id = "quiet"
        label = ""
        speaker = ""

    monkeypatch.setattr(s, "_schedule_target", lambda e: "quiet")

    async def fake_ring(node_id, room, at):
        await asyncio.sleep(10)

    monkeypatch.setattr(s, "_ring_alarm", fake_ring)
    await s._fire_schedule(_E())
    assert "quiet" in s._ring_tasks
    s._ring_tasks.pop("quiet").cancel()


# ---------------------------------------------------------------------------
# Skill-side in-reply refusals (via the injected no_aec_rooms context)
# ---------------------------------------------------------------------------


@pytest.fixture
def req_ctx():
    sk.begin_actions()
    sk.begin_request(
        {
            "rooms": ["office", "garage"],
            "schedules": [],
            "room_id": "office",
            "no_aec_rooms": ["garage"],
        }
    )
    yield
    sk.take_actions()


async def test_connect_room_refuses_non_aec_target(req_ctx):
    from kenzy.llm import asking

    out = await ic.connect_room("garage")
    assert "echo-cancelling" in out
    assert sk.take_actions() == []  # no action queued
    # An AEC-clean target proceeds to the consent ask (parks on the question).
    sk.begin_request(
        {"channel": "voice", "room_id": "office", "rooms": ["office", "garage", "den"],
         "no_aec_rooms": ["garage"]}  # fmt: skip
    )
    outcome = await asking.run_askable(ic.connect_room("den"), kind="llm")
    assert not outcome.finished
    assert outcome.parked.channel.room == "den"
    assert outcome.parked.channel.announce == "Calling the den."
    await asking.cancel(outcome.parked.id)


async def test_connect_room_refuses_from_non_aec_source():
    sk.begin_actions()
    sk.begin_request(
        {"rooms": ["office", "garage"], "room_id": "garage", "no_aec_rooms": ["garage"]}
    )
    out = await ic.connect_room("office")
    assert "this room" in out and "echo-cancelling" in out
    assert sk.take_actions() == []


async def test_set_alarm_refuses_non_aec_room(req_ctx):
    from kenzy.llm.builtin_skills import schedule as sched

    out = await sched.set_alarm("07:00", room="garage")
    assert "can't run alarms" in out and "timer or a reminder" in out
    assert sk.take_actions() == []
    out = await sched.set_alarm("07:00")  # asking room (office) has AEC
    assert out.startswith("Scheduled: alarm")
    assert len(sk.take_actions()) == 1


async def test_fast_alarm_refuses_and_timer_still_works():
    from kenzy.llm.builtin_skills import schedule as sched

    sk.begin_actions()
    sk.begin_request({"rooms": ["garage"], "room_id": "garage", "no_aec_rooms": ["garage"]})
    r = await sched.fast_schedule("wake me up at 7 am", "garage", None)
    assert r.is_handled and "can't run alarms" in r.text
    assert sk.take_actions() == []

    sk.begin_actions()
    r = await sched.fast_schedule("set a timer for 10 minutes", "garage", None)
    assert r.is_handled and "timer" in r.text.lower()
    (action,) = sk.take_actions()
    assert action["kind"] == "timer"  # timers work everywhere


# ---------------------------------------------------------------------------
# Node: wake suppression while the player is active
# ---------------------------------------------------------------------------


async def _run_one_wake_frame(monkeypatch, *, aec: bool, playing: bool) -> list[str]:
    """Push one high-score wake frame through a fresh client's audio loop and
    return the sessions it began. Fresh client per call — a cancelled loop
    leaves an orphaned executor queue.get that could eat a shared queue's frame."""
    import numpy as np

    from kenzy.node.client import NodeClient

    client = NodeClient({"node_id": "n1", "hardware_aec": aec})

    class _Player:
        active = playing

        def play(self):
            pass

    client._player = _Player()  # type: ignore[assignment]
    began: list[str] = []

    async def fake_begin(sid):
        began.append(sid)

    monkeypatch.setattr(client, "_begin_streaming", fake_begin)

    class _OWW:
        def predict(self, frame):
            return {"hey_ken_zee": 0.99}

    client._oww = _OWW()
    client._raw_q.put(np.zeros(1280, dtype=np.int16))
    task = asyncio.create_task(client._audio_loop())
    await asyncio.sleep(0.4)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    return began


async def test_node_ignores_wake_during_playback_without_aec(monkeypatch):
    assert await _run_one_wake_frame(monkeypatch, aec=False, playing=True) == []


async def test_node_acts_on_wake_when_idle_without_aec(monkeypatch):
    assert len(await _run_one_wake_frame(monkeypatch, aec=False, playing=False)) == 1


async def test_node_acts_on_wake_during_playback_with_aec(monkeypatch):
    assert len(await _run_one_wake_frame(monkeypatch, aec=True, playing=True)) == 1
