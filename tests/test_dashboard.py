"""Tests for the dashboard foundation: config gate, service-target derivation,
and the read-only HTTP surfaces served via the websockets HTTP hook."""

from __future__ import annotations

import asyncio
import json
import logging

import httpx
import pytest
import websockets
from websockets.datastructures import Headers
from websockets.http11 import Request

from kenzy.server.dashboard import Dashboard, DashboardConfig, _service_targets
from kenzy.server.server import AudioServer, NodeSession
from kenzy.serviceauth import COOKIE_NAME, hash_password, sign_cookie


def _req(path: str) -> Request:
    return Request(path, Headers())


def test_config_disabled_by_default():
    assert DashboardConfig.from_cfg({}).enabled is False
    d = DashboardConfig.from_cfg({"dashboard": {"enabled": True, "controls": True}})
    assert d.enabled is True and d.controls is True
    assert d.bind == "127.0.0.1" and d.port == 8770


def test_service_targets_derives_health_urls():
    cfg = {
        "stt": {"url": "http://127.0.0.1:8767/transcribe"},
        "llm": {"url": "http://10.0.0.5:8766/process"},
        "tts": {},  # no url → skipped
    }
    targets = _service_targets(cfg)
    assert targets["stt"] == "http://127.0.0.1:8767/health"
    assert targets["llm"] == "http://10.0.0.5:8766/health"
    assert "tts" not in targets


def test_mutation_auth_bearer_and_failclosed():
    server = AudioServer({})
    d = Dashboard(server, {}, DashboardConfig(auth_token="secret"))

    class _Req:
        def __init__(self, tok):
            self.headers = {"Authorization": f"Bearer {tok}"} if tok else {}

    assert d._authorized_mutation(_Req("secret")) is True
    assert d._authorized_mutation(_Req("nope")) is False
    # No credentials configured at all → mutations fail closed (read GETs stay open).
    d2 = Dashboard(server, {}, DashboardConfig())
    assert d2._authorized_mutation(_Req(None)) is False


class _StubWS:
    pass


async def test_dashboard_http_surfaces():
    server = AudioServer({"node_defaults": {"wakeword_threshold": 0.5}})
    server._nodes["den"] = NodeSession(
        ws=_StubWS(), room_id="den", streaming=True, session_id="abcd1234ef"
    )
    dash = Dashboard(server, {}, DashboardConfig(enabled=True, bind="127.0.0.1", port=8771))
    task = asyncio.create_task(dash.serve())
    await asyncio.sleep(0.25)
    try:
        async with httpx.AsyncClient(base_url="http://127.0.0.1:8771") as c:
            # static index
            r = await c.get("/")
            assert r.status_code == 200
            assert "KENZY" in r.text

            # fleet state
            r = await c.get("/api/state")
            assert r.status_code == 200
            state = r.json()
            assert state["nodes"][0]["room_id"] == "den"
            assert state["nodes"][0]["streaming"] is True
            assert state["services"] == []  # none configured
            assert state["flags"] == {"logs": False, "tuning": False, "controls": False}

            # per-room effective config (read-only)
            r = await c.get("/api/rooms/den/config")
            assert r.json()["config"]["wakeword_threshold"] == 0.5

            # unknown api endpoint
            r = await c.get("/api/nope")
            assert r.status_code == 404

            # path traversal is contained
            r = await c.get("/../../etc/passwd")
            assert r.status_code == 404
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_node_state_includes_ip():
    class _WSAddr:
        remote_address = ("192.168.1.42", 54321)

    s = AudioServer({})
    s._nodes["den"] = NodeSession(ws=_WSAddr(), room_id="den")
    d = Dashboard(s, {}, DashboardConfig(enabled=True))
    node = d._nodes_state()[0]
    assert node["ip"] == "192.168.1.42"
    # stub without remote_address → ip is None, not an error
    s._nodes["bath"] = NodeSession(ws=_StubWS(), room_id="bath")
    assert {n["room_id"]: n["ip"] for n in d._nodes_state()}["bath"] is None


