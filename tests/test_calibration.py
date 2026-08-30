"""Tests for the calibration telemetry primitive: tune_start/stop/sample, the node's
per-frame measurement emit, the server relay, and dashboard fan-out to subscribers."""

from __future__ import annotations

import asyncio
import json

import numpy as np

from kenzy import protocol
from kenzy.calibration import (
    percentile as _percentile,
)
from kenzy.calibration import (
    suggest_silence as _suggest_silence_rms,
)
from kenzy.calibration import (
    suggest_vad as _suggest_vad_threshold,
)
from kenzy.calibration import (
    suggest_wake as _suggest_wake_threshold,
)
from kenzy.node.client import NodeClient
from kenzy.server.dashboard import Dashboard, DashboardConfig
from kenzy.server.server import AudioServer, NodeSession


class _RecWS:
    """WebSocket stub that records sent frames."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, m):  # noqa: ANN001, ANN201
        self.sent.append(m)


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


def test_tune_protocol_roundtrip():
    assert json.loads(protocol.tune_start(15))["seconds"] == 15
    assert json.loads(protocol.tune_stop())["type"] == "tune_stop"
    s = json.loads(protocol.tune_sample(rms=12.5, wake=0.7, vad=0.3, seq=4))
    assert s["type"] == "tune_sample"
    assert (s["rms"], s["wake"], s["vad"], s["seq"], s["stopped"]) == (12.5, 0.7, 0.3, 4, False)
    assert json.loads(protocol.tune_sample(stopped=True))["stopped"] is True


# ---------------------------------------------------------------------------
# CLI suggestion heuristics (kenzy-node --calibrate); mirror the dashboard JS
# ---------------------------------------------------------------------------


def test_calibration_suggestions():
    assert _percentile([], 0.5) == 0.0
    assert _percentile([10, 20, 30, 40], 0.5) == 20

    # Silence is two-sided and VOICE-anchored (0.8 distance derate, /2 margin):
    # quiet floor ~10, speech ~700 → 0.4 × 700 = 280 — from the voice, not the
    # floor (ambient can rise after calibration; the floor only clamps).
    quiet = [10.0] * 90
    speech = [700.0] * 40
    assert _suggest_silence_rms(quiet, speech) == 280
    # No separation (speech barely above the floor) → no guess; keep previous.
    assert _suggest_silence_rms([100.0] * 90, [180.0] * 40) is None
    assert _suggest_silence_rms([], speech) is None
    assert _suggest_silence_rms(quiet, []) is None

    from kenzy.calibration import separation_verdict as _separation_verdict

    assert _separation_verdict(quiet, speech) == "good"
    assert _separation_verdict([100.0] * 90, [180.0] * 40) == "poor"
    assert _separation_verdict([100.0] * 90, [500.0] * 40) == "marginal"

    # Wake: a clear gap between ambient and utterance peak → a value inside it.
    w = _suggest_wake_threshold([0.02] * 90 + [0.9] * 10)
    assert w is not None and 0.02 < w < 0.9
    assert _suggest_wake_threshold([0.01] * 50) is None  # no utterance heard → no guess

    # VAD: two-phase — quiet floor + wake-phase speech, gate placed between.
    v = _suggest_vad_threshold([0.05] * 30, [0.05] * 50 + [0.95] * 40)
    assert v is not None and 0.0 <= v <= 0.9
    assert _suggest_vad_threshold([], [0.95] * 30) is None  # no quiet floor
    assert _suggest_vad_threshold([0.05] * 30, []) is None  # no speech phase


def test_vad_diagnostics_explains_every_outcome():
    from kenzy.calibration import vad_diagnostics

    # A phase with no samples — named, not a crash.
    assert "no VAD samples" in vad_diagnostics([], [])
    assert "no VAD samples" in vad_diagnostics([0.05] * 30, [])

    # A clean quiet floor + real speech spikes: diagnostics agrees with the
    # suggestion (both share the constants, so they can never disagree).
    quiet = [0.05] * 30
    speech = [0.05] * 50 + [0.95] * 40
    d = vad_diagnostics(quiet, speech)
    assert "OK: suggest" in d and str(_suggest_vad_threshold(quiet, speech)) in d

    # Speech never cleared the quiet floor → the exact reason, not silence.
    flat_speech = [0.05] * 100
    assert _suggest_vad_threshold(quiet, flat_speech) is None
    assert "gap<" in vad_diagnostics(quiet, flat_speech)

    # The quiet floor itself is voice-like → the other skip reason.
    voiced_floor = [0.7] * 30
    assert _suggest_vad_threshold(voiced_floor, speech) is None
    assert "voice-like" in vad_diagnostics(voiced_floor, speech)


# ---------------------------------------------------------------------------
# Node: measurement emit + window lifecycle
# ---------------------------------------------------------------------------


async def test_emit_tune_sample_sends_measurements():
    client = NodeClient({"node_id": "n1"})
    ws = _RecWS()
    client._ws = ws  # type: ignore[assignment]
    loop = asyncio.get_running_loop()
    client._tuning = True
    client._tune_deadline = loop.time() + 10  # not expired
    client._tune_vad = None  # vad score → 0 without a model
    client._tune_seq = 0

    flat = np.full(1280, 1000, dtype=np.int16)
    await client._emit_tune_sample(flat, {"hey_ken_zee": 0.62}, loop)

    assert len(ws.sent) == 1
    s = json.loads(ws.sent[0])
    assert s["type"] == "tune_sample"
    assert abs(s["rms"] - 1000.0) < 1.0  # RMS of a constant 1000 signal
    assert s["wake"] == 0.62  # max score
    assert s["vad"] == 0.0
    assert s["seq"] == 1
    assert client._tuning is True


async def test_emit_tune_sample_auto_stops_when_expired():
    client = NodeClient({"node_id": "n1"})
    ws = _RecWS()
    client._ws = ws  # type: ignore[assignment]
    loop = asyncio.get_running_loop()
    client._tuning = True
    client._tune_deadline = loop.time() - 1  # already expired
    client._tune_vad = None

    await client._emit_tune_sample(np.zeros(1280, dtype=np.int16), {"m": 0.1}, loop)

    assert client._tuning is False
    assert json.loads(ws.sent[-1])["stopped"] is True


def test_start_stop_tuning(monkeypatch):
    import openwakeword

    class _DummyVAD:
        def __init__(self) -> None:
            self.prediction_buffer = [0.0]

        def __call__(self, x):  # noqa: ANN001, ANN201
            self.prediction_buffer.append(0.9)

    monkeypatch.setattr(openwakeword, "VAD", _DummyVAD)

    async def run() -> None:
        client = NodeClient({"node_id": "n1"})
        client._start_tuning(20)
        assert client._tuning is True
        assert isinstance(client._tune_vad, _DummyVAD)
        assert client._vad_score(np.zeros(1280, dtype=np.int16)) == 0.9  # last buffer entry
        client._stop_tuning()
        assert client._tuning is False
        assert client._tune_vad is None

    asyncio.run(run())


async def test_tune_start_ignored_when_not_idle(monkeypatch):
    client = NodeClient({"node_id": "n1"})
    client._oww = object()  # pretend audio is up
    started: list[float] = []
    monkeypatch.setattr(client, "_start_tuning", lambda s: started.append(s))

    # Busy node: tune_start must be ignored.
    from kenzy.node.client import _STATE_STREAMING

    client._state = _STATE_STREAMING
    task = asyncio.create_task(client._cmd_loop())
    try:
        client._cmd_q.put_nowait({"type": protocol.MSG_TUNE_START, "seconds": 10})
        await asyncio.sleep(0.03)
        assert started == []  # ignored while streaming
        # Idle node: honored.
        from kenzy.node.client import _STATE_IDLE

        client._state = _STATE_IDLE
        client._cmd_q.put_nowait({"type": protocol.MSG_TUNE_START, "seconds": 10})
        await asyncio.sleep(0.03)
        assert started == [10.0]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


# ---------------------------------------------------------------------------
# Server: start/stop frames + sample relay to listeners
# ---------------------------------------------------------------------------


async def test_server_start_stop_tuning_sends_frames():
    srv = AudioServer({})
    ws = _RecWS()
    srv._nodes["k"] = NodeSession(ws=ws, node_id="k", room_id="kitchen")  # type: ignore[arg-type]
    assert await srv.start_node_tuning("k", 12) is True
    assert json.loads(ws.sent[-1]) == {"type": "tune_start", "seconds": 12.0}
    assert await srv.stop_node_tuning("k") is True
    assert json.loads(ws.sent[-1])["type"] == "tune_stop"
    assert await srv.start_node_tuning("ghost") is False  # not connected


async def test_tune_sample_notifies_listeners():
    srv = AudioServer({})
    session = NodeSession(ws=_RecWS(), node_id="k", room_id="kitchen")  # type: ignore[arg-type]
    srv._nodes["k"] = session
    seen: list[tuple[str, dict]] = []
    srv.add_tune_listener(lambda nid, sample: seen.append((nid, sample)))
    await srv._handle_control(
        session,
        {"type": protocol.MSG_TUNE_SAMPLE, "rms": 9.0, "wake": 0.4, "vad": 0.2, "seq": 3},
    )
    assert seen == [("k", {"rms": 9.0, "wake": 0.4, "vad": 0.2, "seq": 3, "stopped": False})]


# ---------------------------------------------------------------------------
# Dashboard: relay only to the subscribed client for the matching node
# ---------------------------------------------------------------------------


class _Cap:
    """Fake browser WS connection that records decoded messages it's sent."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, raw):  # noqa: ANN001, ANN201
        self.sent.append(json.loads(raw))


