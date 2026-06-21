"""Tests for centralized service config (M2): the server's effective service
config + always-on GET /config/<svc> endpoint, and the service-side bootstrap."""

from __future__ import annotations

import asyncio
import json
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from kenzy.server.server import AudioServer, NodeSession, TranscribingServer
from kenzy.serviceboot import _http_base, bootstrap_config


class _StubWS:
    async def send(self, m):  # noqa: ANN001, ANN201
        pass

# ---------------------------------------------------------------------------
# Server: effective service config = packaged default ← stored override
# ---------------------------------------------------------------------------


def test_effective_service_config_deep_merges(tmp_path, monkeypatch):
    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    services = tmp_path / "configs" / "services"
    services.mkdir(parents=True)
    # Partial override of a nested key — the rest of `whisper` must be retained.
    (services / "stt.yaml").write_text("whisper:\n  model: small\nport: 9000\n")
    srv = AudioServer({})
    eff = srv._effective_service_config("stt")
    assert eff["whisper"]["model"] == "small"  # override wins
    assert eff["whisper"]["device"] == "cpu"  # packaged default retained
    assert eff["port"] == 9000  # top-level override
    assert eff["host"] == "127.0.0.1"  # packaged default retained


def test_effective_service_config_strips_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    services = tmp_path / "configs" / "services"
    services.mkdir(parents=True)
    (services / "tts.yaml").write_text("openai:\n  api_key: sk-leak\n  voice: sage\n")
    srv = AudioServer({})
    eff = srv._effective_service_config("tts")
    assert "api_key" not in eff["openai"]  # secret-like key stripped
    assert eff["openai"]["voice"] == "sage"


# ---------------------------------------------------------------------------
# Always-on GET /config/<svc> over the node WS port
# ---------------------------------------------------------------------------


async def _serve(server: AudioServer):
    task = asyncio.create_task(server.serve())
    await asyncio.sleep(0.25)
    return task


def _http_get(url: str, token: str | None = None):
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, None


def test_write_service_override_validation(tmp_path, monkeypatch):
    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    (tmp_path / "configs").mkdir(parents=True)
    srv = AudioServer({})
    with pytest.raises(ValueError):
        srv.write_service_override("bogus", {"x": 1})  # unknown service
    with pytest.raises(ValueError):
        srv.write_service_override("node", {"x": 1})  # node not served here
    with pytest.raises(ValueError):
        srv.write_service_override("tts", {"openai": {"api_key": "sk-x"}})  # secret rejected
    srv.write_service_override("stt", {"whisper": {"model": "small"}})
    assert srv.read_service_override("stt") == {"whisper": {"model": "small"}}
    # And it shows through in the effective config.
    assert srv._effective_service_config("stt")["whisper"]["model"] == "small"
    srv.write_service_override("stt", {})  # empty clears the file
    assert srv.read_service_override("stt") == {}


async def test_config_endpoint_serves_effective(tmp_path, monkeypatch):
    monkeypatch.delenv("KENZY_SERVICE_TOKEN", raising=False)
    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    (tmp_path / "configs").mkdir(parents=True)
    server = AudioServer({"host": "127.0.0.1", "port": 8795})
    task = await _serve(server)
    try:
        status, body = await asyncio.to_thread(_http_get, "http://127.0.0.1:8795/config/stt")
        assert status == 200
        assert body["whisper"]["model"] == "tiny"  # packaged default
        # Unknown / node are not served via this endpoint.
        status, _ = await asyncio.to_thread(_http_get, "http://127.0.0.1:8795/config/node")
        assert status == 404
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_config_endpoint_token_gated(tmp_path, monkeypatch):
    monkeypatch.delenv("KENZY_SERVICE_TOKEN", raising=False)
    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    (tmp_path / "configs").mkdir(parents=True)
    server = AudioServer({"host": "127.0.0.1", "port": 8794, "discovery": {"token": "s3cret"}})
    task = await _serve(server)
    try:
        status, _ = await asyncio.to_thread(_http_get, "http://127.0.0.1:8794/config/stt")
        assert status == 401  # no bearer
        status, body = await asyncio.to_thread(
            _http_get, "http://127.0.0.1:8794/config/stt", "s3cret"
        )
        assert status == 200 and body["port"] == 8767
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


