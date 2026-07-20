"""Presence read-on-demand (4.2): HA person states through the ha_user link,
name gating on the fast path, and honest unlinked/unconfigured answers."""

from __future__ import annotations

import pytest

from kenzy.llm import skills as sk
from kenzy.llm.builtin_skills import presence

PEOPLE = [
    {"id": "john", "name": "John", "voiceprints": ["john"], "ha_user": "person.john"},
    {"id": "nicki", "name": "Nicki", "voiceprints": ["nicki"], "ha_user": "person.nicki"},
    {"id": "guest", "name": "Guest", "voiceprints": [], "ha_user": None},
]


@pytest.fixture(autouse=True)
def _ctx(monkeypatch):
    monkeypatch.setenv("HA_API_KEY", "x")
    # Token-reset both contextvars: a SYNC fixture runs in the thread's base
    # context, so an unreset set() would leak into later test files (async
    # test bodies are insulated by task context-copies; fixtures aren't).
    tok_a = sk.begin_actions()
    tok_r = sk._request_ctx.set(
        {"channel": "voice", "person_id": "john", "speaker_tier": "recognized",
         "people": PEOPLE}  # fmt: skip
    )
    yield
    sk._request_ctx.reset(tok_r)
    sk._actions.reset(tok_a)


def _states(monkeypatch, mapping):
    async def state_of(entity_id):
        return mapping.get(entity_id, "")

    monkeypatch.setattr(presence, "_state_of", state_of)


async def test_is_home_and_zones(monkeypatch):
    _states(monkeypatch, {"person.nicki": "home", "person.john": "Work"})
    r = await presence.fast_presence("is Nicki home?", "office", None)
    assert r.is_handled and r.text == "Nicki is home."
    r = await presence.fast_presence("where is john", "office", None)
    assert r.text == "John is at Work."
    _states(monkeypatch, {"person.nicki": "not_home"})
    r = await presence.fast_presence("Is Nicki home", "office", None)
    assert r.text == "Nicki is away."


async def test_whos_home_summary(monkeypatch):
    _states(monkeypatch, {"person.john": "home", "person.nicki": "not_home"})
    r = await presence.fast_presence("who's home?", "office", None)
    assert r.is_handled and "John is home" in r.text and "Nicki" in r.text
    _states(monkeypatch, {"person.john": "home", "person.nicki": "home"})
    r = await presence.fast_presence("is anyone home", "office", None)
    assert "Everyone's home" in r.text


async def test_unknown_name_misses_to_llm(monkeypatch):
    # "where is my phone" must not be swallowed by the presence intent.
    _states(monkeypatch, {})
    r = await presence.fast_presence("where is my phone", "office", None)
    assert r.status == "miss"


async def test_unlinked_person_honest(monkeypatch):
    _states(monkeypatch, {})
    out = await presence.person_presence("Guest")
    assert "isn't linked" in out


async def test_unconfigured_misses(monkeypatch):
    monkeypatch.delenv("HA_API_KEY", raising=False)
    r = await presence.fast_presence("is Nicki home?", "office", None)
    assert r.status == "miss"
    out = await presence.person_presence("Nicki")
    assert "Home Assistant" in out
