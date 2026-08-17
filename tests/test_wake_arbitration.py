"""Parallel-hearing groundwork: node-measured wake evidence riding audio_start.

Co-audible nodes both wake on one utterance; a louder-wins arbiter needs a
comparable per-node measurement of THE WAKE PHRASE. Server-side session audio
can't provide it (a classic paused session's capture starts at the chime, after
the phrase is gone — measured live 2026-08-14), so each node measures its
pre-roll at wake time and sends it in audio_start. Levels ride in dB, and the
phrase-over-floor MARGIN is the load-bearing one: absolute level wanders with a
device's AGC state (the same clip read RMS 168 and ~1330 on the same M1A
minutes apart), but gain moves the phrase and the floor together.
"""

from __future__ import annotations

import json

import numpy as np

from kenzy import protocol
from kenzy.server.server import NodeSession, TranscribingServer


class _WS:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, data):  # noqa: ANN001
        self.sent.append(data)


def test_audio_start_carries_wake_evidence():
    msg, sid = protocol.audio_start(
        "s1", "office", wake_db=-24.31, wake_margin_db=40.02, wake_score=0.8834
    )
    payload = json.loads(msg)
    assert payload["wake_db"] == -24.3
    assert payload["wake_margin_db"] == 40.0
    assert payload["wake_score"] == 0.883
    assert sid == "s1"
    # Absent by default — a triggered/legacy session claims no wake evidence.
    plain, _ = protocol.audio_start("s2", "office")
    assert "wake_db" not in plain and "wake_score" not in plain


def test_wake_phrase_levels_measure_phrase_and_floor():
    from kenzy.node.client import _wake_phrase_levels

    # 10 quiet frames (floor amplitude 20) + 4 loud frames (phrase amplitude
    # 2000) — the shape of a real pre-roll: phrase at the back of the window.
    frames = [np.full(1280, 20, dtype=np.int16)] * 10 + [
        np.full(1280, 2000, dtype=np.int16)
    ] * 4
    db, margin = _wake_phrase_levels(frames)
    assert db == round(20 * np.log10(2000 / 32768), 1)  # ≈ -24.3 dBFS
    assert margin == 40.0  # 20·log10(2000/20)

    # Gain invariance — the AGC lesson: double every frame, margin holds.
    doubled = [(f * 2).astype(np.int16) for f in frames]
    db2, margin2 = _wake_phrase_levels(doubled)
    assert db2 > db
    assert margin2 == margin

    assert _wake_phrase_levels([]) == (0.0, 0.0)
    assert _wake_phrase_levels([np.zeros(0, dtype=np.int16)]) == (0.0, 0.0)


async def test_server_stashes_and_logs_wake_evidence(tmp_path, monkeypatch, caplog):
    import logging

    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    s = TranscribingServer({})
    sess = NodeSession(ws=_WS(), node_id="n1", room_id="office")
    s._nodes["n1"] = sess

    with caplog.at_level(logging.INFO, logger="kenzy.server.server"):
        await s._handle_control(
            sess,
            {"type": protocol.MSG_AUDIO_START, "session_id": "abc12345",
             "wake_db": -24.3, "wake_margin_db": 40.0, "wake_score": 0.883},
        )
    assert sess.wake_db == -24.3
    assert sess.wake_margin_db == 40.0
    assert sess.wake_score == 0.883
    assert any(
        "wake -24.3 dBFS, +40.0 dB over floor, score 0.883" in r.message
        for r in caplog.records
    )

    # A following session WITHOUT evidence clears the stash — stale numbers
    # must never describe a session they weren't measured for.
    await s._handle_control(
        sess, {"type": protocol.MSG_AUDIO_START, "session_id": "def67890"}
    )
    assert sess.wake_db is None and sess.wake_score is None

    # Garbage on the wire degrades to None, never an exception.
    await s._handle_control(
        sess,
        {"type": protocol.MSG_AUDIO_START, "session_id": "ghi000",
         "wake_db": "loud", "wake_score": []},
    )
    assert sess.wake_db is None and sess.wake_score is None


# ---------------------------------------------------------------------------
# The arbiter: wake_pending → window → stop the losers inside the gate window.
# ---------------------------------------------------------------------------


