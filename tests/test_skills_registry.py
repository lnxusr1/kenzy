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
        dict(reg._MODULES),
        dict(reg._MIN_TIER),
    )
    reg._REGISTRY.clear()
    reg._FAST_REGISTRY.clear()
    reg._DISABLED.clear()
    reg._COUNTS.clear()
    reg._MODULES.clear()
    reg._MIN_TIER.clear()
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
        reg._MODULES.clear()
        reg._MODULES.update(saved[4])
        reg._MIN_TIER.clear()
        reg._MIN_TIER.update(saved[5])


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


async def test_pep604_optional_list_schema_and_coercion(clean_registry):
    """`items: list[str] | None` must advertise as an array in the tool schema.
    The PEP 604 union origin (types.UnionType) used to miss the Union branch and
    fall through to the string fallback — so the model was *instructed* to pass
    "broccoli" as a string, which list() then exploded into letters. The array
    schema also lets execute()'s string-wrapping guard engage."""
    seen: dict[str, object] = {}

    @reg.skill
    async def maker(name: str, items: list[str] | None = None) -> str:
        "Creates a thing."
        seen["items"] = items
        return "ok"

    (schema,) = [t for t in reg.get_tools() if t["function"]["name"] == "maker"]
    props = schema["function"]["parameters"]["properties"]
    assert props["items"] == {"type": "array", "items": {"type": "string"}}
    assert schema["function"]["parameters"]["required"] == ["name"]  # None default → optional

    await reg.execute("maker", {"name": "Groceries", "items": "broccoli"})
    assert seen["items"] == ["broccoli"]


async def test_execute_wraps_bare_string_for_array_params(clean_registry):
    """A model sending "items": "broccoli" for a list[str] param must become
    ["broccoli"], not eight one-letter items (the grocery-list bug)."""
    seen: dict[str, object] = {}

    @reg.skill
    async def list_taker(items: list[str], note: str = "") -> str:
        "Takes a list."
        seen["items"], seen["note"] = items, note
        return "ok"

    await reg.execute("list_taker", {"items": "broccoli", "note": "x"})
    assert seen["items"] == ["broccoli"]  # wrapped, not exploded
    assert seen["note"] == "x"  # string params untouched

    await reg.execute("list_taker", {"items": ["milk", "eggs"]})
    assert seen["items"] == ["milk", "eggs"]  # real arrays pass through


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


# ---------------------------------------------------------------------------
# Module-aware disabling (there is no skill literally named "home_assistant" —
# the MODULE is the unit that means the feature; found via the dashboard)
# ---------------------------------------------------------------------------


async def test_module_disable_gates_members_and_fast_intents(clean_registry, monkeypatch):
    import kenzy.llm.skills as sk

    @sk.skill
    async def fake_control(x: str) -> str:
        """Fake HA control."""
        return "ok"

    @sk.fast_intent(priority=5)
    async def fake_fast(utterance, room_id, speaker):
        return sk.FastResult.handled("fast!", "v")

    # Simulate both living in a module file called fake_ha.py
    monkeypatch.setitem(sk._MODULES, "fake_control", "fake_ha")
    monkeypatch.setitem(sk._MODULES, "fake_fast", "fake_ha")

    sk.set_disabled(["fake_ha"])  # module name, not a function name
    assert sk.is_disabled("fake_ha") is True
    assert sk.is_disabled("fake_control") is True  # member inherits
    assert all(t["function"]["name"] != "fake_control" for t in sk.get_tools())
    assert "disabled" in await sk.execute("fake_control", {"x": "1"})
    r = await sk.dispatch_fast("anything", None, None)
    assert r is None  # fast intent silenced by its module

    sk.set_disabled([])
    assert sk.is_disabled("fake_ha") is False
    assert (await sk.dispatch_fast("anything", None, None)).is_handled


async def test_all_members_disabled_means_module_disabled(clean_registry, monkeypatch):
    import kenzy.llm.skills as sk

    @sk.skill
    async def m_one(x: str) -> str:
        """One."""
        return "1"

    @sk.skill
    async def m_two(x: str) -> str:
        """Two."""
        return "2"

    monkeypatch.setitem(sk._MODULES, "m_one", "featmod")
    monkeypatch.setitem(sk._MODULES, "m_two", "featmod")

    sk.set_disabled(["m_one"])
    assert sk.is_disabled("featmod") is False  # partially off ≠ off
    sk.set_disabled(["m_one", "m_two"])
    assert sk.is_disabled("featmod") is True  # every member off ⇒ module off


