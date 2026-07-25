"""4.4 prompt-caching layout: the system message keeps a byte-stable static
prefix (system prompt + reply contract) with all per-request context after it,
and Anthropic-family models get an explicit cache_control breakpoint."""

from __future__ import annotations

from kenzy.llm import llm as llm_app


def test_plain_provider_gets_string_with_static_head_first(monkeypatch):
    monkeypatch.setattr(llm_app, "_model", "gpt-4o")
    msg = llm_app._system_message("STATIC-HEAD", "DYNAMIC-CTX")
    assert msg["role"] == "system"
    assert isinstance(msg["content"], str)
    assert msg["content"].startswith("STATIC-HEAD")
    assert msg["content"].index("STATIC-HEAD") < msg["content"].index("DYNAMIC-CTX")


def test_anthropic_gets_cache_control_on_static_block(monkeypatch):
    monkeypatch.setattr(llm_app, "_model", "claude-opus-4-8")
    msg = llm_app._system_message("STATIC-HEAD", "DYNAMIC-CTX")
    blocks = msg["content"]
    assert isinstance(blocks, list) and len(blocks) == 2
    assert blocks[0]["text"] == "STATIC-HEAD"
    assert blocks[0]["cache_control"] == {"type": "ephemeral"}
    assert blocks[1]["text"] == "DYNAMIC-CTX"
    assert "cache_control" not in blocks[1]


def test_breakpoint_detection_covers_prefixed_model_strings():
    assert llm_app._wants_cache_breakpoint("claude-opus-4-8")
    assert llm_app._wants_cache_breakpoint("anthropic/claude-sonnet-5")
    assert llm_app._wants_cache_breakpoint("bedrock/claude-x")
    assert not llm_app._wants_cache_breakpoint("gpt-4o")
    assert not llm_app._wants_cache_breakpoint("ollama/hermes3")


def test_clock_line_stays_out_of_the_static_head():
    # The cache-buster this layout exists to avoid: _build_context() carries a
    # minute-granularity clock, so it must never be part of the static head.
    ctx = llm_app._build_context()
    assert "Current date and time" in ctx  # it lives in the dynamic tail...
    assert "Current date and time" not in llm_app._JSON_INSTRUCTION
    assert "Current date and time" not in llm_app._system_prompt