def _wp(sid: str, db: float, margin: float = 30.0, score: float = 0.8) -> dict:
    return {"type": protocol.MSG_WAKE_PENDING, "session_id": sid, "model": "hey_ken_zee",
            "score": score, "wake_db": db, "wake_margin_db": margin}


def _grouped_server(tmp_path, monkeypatch, groups: dict[str, str | None]):
    """A server with one NodeSession per entry; value = audio_group (None = unset)."""
    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    nodes_dir = tmp_path / "configs" / "nodes"
    nodes_dir.mkdir(parents=True, exist_ok=True)
    s = TranscribingServer({})
    for node_id, group in groups.items():
        s._nodes[node_id] = NodeSession(ws=_WS(), node_id=node_id, room_id="office")
        if group is not None:
            (nodes_dir / f"{node_id}.yaml").write_text(f"audio_group: {group}\n")
    return s


def _stops(sess: NodeSession) -> int:
    return sum(1 for m in sess.ws.sent if json.loads(m).get("type") == protocol.MSG_STOP)


async def test_arbitration_stops_all_but_the_loudest(tmp_path, monkeypatch):
    import asyncio

    s = _grouped_server(tmp_path, monkeypatch, {"near": "loft", "far": "loft"})
    await s._handle_control(s._nodes["near"], _wp("sid-near", -21.0))
    await s._handle_control(s._nodes["far"], _wp("sid-far", -28.0))
    await asyncio.sleep(0.35)  # window (0.25 s) closes
    assert _stops(s._nodes["far"]) == 1
    assert _stops(s._nodes["near"]) == 0
    assert s._arb_is_loser("sid-far") and not s._arb_is_loser("sid-near")

    # The loser's audio_start racing the stop is refused: no session opens,
    # the stop is re-sent, and the frames that follow are dropped.
    await s._handle_control(
        s._nodes["far"], {"type": protocol.MSG_AUDIO_START, "session_id": "sid-far"}
    )
    assert s._nodes["far"].streaming is False
    assert _stops(s._nodes["far"]) == 2


async def test_ungrouped_and_solo_wakes_never_arbitrate(tmp_path, monkeypatch):
    import asyncio

    # Two co-audible nodes WITHOUT audio_group: today's behavior, untouched.
    s = _grouped_server(tmp_path, monkeypatch, {"a": None, "b": None})
    await s._handle_control(s._nodes["a"], _wp("sa", -21.0))
    await s._handle_control(s._nodes["b"], _wp("sb", -28.0))
    await asyncio.sleep(0.35)
    assert _stops(s._nodes["a"]) == 0 and _stops(s._nodes["b"]) == 0

    # A grouped node that woke ALONE proceeds untouched — arbitration only
    # exists when there is someone to arbitrate against.
    s2 = _grouped_server(tmp_path, monkeypatch, {"solo": "loft"})
    await s2._handle_control(s2._nodes["solo"], _wp("s1", -25.0))
    await asyncio.sleep(0.35)
    assert _stops(s2._nodes["solo"]) == 0


async def test_loser_capture_is_never_transcribed(tmp_path, monkeypatch):
    import time as _time

    s = _grouped_server(tmp_path, monkeypatch, {"far": "loft"})
    sess = s._nodes["far"]
    sess.session_id = "sid-far"
    s._arb_losers["sid-far"] = _time.monotonic() + 5.0
    s._buffers["far"] = bytearray(b"\x00\x01" * 4000)

    ran: list[str] = []

    async def _fake_transcribe(*a, **k):  # noqa: ANN002, ANN003
        ran.append("pipeline")

    monkeypatch.setattr(s, "_transcribe", _fake_transcribe)
    await s.on_session_end(sess, "server_stop")
    assert ran == []  # the winner answers; the loser's capture dies here


