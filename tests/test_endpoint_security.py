"""F-14 guard: OPENAI_API_KEY must never travel to a custom LiteLLM base_url.

``base_url`` is dashboard-editable by design (Ollama/LM Studio/proxies), so a
request to it must not inherit the OpenAI key — otherwise repointing the URL
exfiltrates it via the Authorization header. ``skills.endpoint_kwargs`` is the
single seam all three LiteLLM call sites (main loop, news summarizer, HA
resolver) route through.
"""

from __future__ import annotations

from kenzy.llm.skills import endpoint_kwargs


def test_no_base_url_means_no_override(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-real")
    assert endpoint_kwargs(None) == {}
    assert endpoint_kwargs("") == {}


def test_custom_base_url_never_inherits_openai_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-real")
    monkeypatch.delenv("CUSTOM_LLM_API_KEY", raising=False)
    kwargs = endpoint_kwargs("http://attacker.example:9999/v1")
    assert kwargs["base_url"] == "http://attacker.example:9999/v1"
    # An explicit api_key override MUST be present (otherwise LiteLLM falls back
    # to OPENAI_API_KEY), and it must not be the real key.
    assert "api_key" in kwargs
    assert kwargs["api_key"] != "sk-real"


def test_custom_endpoint_uses_dedicated_key_when_set(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-real")
    monkeypatch.setenv("CUSTOM_LLM_API_KEY", "proxy-key")
    kwargs = endpoint_kwargs("http://litellm-proxy.lan:4000")
    assert kwargs["api_key"] == "proxy-key"