def test_registry_info_reports_modules(clean_registry, monkeypatch):
    import kenzy.llm.skills as sk

    info = sk.registry_info()
    assert "modules" in info
    for s in info["skills"]:
        assert "module" in s


async def test_unknown_disabled_entry_warns(clean_registry, caplog):
    import logging

    import kenzy.llm.skills as sk

    _register_demo()
    with caplog.at_level(logging.WARNING, logger="kenzy.llm.skills"):
        sk.set_disabled(["definitely_not_a_skill"])
    assert any("match no skill" in r.message for r in caplog.records)

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="kenzy.llm.skills"):
        sk.set_disabled(["demo_skill"])  # valid → silent
    assert not any("match no skill" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# F1.3 — identity-tier gating (min_tier consumed as a contract)
# ---------------------------------------------------------------------------


def _register_gated():
    @reg.skill(min_tier="recognized")
    async def vault_skill() -> str:
        """Open the vault."""
        return "vault open"

    @reg.skill
    async def open_skill() -> str:
        """Available to everyone."""
        return "ok"

    @reg.fast_intent(priority=99, min_tier="recognized")
    async def vault_fast(utterance, room_id, speaker):  # noqa: ANN001
        if "vault" in utterance:
            return reg.FastResult.handled("vault open (fast)")
        return reg.FastResult.miss()


def _as_tier(tier):
    reg.begin_request({"speaker_tier": tier})


async def test_min_tier_hides_tools_from_unknown(clean_registry):
    _register_gated()
    _as_tier("unknown")
    names = [t["function"]["name"] for t in reg.get_tools()]
    assert names == ["open_skill"]  # the gated tool is withheld entirely
    _as_tier("recognized")
    names = [t["function"]["name"] for t in reg.get_tools()]
    assert set(names) == {"vault_skill", "open_skill"}
    _as_tier("verified")  # higher tier satisfies a lower requirement
    assert "vault_skill" in [t["function"]["name"] for t in reg.get_tools()]


async def test_min_tier_execute_refuses_below_tier(clean_registry):
    _register_gated()
    _as_tier("unknown")
    out = await reg.execute("vault_skill", {})
    assert "Refused" in out and "recognized" in out
    assert reg._COUNTS.get("vault_skill", 0) == 0  # a refusal is not an invocation
    _as_tier("recognized")
    assert await reg.execute("vault_skill", {}) == "vault open"


async def test_min_tier_fast_intent_never_runs_below_tier(clean_registry):
    _register_gated()
    _as_tier("unknown")
    assert await reg.dispatch_fast("open the vault", "office", None) is None
    _as_tier("recognized")
    res = await reg.dispatch_fast("open the vault", "office", None)
    assert res is not None and res.text == "vault open (fast)"


async def test_tier_defaults_to_unknown_outside_request(clean_registry):
    # Fresh context (no begin_request in THIS context) — anything gated is
    # withheld: fail-closed for old servers that don't send a tier.
    import contextvars

    _register_gated()

    def check():
        assert reg.current_tier() == "unknown"
        assert [t["function"]["name"] for t in reg.get_tools()] == ["open_skill"]

    contextvars.copy_context().run(check)
    # An explicit None/garbage tier also degrades to unknown.
    _as_tier(None)
    assert reg.current_tier() == "unknown"
    _as_tier("bogus")
    assert reg.current_tier() == "unknown"


async def test_min_tier_in_registry_info(clean_registry):
    _register_gated()
    info = reg.registry_info()
    tiers = {s["name"]: s["min_tier"] for s in info["skills"]}
    assert tiers == {"vault_skill": "recognized", "open_skill": None}
    fast = {f["name"]: f["min_tier"] for f in info["fast_intents"]}
    assert fast["vault_fast"] == "recognized"


async def test_min_tier_validation():
    with pytest.raises(ValueError):

        @reg.skill(min_tier="sudo")
        async def bad() -> str:
            """Nope."""
            return ""