async def test_tune_start_stop_mutations(tmp_path, monkeypatch):
    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    (tmp_path / "configs").mkdir(parents=True)
    srv = AudioServer({})
    node_ws = _RecWS()
    srv._nodes["k"] = NodeSession(ws=node_ws, node_id="k", room_id="kitchen")  # type: ignore[arg-type]
    dash = Dashboard(srv, {}, DashboardConfig(enabled=True, controls=True))
    cap = _Cap()

    await dash._handle_ws_message(
        cap, json.dumps({"id": "1", "type": "tune_start", "node": "k", "seconds": 5})
    )
    assert cap.sent[-1] == {"type": "ack", "id": "1", "ok": True}
    assert json.loads(node_ws.sent[-1]) == {"type": "tune_start", "seconds": 5.0}
    assert dash._tune_subs.get(cap) == "k"  # subscription recorded

    # A relayed sample reaches this subscriber only.
    dash._on_tune_sample("k", {"rms": 1.0, "wake": 0.0, "vad": 0.0})
    await asyncio.sleep(0)
    assert any(m.get("type") == "tune" for m in cap.sent)

    await dash._handle_ws_message(cap, json.dumps({"id": "2", "type": "tune_stop", "node": "k"}))
    assert json.loads(node_ws.sent[-1])["type"] == "tune_stop"
    assert cap not in dash._tune_subs


