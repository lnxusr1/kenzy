"""Dashboard module-toggle semantics — the "Enable all does nothing" bug: a
module that reads disabled because every member was switched off one-by-one
must be enable-able from the group toggle (discarding just the module name was
a silent no-op)."""

from __future__ import annotations

from kenzy.server.dashboard import Dashboard, DashboardConfig
from kenzy.server.server import TranscribingServer

_INFO = {
    "skills": [
        {"name": "handle_home_control", "module": "home_assistant", "disabled": True},
        {"name": "get_device_states", "module": "home_assistant", "disabled": True},
        {"name": "add_to_list", "module": "lists", "disabled": False},
    ],
    "fast_intents": [
        {"name": "fast_home_control", "module": "home_assistant", "disabled": True},
    ],
    "modules": [
        {"name": "home_assistant", "disabled": True},
        {"name": "lists", "disabled": False},
    ],
}


async def test_module_toggle_operates_on_members(tmp_path, monkeypatch):
    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    s = TranscribingServer({})
    d = Dashboard(s, {}, DashboardConfig(enabled=True))
    posted: list[dict] = []

    async def fake_req(method, body=None):
        if method == "POST":
            posted.append(body)
            return {"ok": True}
        return _INFO

    monkeypatch.setattr(d, "_llm_skills_request", fake_req)

    # Enable all: members were disabled individually — all must be cleared.
    ok, err = await d._set_skill_disabled("home_assistant", False)
    assert ok, err
    assert posted[-1]["disabled"] == []

    # Disable all: redundant member entries collapse into one module entry.
    ok, _ = await d._set_skill_disabled("home_assistant", True)
    assert posted[-1]["disabled"] == ["home_assistant"]

    # Individual (non-module) toggles behave as before.
    ok, _ = await d._set_skill_disabled("add_to_list", True)
    assert "add_to_list" in posted[-1]["disabled"]
    ok, _ = await d._set_skill_disabled("fast_home_control", False)
    assert "fast_home_control" not in posted[-1]["disabled"]


async def test_skills_state_passes_modules_through(tmp_path, monkeypatch):
    """The proxy must not drop the modules field — without it the group header
    can never show module state (the "never switches to Enable all" bug)."""
    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    s = TranscribingServer({})
    d = Dashboard(s, {}, DashboardConfig(enabled=True))

    async def fake_req(method, body=None):
        return _INFO

    monkeypatch.setattr(d, "_llm_skills_request", fake_req)
    state = await d._skills_state()
    assert state["modules"] == _INFO["modules"]
    assert state["skills"] and state["fast_intents"]