def test_effective_config_strips_secrets():
    s = AudioServer({"node_defaults": {"wakeword_threshold": 0.5, "openai_api_key": "sk-x"}})
    eff = s._effective_node_config("room")
    assert eff["wakeword_threshold"] == 0.5
    assert "openai_api_key" not in eff


def test_write_node_override_validation(tmp_path, monkeypatch):
    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    s = AudioServer({})
    with pytest.raises(ValueError):  # path traversal
        s.write_node_override("../etc", {"vad_enabled": True})
    with pytest.raises(ValueError):  # key not in allow-list
        s.write_node_override("room", {"openai_api_key": "x"})
    s.write_node_override("room", {"wakeword_threshold": 0.7})
    assert s.read_node_override("room") == {"wakeword_threshold": 0.7}
    s.write_node_override("room", {})  # empty clears the file
    assert s.read_node_override("room") == {}


class _Cap:
    def __init__(self):
        self.sent = []

    async def send(self, m):
        self.sent.append(json.loads(m))


async def test_set_override_gated_by_controls(tmp_path, monkeypatch):
    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    s = AudioServer({})
    dash = Dashboard(s, {}, DashboardConfig(enabled=True, controls=False))
    cap = _Cap()
    await dash._handle_ws_message(
        cap,
        json.dumps(
            {"id": "1", "type": "set_override", "room": "kit", "config": {"vad_enabled": False}}
        ),
    )
    assert cap.sent[0] == {"type": "ack", "id": "1", "ok": False, "error": cap.sent[0]["error"]}
    assert cap.sent[0]["ok"] is False
    assert s.read_node_override("kit") == {}  # nothing written


async def test_set_override_applies_when_controls_on(tmp_path, monkeypatch):
    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    s = AudioServer({"node_defaults": {"wakeword_threshold": 0.5}})
    dash = Dashboard(s, {}, DashboardConfig(enabled=True, controls=True))
    cap = _Cap()
    await dash._handle_ws_message(
        cap,
        json.dumps(
            {
                "id": "2",
                "type": "set_override",
                "room": "kit",
                "config": {"wakeword_threshold": 0.7},
            }
        ),
    )
    acks = [m for m in cap.sent if m.get("type") == "ack"]
    assert acks and acks[0]["ok"] is True
    assert s.read_node_override("kit") == {"wakeword_threshold": 0.7}


async def test_set_override_rejects_unknown_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    s = AudioServer({})
    dash = Dashboard(s, {}, DashboardConfig(enabled=True, controls=True))
    cap = _Cap()
    await dash._handle_ws_message(
        cap,
        json.dumps({"id": "3", "type": "set_override", "room": "kit", "config": {"api_key": "x"}}),
    )
    assert cap.sent[0]["ok"] is False and "unsupported" in cap.sent[0]["error"]


def test_display_name_set_validate_clear(tmp_path, monkeypatch):
    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    s = AudioServer({})
    s.set_display_name("kitchen", "The Kitchen")
    assert s.read_node_names() == {"kitchen": "The Kitchen"}
    with pytest.raises(ValueError):
        s.set_display_name("../x", "X")
    with pytest.raises(ValueError):
        s.set_display_name("kitchen", "x" * 65)
    s.set_display_name("kitchen", "")  # clears
    assert s.read_node_names() == {}


async def test_set_name_via_ws_surfaces_in_state(tmp_path, monkeypatch):
    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    s = AudioServer({})
    s._nodes["kitchen"] = NodeSession(ws=_StubWS(), room_id="kitchen")
    dash = Dashboard(s, {}, DashboardConfig(enabled=True, controls=True))
    assert dash._nodes_state()[0]["configured"] is False  # fresh node is unconfigured

    cap = _Cap()
    await dash._handle_ws_message(
        cap, json.dumps({"id": "1", "type": "set_name", "room": "kitchen", "name": "Kitchen"})
    )
    assert any(m.get("type") == "ack" and m["ok"] for m in cap.sent)
    node = dash._nodes_state()[0]
    assert node["display_name"] == "Kitchen" and node["configured"] is True


