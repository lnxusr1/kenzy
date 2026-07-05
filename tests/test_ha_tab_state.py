"""The HA curation endpoint reports editor-state hints: skill_disabled (tab
shows a banner but stays editable — staging) and configured (no HA_API_KEY ⇒
onboarding guidance instead of an error)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from kenzy.llm import skills as sk
from kenzy.llm.builtin_skills import ha_model
from kenzy.llm.llm import app


def _quiet_ha(monkeypatch):
    async def boom():
        raise OSError("ha down")

    monkeypatch.setattr(ha_model, "fetch_raw", boom)
    monkeypatch.setattr(ha_model, "fetch_todo_lists", boom)
    monkeypatch.setattr(ha_model, "load_curation", lambda: {})


def test_curation_reports_skill_and_config_state(monkeypatch):
    _quiet_ha(monkeypatch)
    monkeypatch.setenv("HA_API_KEY", "token")
    monkeypatch.setattr(sk, "_DISABLED", set())
    body = TestClient(app).get("/ha/curation").json()
    assert body["skill_disabled"] is False
    assert body["configured"] is True

    monkeypatch.setattr(sk, "_DISABLED", {"home_assistant"})
    body = TestClient(app).get("/ha/curation").json()
    assert body["skill_disabled"] is True

    monkeypatch.delenv("HA_API_KEY", raising=False)
    body = TestClient(app).get("/ha/curation").json()
    assert body["configured"] is False
