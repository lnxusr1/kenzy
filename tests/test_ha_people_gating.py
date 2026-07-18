"""Tests for the HA-surface gating (F3): the LLM's /ha/persons endpoint, the
server's assist-seen marker, and the dashboard's cached availability flags."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from kenzy.llm import llm as llm_app
from kenzy.server.dashboard import Dashboard, DashboardConfig
from kenzy.server.server import TranscribingServer

# ---------------------------------------------------------------------------
# kenzy-llm: GET /ha/persons
# ---------------------------------------------------------------------------


def test_ha_persons_unconfigured_is_cheap(monkeypatch):
    monkeypatch.delenv("HA_API_KEY", raising=False)

    def boom():  # any HA call would be a bug when unconfigured
        raise AssertionError("must not call HA")

    from kenzy.llm.builtin_skills import ha_model

    monkeypatch.setattr(ha_model, "fetch_persons", boom)
    body = TestClient(llm_app.app).get("/ha/persons").json()
    assert body == {
        "configured": False,
        "skill_disabled": False,
        "reachable": False,
        "persons": [],
    }


def test_ha_persons_lists_entities(monkeypatch):
    monkeypatch.setenv("HA_API_KEY", "x")

    async def fake_persons():
        return [{"entity_id": "person.john_mark", "name": "John Mark"}]

    from kenzy.llm.builtin_skills import ha_model

    monkeypatch.setattr(ha_model, "fetch_persons", fake_persons)
    body = TestClient(llm_app.app).get("/ha/persons").json()
    assert body["configured"] and body["reachable"]
    assert body["persons"] == [{"entity_id": "person.john_mark", "name": "John Mark"}]


def test_ha_persons_unreachable_degrades(monkeypatch):
    monkeypatch.setenv("HA_API_KEY", "x")

    async def fail():
        raise OSError("nope")

    from kenzy.llm.builtin_skills import ha_model

    monkeypatch.setattr(ha_model, "fetch_persons", fail)
    body = TestClient(llm_app.app).get("/ha/persons").json()
    assert body["configured"] and not body["reachable"] and body["persons"] == []


# ---------------------------------------------------------------------------
# Server: assist-seen marker
# ---------------------------------------------------------------------------


def test_assist_seen_persists(tmp_path, monkeypatch):
    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    s = TranscribingServer({})
    assert s.assist_seen() is False
    s.mark_assist_seen()
    assert s.assist_seen() is True
    # A new server instance (restart) reads the marker back.
    s2 = TranscribingServer({})
    assert s2.assist_seen() is True


# ---------------------------------------------------------------------------
# Dashboard: availability flags
# ---------------------------------------------------------------------------


def _dash(tmp_path, monkeypatch) -> Dashboard:
    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    server = TranscribingServer({})
    return Dashboard(server, {}, DashboardConfig()), server


@pytest.mark.parametrize(
    "assist_seen,ha_key,expect_active",
    [(False, False, False), (True, False, True), (False, True, True)],
)
async def test_ha_flags_active_signal(tmp_path, monkeypatch, assist_seen, ha_key, expect_active):
    monkeypatch.delenv("HA_API_KEY", raising=False)
    if ha_key:
        monkeypatch.setenv("HA_API_KEY", "x")
    dash, server = _dash(tmp_path, monkeypatch)
    monkeypatch.setattr(dash, "_service_base", lambda name: None)  # llm unreachable
    if assist_seen:
        server.mark_assist_seen()
    flags = await dash._ha_flags()
    assert flags["active"] is expect_active


async def test_ha_flags_disabled_module_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("HA_API_KEY", "x")
    dash, server = _dash(tmp_path, monkeypatch)

    class _R:
        status_code = 200

        @staticmethod
        def raise_for_status():
            pass

        @staticmethod
        def json():
            return {"configured": True, "skill_disabled": True, "reachable": False, "persons": []}

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, *a, **k):
            return _R()

    import httpx

    monkeypatch.setattr(dash, "_service_base", lambda name: "http://x")
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    flags = await dash._ha_flags()
    assert flags["skill_disabled"] is True and flags["active"] is False
