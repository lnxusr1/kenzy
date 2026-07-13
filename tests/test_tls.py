"""Optional TLS (F-13, first slice): server-terminated wss/https with
encrypted-but-unverified clients — the self-signed home-LAN posture."""

from __future__ import annotations

import asyncio
import json
import ssl
import subprocess

import httpx
import pytest
import websockets

from kenzy import protocol, tlsutil
from kenzy.discovery import _service_url
from kenzy.server.dashboard import Dashboard, DashboardConfig
from kenzy.server.server import AudioServer


@pytest.fixture(scope="module")
def certpair(tmp_path_factory):
    d = tmp_path_factory.mktemp("tls")
    cert, key = d / "kenzy.crt", d / "kenzy.key"
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-days", "2", "-keyout", str(key), "-out", str(cert),
            "-subj", "/CN=kenzy-test",
        ],
        check=True,
        capture_output=True,
    )
    return str(cert), str(key)


def test_client_context_default_is_encrypted_unverified():
    ctx = tlsutil.client_context()
    assert ctx.verify_mode == ssl.CERT_NONE and ctx.check_hostname is False
    # Opting in restores real verification.
    strict = tlsutil.client_context(verify=True)
    assert strict.verify_mode == ssl.CERT_REQUIRED and strict.check_hostname is True


def test_client_context_from_env(monkeypatch):
    monkeypatch.delenv("KENZY_TLS_VERIFY", raising=False)
    monkeypatch.delenv("KENZY_TLS_CA", raising=False)
    assert tlsutil.client_context_from_env().verify_mode == ssl.CERT_NONE
    monkeypatch.setenv("KENZY_TLS_VERIFY", "1")
    assert tlsutil.client_context_from_env().verify_mode == ssl.CERT_REQUIRED


def test_discovery_url_scheme_follows_tls_flag():
    assert _service_url("10.0.0.5", 8765, {b"tls": b"1"}) == "wss://10.0.0.5:8765"
    assert _service_url("10.0.0.5", 8765, {b"tls": b"0"}) == "ws://10.0.0.5:8765"
    assert _service_url("10.0.0.5", 8765, {}) == "ws://10.0.0.5:8765"  # legacy server


async def test_wss_node_roundtrip(certpair, tmp_path, monkeypatch):
    """A node connects over wss with the unverified client context and completes
    the hello → config handshake — the real join path, encrypted."""
    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    (tmp_path / "configs").mkdir()
    cert, key = certpair
    server = AudioServer(
        {"host": "127.0.0.1", "port": 8821, "tls": {"cert": cert, "key": key}}
    )
    assert server._ssl is not None
    task = asyncio.create_task(server.serve())
    await asyncio.sleep(0.3)
    try:
        ctx = tlsutil.client_context()  # what the node builds for wss://
        async with websockets.connect("wss://127.0.0.1:8821", ssl=ctx) as ws:
            await ws.send(protocol.hello("den", node_id="tls-node"))
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=2.0))
            assert msg["type"] == protocol.MSG_CONFIG
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_https_dashboard_and_secure_cookie(certpair):
    cert, key = certpair
    cfg = {"tls": {"cert": cert, "key": key}}
    from kenzy.serviceauth import hash_password

    pw = hash_password("password")
    d = Dashboard(
        AudioServer(cfg),
        cfg,
        DashboardConfig(
            enabled=True, bind="127.0.0.1", port=8822,
            auth_username="admin", auth_password_hash=pw,
        ),
    )
    assert d._ssl is not None
    task = asyncio.create_task(d.serve())
    await asyncio.sleep(0.3)
    try:
        # The websockets HTTP hook closes after each response; disable keep-alive
        # so strict h11 doesn't trip on the reused connection.
        limits = httpx.Limits(max_keepalive_connections=0)
        async with httpx.AsyncClient(
            verify=False, base_url="https://127.0.0.1:8822", limits=limits
        ) as c:
            r = await c.get("/")
            assert r.status_code == 200 and "KENZY" in r.text
            r = await c.get("/api/login", auth=("admin", "password"))
            assert r.status_code == 200
            # Direct TLS marks the session cookie Secure (F-7).
            assert "Secure" in r.headers.get("set-cookie", "")
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_plain_config_means_no_tls():
    server = AudioServer({"host": "127.0.0.1", "port": 0})
    assert server._ssl is None
    # A broken pair degrades to plaintext with a logged error, not a crash.
    bad = AudioServer(
        {"host": "127.0.0.1", "port": 0, "tls": {"cert": "/nope.crt", "key": "/nope.key"}}
    )
    assert bad._ssl is None


def test_uvicorn_tls_kwargs(certpair, monkeypatch):
    cert, key = certpair
    monkeypatch.delenv("KENZY_TLS_CERT", raising=False)
    monkeypatch.delenv("KENZY_TLS_KEY", raising=False)
    # from the config block
    kw = tlsutil.uvicorn_tls_kwargs({"tls": {"cert": cert, "key": key}})
    assert kw == {"ssl_certfile": cert, "ssl_keyfile": key}
    # absent -> plaintext
    assert tlsutil.uvicorn_tls_kwargs({}) == {}
    # configured but missing files -> warn + plaintext, never a boot failure
    assert tlsutil.uvicorn_tls_kwargs({"tls": {"cert": "/nope.crt", "key": "/nope.key"}}) == {}
    # env wins over config (the multi-host path)
    monkeypatch.setenv("KENZY_TLS_CERT", cert)
    monkeypatch.setenv("KENZY_TLS_KEY", key)
    kw = tlsutil.uvicorn_tls_kwargs({"tls": {"cert": "/other.crt", "key": "/other.key"}})
    assert kw == {"ssl_certfile": cert, "ssl_keyfile": key}


def test_served_service_config_carries_the_tls_pair(certpair, tmp_path, monkeypatch):
    """A TLS server injects its cert pair into co-located services' configs —
    and the injection must survive the secret-stripper (which eats "key")."""
    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    (tmp_path / "configs").mkdir()
    cert, key = certpair
    server = AudioServer({"host": "127.0.0.1", "port": 0, "tls": {"cert": cert, "key": key}})
    cfg = server._effective_service_config("stt")
    assert cfg["tls"] == {"cert": cert, "key": key}
    # plaintext server injects nothing
    plain = AudioServer({"host": "127.0.0.1", "port": 0})
    assert "tls" not in plain._effective_service_config("stt")


def test_loopback_urls_auto_upgrade_with_mesh_tls(certpair, tmp_path, monkeypatch):
    """A pre-TLS config's static http://127.0.0.1 service URLs must follow the
    server into TLS — co-located services WILL be serving https."""
    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    (tmp_path / "configs").mkdir()
    cert, key = certpair
    cfg = {
        "host": "127.0.0.1", "port": 0,
        "tls": {"cert": cert, "key": key},
        "stt": {"url": "http://127.0.0.1:8767/transcribe"},
        "tts": {"url": "http://otherhost:8769/speak"},  # remote: operator's call
    }
    server = AudioServer(cfg)
    assert server._peer_service_urls["stt"] == "https://127.0.0.1:8767/transcribe"
    assert server._peer_service_urls["tts"] == "http://otherhost:8769/speak"
    # without TLS nothing changes
    plain = AudioServer({"host": "127.0.0.1", "port": 0,
                         "stt": {"url": "http://127.0.0.1:8767/transcribe"}})
    assert plain._peer_service_urls["stt"] == "http://127.0.0.1:8767/transcribe"
