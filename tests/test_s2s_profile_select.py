"""The s2s.profile selector (v6.0): which engine sits behind the seam.

"kenzy" (local, default) or "openai-realtime" (the cloud opt-in). The selector
is config-only — the seam and gate are identical either way; what changes is
the endpoint, the auth, and the dialect normalization the profile encodes.
"""

from __future__ import annotations

from typing import Any

from kenzy.server.server import TranscribingServer


def _srv(**s2s: Any) -> TranscribingServer:
    return TranscribingServer({"s2s": {"enabled": True, **s2s}})


# --- profile resolution ------------------------------------------------------


def test_default_profile_is_the_local_engine():
    assert _srv()._s2s_profile().name == "kenzy-s2s"


def test_openai_profile_resolves():
    assert _srv(profile="openai-realtime")._s2s_profile().name == "openai-realtime"


def test_unknown_profile_warns_and_falls_back_local(caplog):
    """A typo'd profile must not silently aim at the wrong engine — warn once
    (not per capture) and use the local default."""
    s = _srv(profile="opnai")
    with caplog.at_level("WARNING"):
        assert s._s2s_profile().name == "kenzy-s2s"
        assert s._s2s_profile().name == "kenzy-s2s"
    warned = [r for r in caplog.records if "Unknown s2s.profile" in r.message]
    assert len(warned) == 1  # once, with the known names in the message
    assert "openai-realtime" in warned[0].getMessage()


# --- endpoint resolution -----------------------------------------------------


def test_cloud_profile_uses_its_own_endpoint_not_the_registry():
    """The registry only knows co-registered kenzy services — falling through
    to it would aim a cloud profile at the local engine."""
    s = _srv(profile="openai-realtime")
    assert s._s2s_engine_url() == "wss://api.openai.com/v1/realtime"


def test_explicit_url_wins_over_the_profile_default():
    s = _srv(profile="openai-realtime", url="wss://proxy.lan/v1/realtime")
    assert s._s2s_engine_url() == "wss://proxy.lan/v1/realtime"


def test_local_profile_still_resolves_via_the_registry():
    s = _srv()
    assert s._s2s_engine_url() == ""  # nothing registered, no fabricated address
    s._announced_services["s2s"] = {"base": "http://10.0.0.5:8771"}
    assert s._s2s_engine_url() == "ws://10.0.0.5:8771/v1/realtime"


# --- the factory: credentials + model ----------------------------------------


class _StubClient:
    last: dict[str, Any] = {}

    def __init__(self, profile: Any, *, model: str = "", api_key: str = "", url: str = "") -> None:
        _StubClient.last = {"profile": profile, "model": model, "api_key": api_key, "url": url}

    async def connect(self) -> None:
        pass


async def _factory_with(monkeypatch, srv: TranscribingServer, url: str) -> dict[str, Any]:
    import kenzy.s2s.engine as engine_mod

    monkeypatch.setattr(engine_mod, "EngineClient", _StubClient)
    await srv._s2s_engine_factory(url)
    return _StubClient.last


async def test_openai_factory_sends_the_openai_key_and_default_model(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("CUSTOM_LLM_API_KEY", "ck-test")
    got = await _factory_with(
        monkeypatch, _srv(profile="openai-realtime"), "wss://api.openai.com/v1/realtime"
    )
    assert got["profile"].name == "openai-realtime"
    assert got["api_key"] == "sk-test"
    assert got["model"] == "gpt-realtime"  # config s2s.model overrides this


async def test_non_openai_bearer_endpoint_never_gets_the_openai_key(monkeypatch):
    """The endpoint-kwargs seam rule, applied to the realtime path: a custom
    bearer endpoint gets CUSTOM_LLM_API_KEY, never the OpenAI credential."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("CUSTOM_LLM_API_KEY", "ck-test")
    got = await _factory_with(
        monkeypatch,
        _srv(profile="openai-realtime", url="wss://proxy.lan/v1/realtime"),
        "wss://proxy.lan/v1/realtime",
    )
    assert got["api_key"] == "ck-test"


async def test_configured_model_overrides_the_profile_default(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    got = await _factory_with(
        monkeypatch,
        _srv(profile="openai-realtime", model="gpt-realtime-mini"),
        "wss://api.openai.com/v1/realtime",
    )
    assert got["model"] == "gpt-realtime-mini"


async def test_local_factory_sends_no_credential(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    got = await _factory_with(monkeypatch, _srv(), "ws://127.0.0.1:8771/v1/realtime")
    assert got["profile"].name == "kenzy-s2s"
    assert got["api_key"] == ""
    assert got["model"] == ""  # the local engine configures its model in s2s.yaml


def test_a_registered_local_engine_cannot_hijack_a_cloud_profile():
    """The dev stack runs kenzy-s2s regardless of profile, and its heartbeat
    fills _s2s_url via /register. That fill is routing convenience, not an
    operator decision — with a cloud profile selected, the profile's endpoint
    wins (lived 2026-08-29: the local engine captured the route and the
    'cloud' conversation ran locally, in the wrong voice)."""
    s = _srv(profile="openai-realtime")
    # what GET /register does when no static url is configured:
    s._announced_services["s2s"] = {"base": "https://127.0.0.1:8771"}
    s._s2s_url = "https://127.0.0.1:8771/v1/realtime"
    assert s._s2s_engine_url() == "wss://api.openai.com/v1/realtime"


def test_a_configured_url_survives_registration():
    """s2s joins the static-services contract every other service has: an
    operator-configured URL is never overwritten by an announce."""
    s = _srv(url="wss://proxy.lan/v1/realtime")
    assert "s2s" in s._static_services