async def test_loser_score_tail_rewake_is_suppressed(tmp_path, monkeypatch):
    """The live 2026-08-15 failure: openwakeword's score tail re-fired a "wake"
    on the loser 6 ms after its stop, and the re-wake — a solo candidate with a
    silent pre-roll — chimed and answered anyway. A wake_pending from a node
    stopped this recently is the tail, not a new utterance."""
    import asyncio

    s = _grouped_server(tmp_path, monkeypatch, {"near": "loft", "far": "loft"})
    await s._handle_control(s._nodes["near"], _wp("sid-near", -21.0))
    await s._handle_control(s._nodes["far"], _wp("sid-far", -28.0))
    await asyncio.sleep(0.35)
    assert _stops(s._nodes["far"]) == 1

    # The tail re-wake: stopped immediately, marked a loser, no new window.
    await s._handle_control(s._nodes["far"], _wp("sid-tail", -90.3, margin=0.0))
    assert _stops(s._nodes["far"]) == 2
    assert s._arb_is_loser("sid-tail")
    assert "loft" not in s._arb_window  # no solo window was opened for it

    # After BOTH guards expire (the per-node re-wake guard and the group dead
    # zone), the same node arbitrates normally again.
    import time as _time

    s._arb_recent["far"] = _time.monotonic() - 1.0
    s._arb_deadzone["loft"] = (_time.monotonic() - 1.0, "near")
    await s._handle_control(s._nodes["far"], _wp("sid-later", -25.0))
    assert not s._arb_is_loser("sid-later")
    await asyncio.sleep(0.35)  # solo candidate → proceeds, no stop
    assert _stops(s._nodes["far"]) == 2


async def test_deadzone_ignores_stragglers(tmp_path, monkeypatch):
    """One utterance gets ONE second of budget from its first wake: the 250 ms
    window arbitrates; a wake landing in the remaining 750 ms is the same
    phrase heard late (a slow device or model), not a new contender — by then
    the winner has proceeded and can't be un-answered. Applies after solo AND
    contested windows (the straggler is exactly what made a window solo)."""
    import asyncio

    # Solo window → straggler in the dead zone → actively stopped, no window.
    s = _grouped_server(tmp_path, monkeypatch, {"fast": "loft", "slow": "loft"})
    await s._handle_control(s._nodes["fast"], _wp("sid-fast", -22.0))
    await asyncio.sleep(0.35)  # window closes solo; dead zone runs to t=1.0 s
    assert _stops(s._nodes["fast"]) == 0
    await s._handle_control(s._nodes["slow"], _wp("sid-slow", -20.0))  # louder, but late
    assert _stops(s._nodes["slow"]) == 1
    assert s._arb_is_loser("sid-slow")
    assert "loft" not in s._arb_window  # no solo window opened for it

    # After the budget expires, the same node is a fresh contender again.
    await asyncio.sleep(0.8)  # past first-wake + 1.0 s
    await s._handle_control(s._nodes["slow"], _wp("sid-new", -25.0))
    await asyncio.sleep(0.35)
    assert _stops(s._nodes["slow"]) == 1  # solo winner this time — no new stop

    # Contested window → a THIRD node straggling in is ignored the same way.
    s2 = _grouped_server(
        tmp_path, monkeypatch, {"n1": "den", "n2": "den", "n3": "den"}
    )
    await s2._handle_control(s2._nodes["n1"], _wp("s1", -21.0))
    await s2._handle_control(s2._nodes["n2"], _wp("s2", -28.0))
    await asyncio.sleep(0.35)  # n1 wins, n2 stopped
    await s2._handle_control(s2._nodes["n3"], _wp("s3", -19.0))
    assert _stops(s2._nodes["n3"]) == 1
    assert s2._arb_is_loser("s3")


async def test_arbitration_timing_is_configurable_and_clamped(tmp_path, monkeypatch):
    """window/deadzone are operator-tunable (slow-waking hardware widens the
    window; a short custom wake phrase shortens the dead zone) with clamps: the
    window stays inside sane bounds and the dead zone can never be shorter
    than the window it contains. The guard TTLs stay code constants — race
    mechanics, not tuning."""
    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    s = TranscribingServer({"arbitration": {"window_ms": 100, "deadzone_ms": 600}})
    assert s._arb_window_s == 0.1 and s._arb_deadzone_s == 0.6

    s2 = TranscribingServer({"arbitration": {"window_ms": 9999, "deadzone_ms": 10}})
    assert s2._arb_window_s == 2.0
    assert s2._arb_deadzone_s == 2.0  # floored at the window

    s3 = TranscribingServer({})
    assert s3._arb_window_s == 0.25 and s3._arb_deadzone_s == 1.0
