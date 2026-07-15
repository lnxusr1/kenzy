"""End-to-end token-proof service auth against a live TLS AudioServer."""

from __future__ import annotations

import asyncio
import json
import subprocess

import httpx
import pytest

from kenzy import serviceauth, serviceboot
from kenzy.server.server import AudioServer

_NO_KEEPALIVE = httpx.Limits(max_keepalive_connections=0)


@pytest.fixture(scope="module")
def certpair(tmp_path_factory):
    d = tmp_path_factory.mktemp("hmac_tls")
    cert, key = d / "s.crt", d / "s.key"
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
            "-keyout", str(key), "-out", str(cert), "-subj", "/CN=hmac-e2e",
        ],
        check=True,
        capture_output=True,
    )
    return str(cert), str(key)


async def _serve(cfg):
    server = AudioServer(cfg)
    task = asyncio.create_task(server.serve())
    await asyncio.sleep(0.3)
    return server, task


async def test_signed_config_pull_over_tls(certpair, tmp_path, monkeypatch):
    """The real serviceboot client pulls /config over wss, and the server's
    response signature verifies against the cert the client actually saw."""
    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    (tmp_path / "configs").mkdir()
    monkeypatch.setenv("KENZY_SERVICE_TOKEN", "the-fleet-token")
    monkeypatch.delenv("KENZY_TLS_VERIFY", raising=False)
    cert, key = certpair
    _server, task = await _serve(
        {
            "host": "127.0.0.1", "port": 8841,
            "tls": {"cert": cert, "key": key},
            "discovery": {"token": "the-fleet-token"},
        }
    )
    try:
        status, body, ok = await asyncio.to_thread(
            serviceboot._signed_get,
            "https://127.0.0.1:8841", "/config/stt", "the-fleet-token", 5.0,
        )
        assert status == 200
        assert ok  # response signature validated against the server's real cert
        assert json.loads(body)["port"] == 8767  # a genuine stt default came back
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_response_binding_rejects_a_forged_cert(certpair, tmp_path, monkeypatch):
    """A relay presenting a different cert can't produce a reply the client
    accepts: the server signs bound to ITS cert; verifying under any other
    binding fails."""
    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    (tmp_path / "configs").mkdir()
    cert, key = certpair
    server, task = await _serve(
        {
            "host": "127.0.0.1", "port": 8842,
            "tls": {"cert": cert, "key": key},
            "discovery": {"token": "tok"},
        }
    )
    try:
        assert server._channel_binding  # server bound to its own leaf cert
        ts, body = 100, b'{"x":1}'
        sig = serviceauth.sign_service_response("tok", ts, body, binding=server._channel_binding)
        assert serviceauth.verify_service_response(
            sig, "tok", ts, body, binding=server._channel_binding
        )
        assert not serviceauth.verify_service_response(
            sig, "tok", ts, body, binding=bytes(32)
        )
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_legacy_bearer_accepted_and_unsigned(certpair, tmp_path, monkeypatch):
    """A pre-3.11 client (bearer only) is accepted during the window; the reply
    is unsigned. A wrong bearer with no signature is 401."""
    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    (tmp_path / "configs").mkdir()
    cert, key = certpair
    _server, task = await _serve(
        {
            "host": "127.0.0.1", "port": 8843,
            "tls": {"cert": cert, "key": key},
            "discovery": {"token": "legacytok"},
        }
    )
    try:
        async with httpx.AsyncClient(verify=False, limits=_NO_KEEPALIVE) as c:
            r = await c.get(
                "https://127.0.0.1:8843/config/tts",
                headers={"Authorization": "Bearer legacytok"},
            )
            assert r.status_code == 200
            assert "X-Kenzy-Sig" not in r.headers  # legacy path is unsigned
            bad = await c.get(
                "https://127.0.0.1:8843/config/tts",
                headers={"Authorization": "Bearer wrong"},
            )
            assert bad.status_code == 401
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_token_proof_pull_signs_the_response(certpair, tmp_path, monkeypatch):
    """A KENZY-HMAC request (no bearer) is accepted and the response carries a
    valid X-Kenzy-Sig."""
    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    (tmp_path / "configs").mkdir()
    cert, key = certpair
    server, task = await _serve(
        {
            "host": "127.0.0.1", "port": 8844,
            "tls": {"cert": cert, "key": key},
            "discovery": {"token": "proof"},
        }
    )
    try:
        hdr = serviceauth.sign_service_request("proof", "GET", "/config/llm")
        async with httpx.AsyncClient(verify=False, limits=_NO_KEEPALIVE) as c:
            r = await c.get(
                "https://127.0.0.1:8844/config/llm",
                headers={serviceauth.SIG_HEADER: hdr},
            )
            assert r.status_code == 200
            ts = int(hdr.split("ts=")[1].split(",")[0])
            assert serviceauth.verify_service_response(
                r.headers.get("X-Kenzy-Sig"), "proof", ts, r.content,
                binding=server._channel_binding,
            )
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
