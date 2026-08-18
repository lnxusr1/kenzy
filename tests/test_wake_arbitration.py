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


def test_supports_arbitration_version_gate():
    """Field report 2026-08-17: a 5-node fleet upgraded its server to 5.1.1 but
    two nodes kept older code — they never sent wake_pending, were invisible to
    arbitration, and one answered a Whisper hallucination of another's TTS
    bleed-through. The failure is silent by design (protocol compat), so the
    VERSION GATE must be loud and tolerant: anything unparseable reads as too
    old, and leading digits only ("1rc1" is 1, not 11)."""
    from kenzy.server.server import _supports_arbitration

    assert _supports_arbitration("5.1.1") is True
    assert _supports_arbitration("5.1.2.dev0") is True
    assert _supports_arbitration("5.2.0") is True
    assert _supports_arbitration("6.0") is True
    assert _supports_arbitration("5.1.1rc1") is True  # leading digits: 5.1.1
    assert _supports_arbitration("5.1.0") is False
    assert _supports_arbitration("5.0.8") is False
    assert _supports_arbitration("5.1") is False  # shorter prefix < (5,1,1)
    assert _supports_arbitration(None) is False
    assert _supports_arbitration("") is False
    assert _supports_arbitration("garbage") is False


async def test_unannounced_grouped_session_is_named_loudly(tmp_path, monkeypatch, caplog):
    """The mixed-fleet signature (field report 2026-08-17): a grouped node that
    opens a session with NO wake evidence while its group is mid-arbitration
    never announced — it can't be stood down and will answer alongside the
    winner. The server can't fix that safely (it can't know how well the silent
    node heard), but it must NAME it, or the operator sees a 3-way collision
    with one node mysteriously 'skipping' arbitration."""
    import asyncio
    import logging

    s = _grouped_server(tmp_path, monkeypatch, {"new": "house", "old": "house"})
    await s._handle_control(s._nodes["new"], _wp("sid-new", -25.0))  # window opens
    with caplog.at_level(logging.WARNING, logger="kenzy.server.server"):
        await s._handle_control(
            s._nodes["old"],
            {"type": protocol.MSG_AUDIO_START, "session_id": "sid-old"},  # no evidence
        )
    assert any("UNANNOUNCED" in r.message for r in caplog.records)
    # The unannounced session itself is NOT stopped — a duplicate answer beats
    # standing down a node whose hearing we know nothing about.
    assert _stops(s._nodes["old"]) == 0
    await asyncio.sleep(0.35)  # let the window close

    # Outside any arbitration activity, an evidence-less session from a grouped
    # node is ordinary (triggers, follow-ups) — no warning.
    await asyncio.sleep(0.8)  # past the dead zone
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="kenzy.server.server"):
        await s._handle_control(
            s._nodes["old"],
            {"type": protocol.MSG_AUDIO_START, "session_id": "sid-later"},
        )
    assert not any("UNANNOUNCED" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Layer 1 stateful groups: the engagement record + the one-virtual-node rule.
# ---------------------------------------------------------------------------


async def test_group_is_one_virtual_node(tmp_path, monkeypatch):
    """A wake heard by ANY member ends the group's current conversation: node A
    is mid-exchange (streaming + pipeline in flight) when node B's wake wins a
    window — A is stopped, its pipeline dies, and B owns the new engagement."""
    import asyncio

    s = _grouped_server(tmp_path, monkeypatch, {"a": "loft", "b": "loft"})
    sess_a = s._nodes["a"]
    sess_a.session_id = "sid-a-old"
    await s.on_session_start(sess_a)  # A claims the engagement (capturing)
    assert s._engagements["loft"].owner == "a"
    sess_a.streaming = True
    s._stt_tasks["a"] = asyncio.create_task(asyncio.sleep(30))

    await s._handle_control(s._nodes["b"], _wp("sid-b", -22.0))
    await asyncio.sleep(0.35)  # window closes (solo B) → group cancel fires
    await asyncio.sleep(0)  # let the cancellation propagate
    assert _stops(s._nodes["a"]) == 1  # A's conversation stopped
    assert _stops(s._nodes["b"]) == 0  # the new wake is never stopped
    assert "a" not in s._stt_tasks  # A's pipeline cancelled
    assert "loft" not in s._engagements  # old engagement gone

    await s._handle_control(
        s._nodes["b"], {"type": protocol.MSG_AUDIO_START, "session_id": "sid-b"}
    )
    assert s._engagements["loft"].owner == "b"  # B owns the new conversation


async def test_candidates_spared_but_their_old_pipelines_die(tmp_path, monkeypatch):
    """A node that hears the new wake is a candidate — its NEW gate must never
    be stopped by the group cancel (only by losing arbitration) — but its OLD
    pipeline belongs to the conversation being ended and dies with it."""
    import asyncio

    s = _grouped_server(tmp_path, monkeypatch, {"a": "loft", "b": "loft"})
    s._stt_tasks["a"] = asyncio.create_task(asyncio.sleep(30))
    old_task = s._stt_tasks["a"]

    # A hears the new wake LOUDER (it wins); B also hears it and loses.
    await s._handle_control(s._nodes["a"], _wp("sid-a-new", -20.0))
    await s._handle_control(s._nodes["b"], _wp("sid-b-new", -28.0))
    await asyncio.sleep(0.35)
    await asyncio.sleep(0)
    assert _stops(s._nodes["a"]) == 0  # winner: no stop of any kind
    assert _stops(s._nodes["b"]) == 1  # loser: exactly the arbitration stop
    assert old_task.cancelled()  # but A's previous reply is dead


async def test_wake_during_speaking_phase_logs_bleed_suspect(
    tmp_path, monkeypatch, caplog
):
    """The accepted risk stays visible: a wake from a non-owner while the
    group's reply is playing is possible TTS bleed — logged, then processed
    normally (it cancels the conversation by design)."""
    import logging
    import time as _time

    from kenzy.server.server import GroupEngagement

    s = _grouped_server(tmp_path, monkeypatch, {"a": "loft", "b": "loft"})
    s._engagements["loft"] = GroupEngagement(
        "loft", "a", "sid-a", "speaking", _time.monotonic()
    )
    with caplog.at_level(logging.INFO, logger="kenzy.server.server"):
        await s._handle_control(s._nodes["b"], _wp("sid-b", -25.0))
    assert any("possible TTS bleed" in r.message for r in caplog.records)


async def test_engagement_lifecycle(tmp_path, monkeypatch):
    """capturing → thinking → reply-window → cleared; and an empty capture
    clears rather than leaving a stale claim."""
    import asyncio

    s = _grouped_server(tmp_path, monkeypatch, {"a": "loft"})
    sess = s._nodes["a"]
    sess.session_id = "sid-1"

    async def _fake_transcribe(*args, **kwargs):  # noqa: ANN002, ANN003
        await asyncio.sleep(0.01)

    monkeypatch.setattr(s, "_transcribe", _fake_transcribe)

    await s.on_session_start(sess)
    assert s._engagements["loft"].phase == "capturing"
    s._buffers["a"] = bytearray(b"\x00\x01" * 2000)
    await s.on_session_end(sess, "silence")
    assert s._engagements["loft"].phase == "thinking"

    s._followup_turns["a"] = 0
    s._engagement_update("a", None, "reply-window")
    assert s._engagements["loft"].phase == "reply-window"
    s._end_followup_dialog("a")
    assert "loft" not in s._engagements  # exchange over

    # Empty capture: claim, then nothing arrives — the engagement must not
    # linger as a stale conversation.
    await s.on_session_start(sess)
    assert s._engagements["loft"].phase == "capturing"
    await s.on_session_end(sess, "no_speech")  # buffer was reset by start
    assert "loft" not in s._engagements


async def test_engagement_survives_playback_tail(tmp_path, monkeypatch):
    """Measured live 2026-08-18: clearing the engagement at reply DISPATCH left
    a ~2 s deaf window while the audio still played at the speaker — a wake
    heard only elsewhere in the group couldn't stop the tail. The engagement
    now holds `speaking` until the node's tts_done (playback truly finished);
    nodes too old to send tts_done keep the old clear-at-dispatch behavior."""
    import time as _time

    from kenzy.server.server import GroupEngagement

    s = _grouped_server(tmp_path, monkeypatch, {"a": "loft"})
    s._nodes["a"].kenzy_version = "5.1.3"
    s._engagements["loft"] = GroupEngagement(
        "loft", "a", "sid-a", "speaking", _time.monotonic()
    )
    s._end_followup_dialog("a")  # exchange over — but audio still playing
    assert s._engagements["loft"].phase == "speaking"  # held for the tail

    await s._handle_control(
        s._nodes["a"], {"type": protocol.MSG_TTS_DONE, "session_id": "sid-a"}
    )
    assert "loft" not in s._engagements  # cleared when playback truly ended

    # A held floor is NOT cleared by tts_done — the conversation is live.
    s._engagements["loft"] = GroupEngagement(
        "loft", "a", "sid-a", "reply-window", _time.monotonic()
    )
    await s._handle_control(
        s._nodes["a"], {"type": protocol.MSG_TTS_DONE, "session_id": "sid-a"}
    )
    assert s._engagements["loft"].phase == "reply-window"

    # An old node (no tts_done) clears at dispatch — never sticks in speaking.
    s._nodes["a"].kenzy_version = "5.1.2"
    s._engagements["loft"] = GroupEngagement(
        "loft", "a", "sid-a", "speaking", _time.monotonic()
    )
    s._end_followup_dialog("a")
    assert "loft" not in s._engagements


async def test_disconnect_clears_engagement(tmp_path, monkeypatch):
    import time as _time

    from kenzy.server.server import GroupEngagement

    s = _grouped_server(tmp_path, monkeypatch, {"a": "loft"})
    s._engagements["loft"] = GroupEngagement(
        "loft", "a", "sid-a", "speaking", _time.monotonic()
    )
    s._engagement_clear("a", "node disconnected")  # the disconnect path's call
    assert "loft" not in s._engagements


async def test_fast_path_engagement_covers_delivery(tmp_path, monkeypatch):
    """Measured live 2026-08-18 (second smoke): the FAST path decides the floor
    when the reply is computed — 7–10 s before its audio finishes playing — and
    the engagement died in `thinking`, leaving the whole delivery uncovered. A
    still-running pipeline now holds the engagement through the gap; dispatch
    advances it to `speaking`; the reply's tts_done (matching the capture sid —
    cues ride fresh sids and must not end it) closes it."""
    import asyncio
    import time as _time

    from kenzy.server.server import GroupEngagement

    s = _grouped_server(tmp_path, monkeypatch, {"a": "loft"})
    s._nodes["a"].kenzy_version = "5.1.3"
    s._engagements["loft"] = GroupEngagement(
        "loft", "a", "sid-cap", "thinking", _time.monotonic()
    )
    s._stt_tasks["a"] = asyncio.create_task(asyncio.sleep(30))  # pipeline in flight
    try:
        s._end_followup_dialog("a")  # fast path: floor decided pre-dispatch
        assert s._engagements["loft"].phase == "thinking"  # held, not cleared

        await s.send_tts_start("a", "sid-cap", stream=True)  # dispatch begins
        assert s._engagements["loft"].phase == "speaking"

        # The processing cue finishing (fresh sid) must not end the engagement.
        await s._handle_control(
            s._nodes["a"], {"type": protocol.MSG_TTS_DONE, "session_id": "sid-cue"}
        )
        assert s._engagements["loft"].phase == "speaking"

        # The reply's own completion (capture sid) ends it.
        await s._handle_control(
            s._nodes["a"], {"type": protocol.MSG_TTS_DONE, "session_id": "sid-cap"}
        )
        assert "loft" not in s._engagements
    finally:
        s._stt_tasks.pop("a", None).cancel()


async def test_operator_ignore_audio(tmp_path, monkeypatch):
    """The test/ops input-mute (founder, 2026-08-18): the server disregards a
    node's audio so live tests can force who-hears-what without config writes
    or restarts. An ignored node's wake never enters arbitration (and never
    cancels the group's conversation); its sessions are refused like a loser's.
    Runtime-only: the flag lives on the connection and dies with it."""
    import asyncio

    s = _grouped_server(tmp_path, monkeypatch, {"near": "loft", "far": "loft"})
    assert s.set_node_ignore_audio("near", True) is True
    assert s.set_node_ignore_audio("ghost", True) is False  # unknown node

    # Ignored node's wake: no candidacy — the OTHER node wins solo, unstopped.
    await s._handle_control(s._nodes["near"], _wp("sid-near", -20.0))  # louder!
    await s._handle_control(s._nodes["far"], _wp("sid-far", -30.0))
    await asyncio.sleep(0.35)
    assert _stops(s._nodes["far"]) == 0  # solo winner — nobody to lose to
    assert "loft" not in s._arb_window

    # Ignored node's session: refused, stop sent, nothing opens.
    await s._handle_control(
        s._nodes["near"], {"type": protocol.MSG_AUDIO_START, "session_id": "sid-near"}
    )
    assert s._nodes["near"].streaming is False
    assert _stops(s._nodes["near"]) == 1

    # Un-ignore: back to normal — its wake arbitrates again.
    assert s.set_node_ignore_audio("near", False) is True
    await asyncio.sleep(0.8)  # clear the dead zone from the solo win above
    await s._handle_control(s._nodes["near"], _wp("sid-n2", -20.0))
    await s._handle_control(s._nodes["far"], _wp("sid-f2", -30.0))
    await asyncio.sleep(0.35)
    assert _stops(s._nodes["far"]) == 1  # far loses to the un-ignored near


async def test_force_wake_sends_frame(tmp_path, monkeypatch):
    """force_wake (founder, 2026-08-18): scripted live tests need to make a
    specific node run its REAL wake path — evidence, announcement, gate — so
    collisions can be forced programmatically instead of by staging acoustics.
    (`trigger` deliberately bypasses the wake machinery; this exercises it.)"""
    s = _grouped_server(tmp_path, monkeypatch, {"a": "loft"})
    assert await s.force_wake_node("a") is True
    frames = [json.loads(m) for m in s._nodes["a"].ws.sent]
    assert any(f.get("type") == protocol.MSG_FORCE_WAKE for f in frames)
    assert await s.force_wake_node("ghost") is False


async def test_claim_over_live_conversation_cancels_it(tmp_path, monkeypatch):
    """Found live 2026-08-18 by FORCING the ordering with the test tools: a
    one-breath confirm sends audio_start ~200 ms after the wake — BEFORE the
    250 ms arbitration window closes — and the new claim used to silently
    destroy the old engagement, leaving the old owner's still-playing answer
    unstoppable. Now the claim itself cancels: whoever claims, cancels."""
    import asyncio
    import time as _time

    from kenzy.server.server import GroupEngagement

    s = _grouped_server(tmp_path, monkeypatch, {"a": "loft", "b": "loft"})
    # B is mid-answer: engagement speaking, pipeline still alive.
    s._engagements["loft"] = GroupEngagement(
        "loft", "b", "sid-b", "speaking", _time.monotonic()
    )
    s._stt_tasks["b"] = asyncio.create_task(asyncio.sleep(30))

    # A's audio_start arrives (early one-breath confirm) — no window has closed.
    sess_a = s._nodes["a"]
    sess_a.session_id = "sid-a"
    await s.on_session_start(sess_a)
    await asyncio.sleep(0)
    assert _stops(s._nodes["b"]) == 1  # B's answer stopped BY THE CLAIM
    assert "b" not in s._stt_tasks  # B's pipeline dead
    assert s._engagements["loft"].owner == "a"  # A owns the group now