# ---------------------------------------------------------------------------
# Always-on /announce endpoint (Home Assistant / scripts → Kenzy speaks)
# ---------------------------------------------------------------------------


async def test_announce_endpoint(tmp_path, monkeypatch):
    monkeypatch.delenv("KENZY_SERVICE_TOKEN", raising=False)
    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    (tmp_path / "configs").mkdir(parents=True)
    server = TranscribingServer({"host": "127.0.0.1", "port": 8793})
    server._nodes["k"] = NodeSession(ws=_StubWS(), node_id="k", room_id="kitchen")
    calls: list[tuple[str, list[str] | None]] = []

    async def rec(text, rooms=None):
        calls.append((text, rooms))
        return len(rooms or [])

    monkeypatch.setattr(server, "announce", rec)
    task = await _serve(server)
    try:
        status, body = await asyncio.to_thread(
            _http_get, "http://127.0.0.1:8793/announce?text=hi&rooms=kitchen"
        )
        assert status == 200 and body["announced"] == 1
        assert calls == [("hi", ["k"])]  # room name resolved to node_id
        # Missing text → 400.
        status, _ = await asyncio.to_thread(_http_get, "http://127.0.0.1:8793/announce")
        assert status == 400
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_announce_endpoint_token_gated(tmp_path, monkeypatch):
    monkeypatch.delenv("KENZY_SERVICE_TOKEN", raising=False)
    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    (tmp_path / "configs").mkdir(parents=True)
    server = TranscribingServer(
        {"host": "127.0.0.1", "port": 8792, "discovery": {"token": "s3cret"}}
    )
    server._nodes["k"] = NodeSession(ws=_StubWS(), node_id="k", room_id="kitchen")

    async def rec(text, rooms=None):
        return 1

    monkeypatch.setattr(server, "announce", rec)
    task = await _serve(server)
    try:
        status, _ = await asyncio.to_thread(_http_get, "http://127.0.0.1:8792/announce?text=hi")
        assert status == 401  # no bearer
        status, body = await asyncio.to_thread(
            _http_get, "http://127.0.0.1:8792/announce?text=hi", "s3cret"
        )
        assert status == 200
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


# ---------------------------------------------------------------------------
# Service-side bootstrap_config
# ---------------------------------------------------------------------------


def test_http_base_normalizes():
    assert _http_base("ws://host:8765") == "http://host:8765"
    assert _http_base("wss://host:8765") == "https://host:8765"
    assert _http_base("http://host:8765/") == "http://host:8765"
    assert _http_base("host:8765") == "http://host:8765"
    with pytest.raises(ValueError):
        _http_base("")


def test_bootstrap_config_pulls_and_saves(tmp_path, monkeypatch):
    monkeypatch.delenv("KENZY_SERVICE_TOKEN", raising=False)
    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    (tmp_path / "configs").mkdir(parents=True)
    served = {"host": "127.0.0.1", "port": 8767, "whisper": {"model": "base"}}

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(served).encode())

        def log_message(self, *a):  # silence
            pass

    httpd = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    monkeypatch.setenv("KENZY_SERVER_URL", f"http://127.0.0.1:{httpd.server_address[1]}")
    try:
        cfg = bootstrap_config("stt", timeout=2.0)
    finally:
        httpd.shutdown()

    assert cfg == served
    # A local record is written to the writable config path.
    import yaml

    saved = yaml.safe_load((tmp_path / "configs" / "stt.yaml").read_text())
    assert saved["whisper"]["model"] == "base"
