"""Audio-init failure must be non-fatal: the node stays connected and controllable
(so a bad device can be fixed + the node restarted from the dashboard)."""

from __future__ import annotations

import asyncio
import json

from kenzy import protocol
from kenzy.node.client import NodeClient
from kenzy.server.server import AudioServer, NodeSession


class _StubWS:
    async def send(self, m):  # noqa: ANN001, ANN201
        pass


class _FakeWS:
    """Minimal client WebSocket: yields queued frames from recv(), records sends."""

    def __init__(self, frames: list[str]):
        self._frames = list(frames)
        self.sent: list[str] = []

    async def send(self, m):  # noqa: ANN001, ANN201
        self.sent.append(m)

    async def recv(self):  # noqa: ANN201
        if self._frames:
            return self._frames.pop(0)
        await asyncio.Future()  # block until cancelled

    async def close(self):  # noqa: ANN201
        pass


def test_status_message_roundtrip():
    assert json.loads(protocol.status(True)) == {
        "type": "status",
        "audio_ok": True,
        "audio_error": None,
    }
    assert json.loads(protocol.status(False, "bad device"))["audio_error"] == "bad device"


async def test_server_marks_node_audio_failed():
    srv = AudioServer({})
    session = NodeSession(ws=_StubWS(), node_id="k", room_id="kitchen")
    srv._nodes["k"] = session
    assert session.audio_ok is True  # healthy by default
    await srv._handle_control(
        session, {"type": protocol.MSG_STATUS, "audio_ok": False, "audio_error": "PortAudio"}
    )
    assert session.audio_ok is False
    assert session.audio_error == "PortAudio"


async def test_status_delivers_device_list():
    srv = AudioServer({})
    session = NodeSession(ws=_StubWS(), node_id="k", room_id="kitchen")
    srv._nodes["k"] = session
    devices = [{"index": 1, "name": "Anker", "suggested": {"audio_device": "Anker"}}]
    await srv._handle_control(
        session, {"type": protocol.MSG_STATUS, "audio_ok": True, "devices": devices}
    )
    assert session.audio_ok is True
    assert session.capabilities["devices"] == devices


async def test_audio_init_failure_is_non_fatal_and_restartable(monkeypatch):
    import kenzy.node.client as client_mod

    execv_calls: list[tuple] = []
    monkeypatch.setattr(client_mod.os, "execv", lambda *a: execv_calls.append(a))
    monkeypatch.setattr("kenzy.node.devices.probe_devices", lambda: [])  # no real audio probe

    client = NodeClient({"node_id": "n1", "room_id": "kitchen"})

    async def boom():
        raise RuntimeError("no such audio device")

    monkeypatch.setattr(client, "_init_audio", boom)

    ws = _FakeWS([protocol.config({})])  # server pushes (empty) config on connect
    task = asyncio.create_task(client._run_session(ws))  # type: ignore[arg-type]
    try:
        await asyncio.sleep(0.05)
        # Init failed but the node did NOT disconnect — it's degraded, not dead.
        assert client._audio_failed is True
        assert client._audio_error == "no such audio device"
        assert not task.done()

        # It reported the failure to the server.
        statuses = [json.loads(s) for s in ws.sent if '"status"' in s]
        assert statuses and statuses[-1]["audio_ok"] is False

        # The command loop is live, so Restart (the fix path) is reachable.
        client._cmd_q.put_nowait({"type": protocol.MSG_RESTART})
        await asyncio.sleep(0.05)
        assert execv_calls, "restart command was not honored while audio was degraded"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_discover_server_cancel_returns_promptly(monkeypatch):
    """A set cancel_event must abort the browse fast (not block for the full timeout),
    so Ctrl+C during discovery doesn't stall interpreter exit."""
    import threading
    import time

    import zeroconf

    class _DummyZC:
        def __init__(self, *a, **k):
            pass

        def get_service_info(self, *a, **k):
            return None

        def close(self):
            pass

    monkeypatch.setattr(zeroconf, "Zeroconf", _DummyZC)
    monkeypatch.setattr(zeroconf, "ServiceBrowser", lambda *a, **k: None)
    monkeypatch.setattr(zeroconf, "ServiceListener", object)

    from kenzy.discovery import discover_server

    cancel = threading.Event()
    out: dict = {}

    def go():
        t0 = time.monotonic()
        out["url"] = discover_server(30.0, cancel)  # would block 30s without cancel
        out["dt"] = time.monotonic() - t0

    th = threading.Thread(target=go)
    th.start()
    time.sleep(0.1)
    cancel.set()
    th.join(2.0)
    assert not th.is_alive(), "discover_server ignored cancel_event"
    assert out["dt"] < 1.0
    assert out["url"] is None


async def test_run_exits_gracefully_on_cancel(monkeypatch):
    """Cancelling run() (what the SIGINT handler does) returns cleanly, not by raising
    CancelledError out of the process."""
    import kenzy.node.client as client_mod

    monkeypatch.setattr("kenzy.node.devices.probe_devices", lambda: [])  # no real audio probe
    client = NodeClient({"node_id": "n1", "room_id": "kitchen"})
    client._server_url = "ws://test"  # skip discovery

    async def fake_connect(url, *a, **k):
        return _FakeWS([protocol.config({})])

    monkeypatch.setattr(client_mod.websockets, "connect", fake_connect)

    async def fake_init():
        client._audio_ready = True

    monkeypatch.setattr(client, "_init_audio", fake_init)

    task = asyncio.create_task(client.run())
    await asyncio.sleep(0.1)  # connect + settle into the session
    task.cancel()
    # run() catches CancelledError and returns None — no exception escapes.
    await asyncio.wait_for(task, timeout=3.0)


async def test_audio_init_success_clears_failed_flag(monkeypatch):
    monkeypatch.setattr("kenzy.node.devices.probe_devices", lambda: [])  # no real audio probe
    client = NodeClient({"node_id": "n1", "room_id": "kitchen"})

    async def ok():
        client._audio_ready = True

    monkeypatch.setattr(client, "_init_audio", ok)

    ws = _FakeWS([protocol.config({})])
    task = asyncio.create_task(client._run_session(ws))  # type: ignore[arg-type]
    try:
        await asyncio.sleep(0.05)
        assert client._audio_failed is False
        assert client._audio_ready is True
        # A healthy node may emit a status to deliver its device list, but it must
        # never report an audio failure.
        statuses = [json.loads(s) for s in ws.sent if json.loads(s).get("type") == "status"]
        assert all(s["audio_ok"] for s in statuses)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