async def test_set_name_gated_by_controls(tmp_path, monkeypatch):
    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    s = AudioServer({})
    dash = Dashboard(s, {}, DashboardConfig(enabled=True, controls=False))
    cap = _Cap()
    await dash._handle_ws_message(
        cap, json.dumps({"id": "1", "type": "set_name", "room": "k", "name": "X"})
    )
    assert cap.sent[0]["ok"] is False
    assert s.read_node_names() == {}


async def test_controls_gated_and_dispatched(tmp_path, monkeypatch):
    s = AudioServer({})
    node = _Cap()  # records frames the server sends to the node
    s._nodes["kit"] = NodeSession(ws=node, room_id="kit")

    # controls off → refused, nothing sent to the node
    off = Dashboard(s, {}, DashboardConfig(enabled=True, controls=False))
    cap = _Cap()
    await off._handle_ws_message(cap, json.dumps({"id": "1", "type": "restart", "room": "kit"}))
    assert cap.sent[0]["ok"] is False and node.sent == []

    # controls on → dispatched to the node
    on = Dashboard(s, {}, DashboardConfig(enabled=True, controls=True))
    cap = _Cap()
    for i, t in enumerate(("trigger", "stop", "restart")):
        await on._handle_ws_message(cap, json.dumps({"id": str(i), "type": t, "room": "kit"}))
    assert all(m["ok"] for m in cap.sent if m["type"] == "ack")
    assert {m["type"] for m in node.sent} == {"trigger", "stop", "restart"}

    # unknown room → ok False
    cap = _Cap()
    await on._handle_ws_message(cap, json.dumps({"id": "9", "type": "stop", "room": "ghost"}))
    assert cap.sent[0]["ok"] is False


class _RecWS:
    """Node WS stub that records raw frames (text JSON + binary PCM)."""

    def __init__(self):
        self.sent = []

    async def send(self, m):
        self.sent.append(m)


async def test_announce_streams_to_all_nodes(monkeypatch):
    from kenzy.server.server import TranscribingServer

    s = TranscribingServer({"tts": {"url": "http://tts/speak"}})
    a, b = _RecWS(), _RecWS()
    s._nodes["a"] = NodeSession(ws=a, room_id="a")
    s._nodes["b"] = NodeSession(ws=b, room_id="b")

    async def fake_synth(text, vp):
        return b"\x00\x01\x02\x03"

    monkeypatch.setattr(s, "_synthesize", fake_synth)
    assert await s.announce("dinner is ready") == 2
    for ws in (a, b):
        texts = [m for m in ws.sent if isinstance(m, str)]
        assert any("tts_start" in t for t in texts)
        assert any("tts_end" in t for t in texts)
        assert any(isinstance(m, bytes) for m in ws.sent)  # got audio


async def test_announce_noop_without_tts():
    assert await AudioServer({}).announce("hi") == 0  # base server has no TTS


async def test_announce_ws_gated_empty_and_ok(monkeypatch):
    from kenzy.server.server import TranscribingServer

    s = TranscribingServer({"tts": {"url": "http://tts/speak"}})
    s._nodes["a"] = NodeSession(ws=_RecWS(), room_id="a")

    off = Dashboard(s, {}, DashboardConfig(enabled=True, controls=False))
    cap = _Cap()
    await off._handle_ws_message(cap, json.dumps({"id": "1", "type": "announce", "text": "hi"}))
    assert cap.sent[0]["ok"] is False  # controls disabled

    on = Dashboard(s, {}, DashboardConfig(enabled=True, controls=True))
    cap = _Cap()
    await on._handle_ws_message(cap, json.dumps({"id": "2", "type": "announce", "text": "  "}))
    assert cap.sent[0]["ok"] is False and "empty" in cap.sent[0]["error"]

    async def fake_synth(text, vp):
        return b"\x00\x01"

    monkeypatch.setattr(s, "_synthesize", fake_synth)
    cap = _Cap()
    await on._handle_ws_message(cap, json.dumps({"id": "3", "type": "announce", "text": "hello"}))
    assert any(m.get("type") == "ack" and m["ok"] for m in cap.sent)
    assert [m for m in cap.sent if m.get("type") == "announce_result"][0]["count"] == 1


