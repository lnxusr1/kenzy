"""Tests for the skill registry's live enable/disable gate, invocation counts,
and the introspection snapshot (registry_info) backing the dashboard Skills view,
plus the kenzy-llm /skills endpoints."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from kenzy.llm import llm as llm_app
from kenzy.llm import skills as reg


@pytest.fixture
def clean_registry():
    """Isolate every registry global so tests don't leak into each other."""
    saved = (
        dict(reg._REGISTRY),
        list(reg._FAST_REGISTRY),
        set(reg._DISABLED),
        dict(reg._COUNTS),
    )
    reg._REGISTRY.clear()
    reg._FAST_REGISTRY.clear()
    reg._DISABLED.clear()
    reg._COUNTS.clear()
    try:
        yield
    finally:
        reg._REGISTRY.clear()
        reg._REGISTRY.update(saved[0])
        reg._FAST_REGISTRY[:] = saved[1]
        reg._DISABLED.clear()
        reg._DISABLED.update(saved[2])
        reg._COUNTS.clear()
        reg._COUNTS.update(saved[3])


def _register_demo():
    @reg.skill
    async def demo_skill(value: str) -> str:
        "A demo skill."
        return f"got {value}"

    @reg.fast_intent(priority=50)
    async def demo_skill_fast(utterance, room_id, speaker):  # same name family
        "fast"
        if "demo" in utterance:
            return reg.FastResult.handled("fast-handled")
        return reg.FastResult.miss()

    return demo_skill, demo_skill_fast


async def test_disabled_skill_excluded_from_tools_and_execute(clean_registry):
    _register_demo()
    assert any(t["function"]["name"] == "demo_skill" for t in reg.get_tools())

    reg.set_disabled(["demo_skill"])
    assert not any(t["function"]["name"] == "demo_skill" for t in reg.get_tools())
    # execute() guards even if called directly
    out = await reg.execute("demo_skill", {"value": "x"})
    assert "disabled" in out.lower()


async def test_enable_disable_is_live(clean_registry):
    _register_demo()
    reg.set_disabled(["demo_skill"])
    assert reg.get_tools() == []
    reg.set_disabled([])  # re-enable
    assert any(t["function"]["name"] == "demo_skill" for t in reg.get_tools())


async def test_invocation_counts(clean_registry):
    _register_demo()
    await reg.execute("demo_skill", {"value": "a"})
    await reg.execute("demo_skill", {"value": "b"})
    info = {s["name"]: s for s in reg.registry_info()["skills"]}
    assert info["demo_skill"]["calls"] == 2


async def test_disabled_fast_intent_skipped(clean_registry):
    _register_demo()
    res = await reg.dispatch_fast("demo please", None, None)
    assert res is not None and res.text == "fast-handled"

    reg.set_disabled(["demo_skill_fast"])
    assert await reg.dispatch_fast("demo please", None, None) is None


def test_registry_info_shape(clean_registry):
    _register_demo()
    reg.set_disabled(["demo_skill"])
    info = reg.registry_info()
    by_name = {s["name"]: s for s in info["skills"]}
    assert by_name["demo_skill"]["disabled"] is True
    assert by_name["demo_skill"]["description"] == "A demo skill."
    fast_names = {f["name"] for f in info["fast_intents"]}
    assert "demo_skill_fast" in fast_names


# --- kenzy-llm HTTP endpoints (auth is installed only in main(); not here) ---


def test_skills_endpoints(clean_registry):
    _register_demo()
    client = TestClient(llm_app.app)

    r = client.get("/skills")
    assert r.status_code == 200
    names = {s["name"] for s in r.json()["skills"]}
    assert "demo_skill" in names

    r = client.post("/skills", json={"disabled": ["demo_skill"]})
    assert r.status_code == 200
    assert reg._DISABLED == {"demo_skill"}
    by_name = {s["name"]: s for s in r.json()["skills"]}
    assert by_name["demo_skill"]["disabled"] is True


# --- dashboard toggle: persists to the llm override + live-applies ---


async def test_dashboard_set_skill_disabled_persists_and_applies(tmp_path, monkeypatch):
    from kenzy.server.dashboard import Dashboard, DashboardConfig
    from kenzy.server.server import AudioServer

    server = AudioServer({})
    monkeypatch.setattr(server, "_service_override_path", lambda svc: tmp_path / f"{svc}.yaml")
    dash = Dashboard(server, {}, DashboardConfig(controls=True))

    posted: dict = {}

    async def fake_req(method, payload=None):
        if method == "GET":
            return {"skills": [{"name": "weather", "disabled": False}], "fast_intents": []}
        posted["disabled"] = payload["disabled"]
        return {"skills": [], "fast_intents": []}

    monkeypatch.setattr(dash, "_llm_skills_request", fake_req)

    ok, err = await dash._set_skill_disabled("weather", True)
    assert ok and err is None
    # live-applied with the new list…
    assert posted["disabled"] == ["weather"]
    # …and persisted to the llm override (survives restart)
    import yaml

    saved = yaml.safe_load((tmp_path / "llm.yaml").read_text())
    assert saved["skills"]["disabled"] == ["weather"]
