"""Tests for the dashboard foundation: config gate, service-target derivation,
and the read-only HTTP surfaces served via the websockets HTTP hook."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from kenzy.server.dashboard import Dashboard, DashboardConfig, _service_targets
from kenzy.server.server import AudioServer, NodeSession


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


def test_auth_token_check():
    server = AudioServer({})
    d = Dashboard(server, {}, DashboardConfig(auth_token="secret"))

    class _Req:
        def __init__(self, tok):
            self.headers = {"Authorization": f"Bearer {tok}"} if tok else {}

    assert d._authorized(_Req("secret")) is True
    assert d._authorized(_Req("nope")) is False
    # No token configured → open
    d2 = Dashboard(server, {}, DashboardConfig())
    assert d2._authorized(_Req(None)) is True


class _StubWS:
    pass


async def test_dashboard_http_surfaces():
    server = AudioServer({"node_defaults": {"wakeword_threshold": 0.5}})
    server._nodes["den"] = NodeSession(ws=_StubWS(), room_id="den", streaming=True,
                                       session_id="abcd1234ef")
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
            assert state["services"] == []           # none configured
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