def test_ring_buffer_tail_and_level():
    from kenzy.logutil import RingBufferHandler

    h = RingBufferHandler(capacity=5)
    lg = logging.getLogger("ring-test")
    lg.setLevel(logging.DEBUG)
    lg.addHandler(h)
    lg.debug("d")
    lg.info("i")
    lg.warning("w")
    assert [r["msg"] for r in h.tail()] == ["d", "i", "w"]
    assert [r["msg"] for r in h.tail(logging.WARNING)] == ["w"]
    assert len(h.tail(limit=2)) == 2


def test_service_logs_endpoint(monkeypatch):
    monkeypatch.delenv("KENZY_SERVICE_TOKEN", raising=False)
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from kenzy.fastapi_auth import install_logs_endpoint

    app = FastAPI()
    install_logs_endpoint(app, "svc-log-test")
    logging.getLogger("svc-log-test").warning("hello from service")
    logs = TestClient(app).get("/logs?level=WARNING").json()["logs"]
    assert any("hello from service" in e["msg"] for e in logs)


async def test_request_node_logs_roundtrip():
    s = AudioServer({})
    captured = {}

    class _NodeWS:
        async def send(self, raw):
            captured["req"] = json.loads(raw)

    sess = NodeSession(ws=_NodeWS(), room_id="kit")
    s._nodes["kit"] = sess

    async def driver():
        for _ in range(100):
            if "req" in captured:
                break
            await asyncio.sleep(0.005)
        await s._handle_control(
            sess,
            {
                "type": "logs",
                "request_id": captured["req"]["request_id"],
                "logs": [{"msg": "node line"}],
            },
        )

    task = asyncio.create_task(driver())
    out = await s.request_node_logs("kit", timeout=2)
    await task
    assert out == [{"msg": "node line"}]
    assert await s.request_node_logs("ghost") is None  # offline → None


async def test_dashboard_log_routes():
    s = AudioServer({})
    d = Dashboard(s, {}, DashboardConfig(enabled=True, logs=True))
    assert s._capture_node_logs is True  # logs flag drives node capture
    logging.getLogger("kenzy").warning("server marker xyz")
    assert any("server marker xyz" in e["msg"] for e in d._tail_server_logs(_req("/api/logs")))
    out = await d._node_logs("ghost", _req("/api/rooms/ghost/logs"))
    assert out["reachable"] is False and out["logs"] == []
    # logs disabled → server buffer not installed, route returns empty
    d2 = Dashboard(AudioServer({}), {}, DashboardConfig(enabled=True, logs=False))
    assert d2._tail_server_logs(_req("/api/logs")) == []


async def test_ws_live_channel_requires_auth_and_pushes():
    pw = hash_password("password")
    server = AudioServer({})
    dash = Dashboard(
        server,
        {},
        DashboardConfig(
            enabled=True,
            bind="127.0.0.1",
            port=8794,
            auth_username="admin",
            auth_password_hash=pw,
        ),
    )
    task = asyncio.create_task(dash.serve())
    await asyncio.sleep(0.25)
    try:
        # Unauthenticated upgrade is rejected (process_request returns 401).
        with pytest.raises(websockets.exceptions.InvalidStatus):
            await websockets.connect("ws://127.0.0.1:8794/ws")

        # Authenticated via the login cookie: snapshot on connect, push on change.
        cookie = f"{COOKIE_NAME}={sign_cookie('admin', pw)}"
        async with websockets.connect(
            "ws://127.0.0.1:8794/ws", additional_headers={"Cookie": cookie}
        ) as ws:
            snap = json.loads(await asyncio.wait_for(ws.recv(), 2))
            assert snap["type"] == "state" and snap["data"]["nodes"] == []

            server._nodes["kitchen"] = NodeSession(ws=_StubWS(), room_id="kitchen")
            server._notify_state()
            push = json.loads(await asyncio.wait_for(ws.recv(), 2))
            assert any(n["room_id"] == "kitchen" for n in push["data"]["nodes"])
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
