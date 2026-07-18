"""Tests for the 4.0.2 privacy slice: private-tier facts never enter a cloud
model's context injection or semantic consolidation (locality detection, the
context filter, the consolidation gate, and the operator opt-out)."""

from __future__ import annotations

import pytest

from kenzy.llm import llm as llm_app
from kenzy.llm import memory, memory_semantic
from kenzy.llm import skills as sk
from kenzy.llm.locality import model_is_local
from kenzy.llm.memory import MemoryStore


@pytest.fixture(autouse=True)
def _fresh_request_context():
    t_req = sk._request_ctx.set({})
    t_act = sk._actions.set([])
    yield
    sk._actions.reset(t_act)
    sk._request_ctx.reset(t_req)
    memory_semantic._MODEL.clear()  # configure() state must not leak cross-file


# ---------------------------------------------------------------------------
# Locality detection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "model,base_url,expect",
    [
        # No base_url: judged by model string.
        ("gpt-5.1", None, False),
        ("anthropic/claude-sonnet-5", None, False),
        ("gpt-4o", None, False),
        ("ollama/qwen3:8b", None, True),
        ("ollama_chat/llama3.1", None, True),
        # base_url: judged by host.
        ("anything", "http://localhost:11434", True),
        ("anything", "http://127.0.0.1:11434", True),
        ("anything", "http://192.168.1.20:11434", True),
        ("anything", "http://10.0.0.5:8000", True),
        ("anything", "http://172.16.0.9:8000", True),
        ("anything", "http://mouse.lan:11434", True),
        ("anything", "http://ollama-box:11434", True),  # bare LAN hostname
        ("anything", "https://openrouter.ai/api/v1", False),  # hosted proxy = leaves the house
        ("anything", "https://api.example.com", False),
        ("anything", "http://8.8.8.8:80", False),  # public IP
    ],
)
def test_model_is_local(model, base_url, expect):
    assert model_is_local(model, base_url) is expect


# ---------------------------------------------------------------------------
# Context injection filter
# ---------------------------------------------------------------------------


def _seed(tmp_path) -> MemoryStore:
    s = MemoryStore(tmp_path / "facts.jsonl")
    s.remember("john", "the gate code is 4312", tier=memory.TIER_PRIVATE)
    s.remember("john", "the wifi is BlueHouse", tier=memory.TIER_SHARED)
    return s


def _ctx():
    sk.begin_actions()
    sk.begin_request(
        {"person_id": "john", "speaker_tier": "recognized", "channel": "voice"}
    )


def test_cloud_model_excludes_private_from_context(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "_store", _seed(tmp_path))
    monkeypatch.setattr(llm_app, "_model", "gpt-5.1")
    monkeypatch.setattr(llm_app, "_base_url", None)
    monkeypatch.setattr(llm_app, "_private_to_cloud", False)
    _ctx()
    ctx = llm_app._memory_context("gate code wifi")
    assert "wifi is BlueHouse" in ctx  # shared still injects
    assert "gate code" not in ctx  # private withheld from the cloud


def test_local_model_injects_private(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "_store", _seed(tmp_path))
    monkeypatch.setattr(llm_app, "_model", "ollama/qwen3:8b")
    monkeypatch.setattr(llm_app, "_base_url", "http://127.0.0.1:11434")
    monkeypatch.setattr(llm_app, "_private_to_cloud", False)
    _ctx()
    ctx = llm_app._memory_context("gate code wifi")
    assert "gate code is 4312" in ctx


def test_opt_out_restores_cloud_injection(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "_store", _seed(tmp_path))
    monkeypatch.setattr(llm_app, "_model", "gpt-5.1")
    monkeypatch.setattr(llm_app, "_base_url", None)
    monkeypatch.setattr(llm_app, "_private_to_cloud", True)
    _ctx()
    ctx = llm_app._memory_context("gate code wifi")
    assert "gate code is 4312" in ctx


# ---------------------------------------------------------------------------
# Consolidation gate
# ---------------------------------------------------------------------------


async def test_consolidation_withholds_private_from_cloud(tmp_path, monkeypatch):
    s = MemoryStore(tmp_path / "facts.jsonl")
    monkeypatch.setattr(memory_semantic, "_state_path", lambda st: tmp_path / "state.json")
    # Two similar PRIVATE facts (would normally be a merge candidate pair)
    # and two similar shared facts.
    s.remember("john", "the gate code is 4312", tier=memory.TIER_PRIVATE)
    s.remember("john", "gate code: 4312", tier=memory.TIER_PRIVATE)
    s.remember("john", "the wifi is BlueHouse", tier=memory.TIER_SHARED)
    s.remember("john", "wifi network BlueHouse", tier=memory.TIER_SHARED)

    seen_prompts: list[str] = []

    async def fake_completion(kwargs):
        seen_prompts.append(str(kwargs.get("messages")))

        class R:
            class C:
                class M:
                    content = "[]"

                message = M()

            choices = [C()]

        return R()

    monkeypatch.setattr(
        memory_semantic.skill_registry, "acompletion_with_fallback", fake_completion
    )
    memory_semantic.configure("gpt-5.1", None, private_to_cloud=False)
    out = await memory_semantic.run_pass(s)
    assert out.get("private_withheld", 0) == 2 or "4312" not in "".join(seen_prompts)
    assert "4312" not in "".join(seen_prompts)  # the code never reached the cloud
    assert "BlueHouse" in "".join(seen_prompts)  # shared still consolidates

    # Withheld facts don't loop back: a second run has nothing pending.
    seen_prompts.clear()
    out2 = await memory_semantic.run_pass(s)
    assert out2 == {"pending": 0}


async def test_consolidation_local_model_sees_private(tmp_path, monkeypatch):
    s = MemoryStore(tmp_path / "facts.jsonl")
    monkeypatch.setattr(memory_semantic, "_state_path", lambda st: tmp_path / "state.json")
    s.remember("john", "the gate code is 4312", tier=memory.TIER_PRIVATE)
    s.remember("john", "gate code: 4312", tier=memory.TIER_PRIVATE)

    seen: list[str] = []

    async def fake_completion(kwargs):
        seen.append(str(kwargs.get("messages")))

        class R:
            class C:
                class M:
                    content = "[]"

                message = M()

            choices = [C()]

        return R()

    monkeypatch.setattr(
        memory_semantic.skill_registry, "acompletion_with_fallback", fake_completion
    )
    memory_semantic.configure("ollama/qwen3:8b", "http://127.0.0.1:11434", private_to_cloud=False)
    await memory_semantic.run_pass(s)
    assert "4312" in "".join(seen)  # local model may consolidate private facts
