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
