"""Tests for the calibration telemetry primitive: tune_start/stop/sample, the node's
per-frame measurement emit, the server relay, and dashboard fan-out to subscribers."""

from __future__ import annotations

import asyncio
import json

import numpy as np

from kenzy import protocol
from kenzy.node.client import (
    NodeClient,
    _percentile,
    _suggest_silence_rms,
    _suggest_vad_threshold,
    _suggest_wake_threshold,
)
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

    # Silence: mostly-quiet samples → threshold just above the p90 floor.
    assert _suggest_silence_rms([10] * 90 + [200] * 10) == 25
    assert _suggest_silence_rms([]) is None

    # Wake: a clear gap between ambient and utterance peak → a value inside it.
    w = _suggest_wake_threshold([0.02] * 90 + [0.9] * 10)
    assert w is not None and 0.02 < w < 0.9
    assert _suggest_wake_threshold([0.01] * 50) is None  # no utterance heard → no guess

    # VAD: same gap logic, clamped to [0, 0.9].
    v = _suggest_vad_threshold([0.05] * 90 + [0.95] * 10)
    assert v is not None and 0.0 <= v <= 0.9
    assert _suggest_vad_threshold([0.0] * 30) is None


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
