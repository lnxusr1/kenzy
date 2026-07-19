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

from kenzy.server.server import AudioServer, LlmReply, NodeSession, TranscribingServer
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


def test_effective_service_config_without_override(tmp_path, monkeypatch):
    """include_override=False returns just the inherited layer — what the
    dashboard editor shows as field placeholders."""
    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    services = tmp_path / "configs" / "services"
    services.mkdir(parents=True)
    (services / "stt.yaml").write_text("whisper:\n  model: small\n")
    import yaml as _yaml

    from kenzy.config import packaged_config

    packaged = _yaml.safe_load(packaged_config("stt").read_text())
    srv = AudioServer({})
    defaults = srv._effective_service_config("stt", include_override=False)
    assert defaults["whisper"]["model"] == packaged["whisper"]["model"]  # override ignored
    assert srv._effective_service_config("stt")["whisper"]["model"] == "small"


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


# ---------------------------------------------------------------------------
# Auto-wired peer endpoints (server injects e.g. tts.url into speaker config)
# ---------------------------------------------------------------------------


def test_effective_config_injects_peer_url(tmp_path, monkeypatch):
    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    (tmp_path / "configs").mkdir(parents=True)
    srv = AudioServer({"tts": {"url": "http://tts:8769/speak"}})
    # speaker depends on tts → auto-wired from the server's configured tts.url.
    assert srv._effective_service_config("speaker")["tts"]["url"] == "http://tts:8769/speak"
    # A service with no declared peer deps doesn't get it.
    assert "tts" not in srv._effective_service_config("stt")


def test_peer_url_override_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    services = tmp_path / "configs" / "services"
    services.mkdir(parents=True)
    (services / "speaker.yaml").write_text("tts:\n  url: http://local-tts:9/speak\n")
    srv = AudioServer({"tts": {"url": "http://server-tts:8769/speak"}})
    # An explicit value in the service's own config (override) wins (multi-host escape).
    assert srv._effective_service_config("speaker")["tts"]["url"] == "http://local-tts:9/speak"


def test_no_injection_without_configured_peer(tmp_path, monkeypatch):
    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    (tmp_path / "configs").mkdir(parents=True)
    srv = AudioServer({})  # server has no tts.url configured
    assert "tts" not in srv._effective_service_config("speaker")


def test_fetch_service_config_best_effort(tmp_path, monkeypatch):
    monkeypatch.delenv("KENZY_SERVICE_TOKEN", raising=False)
    from kenzy.serviceboot import fetch_service_config

    served = {"tts": {"url": "http://tts:8769/speak"}}

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(served).encode())

        def log_message(self, *a):
            pass

    httpd = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    monkeypatch.setenv("KENZY_SERVER_URL", f"http://127.0.0.1:{httpd.server_address[1]}")
    try:
        assert fetch_service_config("speaker", timeout=2.0) == served
    finally:
        httpd.shutdown()

    # Unreachable server → None, no retry/block (dead port).
    monkeypatch.setenv("KENZY_SERVER_URL", "http://127.0.0.1:1")
    assert fetch_service_config("speaker", timeout=0.5) is None


# ---------------------------------------------------------------------------
# Always-on /assist endpoint (F3 — the HA Assist conversation channel)
# ---------------------------------------------------------------------------


async def test_assist_endpoint(tmp_path, monkeypatch):
    from kenzy.server.people import PeopleStore

    monkeypatch.delenv("KENZY_SERVICE_TOKEN", raising=False)
    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    (tmp_path / "configs").mkdir(parents=True)
    server = TranscribingServer(
        {"host": "127.0.0.1", "port": 8792, "llm": {"url": "http://llm:8766/process"}}
    )
    pfile = tmp_path / "people.yaml"
    pfile.write_text(
        "people:\n  john:\n    name: John\n    voiceprints: [johnmark]\n"
        "    ha_user: person.john_mark\n"
    )
    server._people = PeopleStore(pfile)
    seen: list[tuple[str, str, object]] = []

    async def fake_llm(
        text, room_id, session_id, speaker=None, node_id=None, identity=None, channel="voice"
    ):
        seen.append((text, room_id, identity, channel))
        return LlmReply(f"Hi {speaker}!", "vp", fast=True)

    monkeypatch.setattr(server, "_call_llm", fake_llm)
    task = await _serve(server)
    try:
        # Mapped HA person → recognized identity, per-person assist lane.
        status, body = await asyncio.to_thread(
            _http_get,
            "http://127.0.0.1:8792/assist?text=hello%20there&ha_user=person.john_mark",
        )
        assert status == 200
        assert body == {"text": "Hi John!", "speaker": "John", "recognized": True, "fast": True}
        text, lane, identity, channel = seen[-1]
        assert text == "hello there" and lane == "assist:john"
        assert identity.person_id == "john" and identity.tier == "recognized"
        assert channel == "assist"  # F3.2: skills see the nodeless channel

        # Unmapped HA user → unknown, fail closed (still answers, ungated tier).
        status, body = await asyncio.to_thread(
            _http_get, "http://127.0.0.1:8792/assist?text=hi&ha_user=person.guest"
        )
        assert status == 200 and body["recognized"] is False
        # Guests get a PER-HA-USER lane so two guests never share context.
        assert seen[-1][1] == "assist:guest:person.guest" and seen[-1][2].tier == "unknown"

        # Missing text → 400.
        status, _ = await asyncio.to_thread(_http_get, "http://127.0.0.1:8792/assist")
        assert status == 400

        # The endpoint itself flips the assist-seen marker (HA-surface reveal
        # for app-only households) — regression: it was once wired to /announce.
        assert server.assist_seen() is True
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_assist_endpoint_token_gated_and_base_stub(tmp_path, monkeypatch):
    monkeypatch.delenv("KENZY_SERVICE_TOKEN", raising=False)
    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    (tmp_path / "configs").mkdir(parents=True)
    # Token set ⇒ unauthenticated /assist is refused.
    server = TranscribingServer(
        {"host": "127.0.0.1", "port": 8791, "discovery": {"token": "s3cret"},
         "llm": {"url": "http://llm:8766/process"}}
    )
    task = await _serve(server)
    try:
        status, _ = await asyncio.to_thread(_http_get, "http://127.0.0.1:8791/assist?text=hi")
        assert status == 401
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    # The base AudioServer (no pipeline) answers 501, not a crash.
    base = AudioServer({"host": "127.0.0.1", "port": 8790})
    task = await _serve(base)
    try:
        status, _ = await asyncio.to_thread(_http_get, "http://127.0.0.1:8790/assist?text=hi")
        assert status == 501
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_service_signature_wire_format_is_frozen():
    """The KENZY-HMAC request signature is a WIRE CONTRACT reimplemented by
    external clients (kenzy-hass carries a byte-for-byte copy with this same
    vector). Changing the derivation breaks every deployed integration —
    this test makes that a loud, deliberate decision."""
    from kenzy.serviceauth import sign_service_request

    assert sign_service_request("test-fleet-token", "GET", "/assist", ts=1700000000) == (
        "KENZY-HMAC ts=1700000000, "
        "sig=eb3e55be3ad06bc15573b6b3e5807a6706a3db9e92a5fae6ad2fbc44c5a99e68"
    )