async def test_tune_start_gated_by_controls(tmp_path, monkeypatch):
    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    srv = AudioServer({})
    node_ws = _RecWS()
    srv._nodes["k"] = NodeSession(ws=node_ws, node_id="k", room_id="kitchen")  # type: ignore[arg-type]
    dash = Dashboard(srv, {}, DashboardConfig(enabled=True, controls=False))
    cap = _Cap()
    await dash._handle_ws_message(cap, json.dumps({"id": "1", "type": "tune_start", "node": "k"}))
    assert cap.sent[-1]["ok"] is False
    assert node_ws.sent == []  # node was not asked to tune
    assert cap not in dash._tune_subs


async def test_dashboard_relays_only_to_subscriber():
    srv = AudioServer({})
    dash = Dashboard(srv, {}, DashboardConfig(controls=True))
    sub = _RecWS()
    other = _RecWS()
    dash._tune_subs = {sub: "k", other: "different"}  # type: ignore[dict-item]

    dash._on_tune_sample("k", {"rms": 1.0, "wake": 0.5, "vad": 0.1, "seq": 1, "stopped": False})
    await asyncio.sleep(0)  # let the relay task run

    assert len(sub.sent) == 1
    msg = json.loads(sub.sent[0])
    assert msg["type"] == "tune" and msg["node"] == "k"
    assert msg["sample"]["wake"] == 0.5
    assert other.sent == []  # node mismatch → not forwarded


# ---------------------------------------------------------------------------
# aec_verdict — the hardware-AEC probe judgment. Shipped untested, and the
# EMEET M1A promptly proved why that mattered: its AGC and playback-gated mic
# broke the probe's constant-gain assumption and produced a false "absent".
# ---------------------------------------------------------------------------


def _frames(value: float, n: int = 30) -> list[float]:
    return [value] * n


def test_aec_present_reads_true():
    # S330-shaped: residual during the beep sits at the quiet floor.
    from kenzy.calibration import aec_verdict

    assert aec_verdict(_frames(40), _frames(45)) is True


def test_aec_absent_reads_false_only_when_beep_loud():
    # Y02-shaped (a plain speaker/mic, no AEC): the co-located beep is heard
    # near clipping — thousands of RMS, nothing like ambient.
    from kenzy.calibration import aec_verdict

    assert aec_verdict(_frames(40), _frames(6000)) is False


