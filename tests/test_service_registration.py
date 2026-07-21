"""Backend-service auto-registration.

A service announces itself via ``GET /register``; the server records it, fills the
pipeline URL when none is statically configured (but never overrides a configured
one), resolves ``0.0.0.0`` to the request's source IP, exposes it for the dashboard
health view, TTL-prunes stale registrations, and gates the endpoint on the token.
"""

from __future__ import annotations

import asyncio
import json
import time
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlencode

from kenzy.server.server import TranscribingServer


async def _serve(server: TranscribingServer) -> asyncio.Task[None]:
    task = asyncio.create_task(server.serve())
    await asyncio.sleep(0.25)
    return task


def _register(base_url: str, token: str | None = None, **params: Any) -> tuple[int, Any]:
    req = urllib.request.Request(f"{base_url}/register?{urlencode(params)}")
    if token:
        from kenzy import serviceauth

        req.add_header(
            serviceauth.SIG_HEADER, serviceauth.sign_service_request(token, "GET", "/register")
        )
    try:
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, None


async def test_register_records_and_fills_pipeline_url(tmp_path: Any, monkeypatch: Any) -> None:
    monkeypatch.delenv("KENZY_SERVICE_TOKEN", raising=False)
    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    (tmp_path / "configs").mkdir(parents=True)
    server = TranscribingServer({"host": "127.0.0.1", "port": 8796})  # no static llm.url
    assert server._llm_url is None
    task = await _serve(server)
    try:
        status, body = await asyncio.to_thread(
            _register,
            "http://127.0.0.1:8796",
            service="llm",
            host="127.0.0.1",
            port=8766,
            version="3.3.1",
        )
        assert status == 200 and body["ok"] is True
        assert server._llm_url == "http://127.0.0.1:8766/process"  # pipeline url filled
        assert server.announced_health_urls()["llm"] == "http://127.0.0.1:8766/health"
        assert server.announced_service_version("llm") == "3.3.1"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_register_host_0000_uses_source_ip(tmp_path: Any, monkeypatch: Any) -> None:
    monkeypatch.delenv("KENZY_SERVICE_TOKEN", raising=False)
    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    (tmp_path / "configs").mkdir(parents=True)
    server = TranscribingServer({"host": "127.0.0.1", "port": 8797})
    task = await _serve(server)
    try:
        await asyncio.to_thread(
            _register, "http://127.0.0.1:8797", service="stt", host="0.0.0.0", port=8767
        )
        # 0.0.0.0 → resolved to the request's source IP (loopback in the test)
        assert server._stt_url == "http://127.0.0.1:8767/transcribe"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_register_does_not_override_static_url(tmp_path: Any, monkeypatch: Any) -> None:
    monkeypatch.delenv("KENZY_SERVICE_TOKEN", raising=False)
    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    (tmp_path / "configs").mkdir(parents=True)
    server = TranscribingServer(
        {"host": "127.0.0.1", "port": 8798, "llm": {"url": "http://static:8766/process"}}
    )
    task = await _serve(server)
    try:
        await asyncio.to_thread(
            _register, "http://127.0.0.1:8798", service="llm", host="127.0.0.1", port=9999
        )
        assert server._llm_url == "http://static:8766/process"  # configured url wins
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_register_token_gated(tmp_path: Any, monkeypatch: Any) -> None:
    monkeypatch.delenv("KENZY_SERVICE_TOKEN", raising=False)
    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    (tmp_path / "configs").mkdir(parents=True)
    server = TranscribingServer(
        {"host": "127.0.0.1", "port": 8799, "discovery": {"token": "s3cret"}}
    )
    task = await _serve(server)
    try:
        status, _ = await asyncio.to_thread(
            _register, "http://127.0.0.1:8799", service="llm", host="127.0.0.1", port=8766
        )
        assert status == 401  # no auth → rejected
        status, _ = await asyncio.to_thread(
            _register, "http://127.0.0.1:8799", "s3cret", service="llm", host="127.0.0.1", port=8766
        )
        assert status == 200
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_announced_health_urls_prunes_stale(tmp_path: Any, monkeypatch: Any) -> None:
    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    (tmp_path / "configs").mkdir(parents=True)
    server = TranscribingServer({})
    server._announced_services["tts"] = {
        "base": "http://x:8769",
        "version": None,
        "last_seen": time.time() - 999,  # well past the TTL
    }
    server._tts_url = "http://x:8769/speak"
    assert server.announced_health_urls() == {}  # pruned out
    assert server._tts_url is None  # and its pipeline url cleared