def test_m1a_agc_residual_is_ambiguous_never_absent():
    """The 2026-08-14 regression, with the real numbers. The M1A's AGC lifts
    ambient alone to ~1400 RMS in a quiet room, and its quiet baseline can sit
    near zero (mic gated while nothing plays). The old relative-only bars read
    that as "no AEC" and switched a full-duplex device to half-duplex — which
    then ignored wake words during any playback. Elevated-but-not-beep-loud
    must be AMBIGUOUS: the flag stays as it was."""
    from kenzy.calibration import aec_verdict

    # AGC-lifted ambient during the probe vs an ordinary quiet floor…
    assert aec_verdict(_frames(60), _frames(1400)) is None
    # …and vs a gated-mic near-zero baseline.
    assert aec_verdict(_frames(2), _frames(900)) is None
    # A real un-cancelled beep still reads absent even against a tiny baseline.
    assert aec_verdict(_frames(2), _frames(6000)) is False


def test_aec_convergence_warmup_is_discarded():
    """Hardware AEC leaks for the first fraction of a second on a fresh echo
    path. Those frames are warm-up, not evidence — a converging canceller
    must still read as present."""
    from kenzy.calibration import ECHO_WARMUP_FRAMES, aec_verdict

    echo = _frames(3000, ECHO_WARMUP_FRAMES) + _frames(50, 20)
    assert aec_verdict(_frames(40), echo) is True


def test_aec_too_few_frames_is_no_verdict():
    from kenzy.calibration import ECHO_WARMUP_FRAMES, aec_verdict

    # Enough raw frames, but not after the warm-up discard.
    assert aec_verdict(_frames(40), _frames(50, ECHO_WARMUP_FRAMES + 3)) is None
    assert aec_verdict([], _frames(50)) is None


# ---------------------------------------------------------------------------
# AGC-aware suggestions — the M1A's second failure mode (2026-08-14): the wake
# phase measures speech at fully recovered gain, but a real command is spoken
# right after the ready chime with the gain clamped, so the voice-anchored
# silence suggestion (151–175 on the M1A) killed every live capture. The
# working value is ~60.
# ---------------------------------------------------------------------------


def _agc_quiet(n: int = 75) -> list[float]:
    """A quiet phase right after the probe beep on an AGC device: the floor
    climbs as the gain recovers."""
    return [15.0 + i * (150.0 - 15.0) / (n - 1) for i in range(n)]


def test_agc_suspected_on_rising_floor_only():
    from kenzy.calibration import agc_suspected

    assert agc_suspected(_agc_quiet()) is True
    # A stationary floor — loud or near-zero (gated mic) — is not AGC drift.
    assert agc_suspected([30.0] * 75) is False
    assert agc_suspected([1.0] * 75) is False
    # Too few frames isn't a trend.
    assert agc_suspected(_agc_quiet(12)) is False


def test_suggest_silence_caps_under_agc_drift():
    from kenzy.calibration import agc_suspected, suggest_silence

    quiet = _agc_quiet()
    speech = [400.0] * 40  # measured at recovered gain — NOT the capture gain
    assert agc_suspected(quiet) is True
    sil = suggest_silence(quiet, speech)
    # The voice anchor alone would say 0.4 × 400 = 160 — the value that broke
    # live capture. The suggestion must sit near the early (post-playback,
    # gain-clamped) floor instead.
    assert sil is not None
    assert 40 <= sil <= 90
    # A flat floor keeps the classic voice-anchored suggestion untouched.
    assert suggest_silence([10.0] * 90, [700.0] * 40) == 280


def test_suggest_vad_distrusts_voicey_ambient_and_caps():
    from kenzy.calibration import suggest_vad

    speech = [0.05] * 50 + [0.95] * 40
    # M1A shape: AGC pumps room noise into the speech band, so the QUIET floor
    # itself reads ~0.7 — untrustworthy, keep current rather than gate quiet wakes.
    assert suggest_vad([0.7] * 30, speech) is None
    # Just-trustworthy floor + a big gap: the suggestion exists but is capped at 0.6.
    v = suggest_vad([0.45] * 30, [1.0] * 40)
    assert v is not None and v <= 0.6
    # An ordinary quiet room gives a sensible mid gate.
    v2 = suggest_vad([0.05] * 30, speech)
    assert v2 is not None and 0.0 < v2 < 0.5

    # Regression — the 2026-08-30 field bug. A real wake capture is ~half speech,
    # so its own p75 lands in the speech band (here p75≈0.85, matching the live
    # NUROUM run). The old single-phase estimator read that as a "voice-like
    # floor" and refused EVERY honest calibration. Anchored on the quiet phase,
    # it now succeeds — the gate near ~0.3, where pi-b's real 0.36 once sat.
    half_speech = [0.24] * 72 + [0.9] * 79  # ~52% voiced, p75≈0.85
    v3 = suggest_vad([0.05] * 60, half_speech)
    assert v3 is not None and 0.2 < v3 < 0.45
