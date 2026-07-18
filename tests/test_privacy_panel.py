"""Tests for the F7.4 privacy panel: memory erase_person, the person-export
composer, revoke-all composition, and the memory opt-out flag end to end."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from kenzy.llm import llm as llm_app
from kenzy.llm import memory
from kenzy.llm import skills as sk
from kenzy.llm.builtin_skills import memory_skill
from kenzy.llm.memory import MemoryStore
from kenzy.server.dashboard import Dashboard, DashboardConfig
from kenzy.server.server import TranscribingServer


@pytest.fixture(autouse=True)
def _fresh_request_context():
    """Reset the request/action contextvars after each test — a "recognized"
    context set here must not leak into tests that rely on running outside a
    request scope."""
    t_req = sk._request_ctx.set({})
    t_act = sk._actions.set([])
    yield
    sk._actions.reset(t_act)
    sk._request_ctx.reset(t_req)


def _store(tmp_path) -> MemoryStore:
    return MemoryStore(tmp_path / "facts.jsonl")


# ---------------------------------------------------------------------------
# MemoryStore.erase_person
# ---------------------------------------------------------------------------


def test_erase_person_spares_shared_by_default(tmp_path):
    s = _store(tmp_path)
    s.remember("guest", "my locker code is 12", tier=memory.TIER_PRIVATE)
    s.remember("guest", "guest likes tea", tier=memory.TIER_PERSONAL)
    shared = s.remember("guest", "the gate code is 4312", tier=memory.TIER_SHARED)
    s.remember("john", "john's fact", tier=memory.TIER_PRIVATE)

    assert s.erase_person("guest") == 2
    left = {f.text for f in s.all_facts()}
    assert left == {"the gate code is 4312", "john's fact"}
    # Reload from disk — the rewrite persisted.
    left2 = {f.text for f in MemoryStore(tmp_path / "facts.jsonl").all_facts()}
    assert left2 == left
    assert s.get_fact(shared.id) is not None


def test_erase_person_include_shared(tmp_path):
    s = _store(tmp_path)
    s.remember("guest", "private thing", tier=memory.TIER_PRIVATE)
    s.remember("guest", "shared thing", tier=memory.TIER_SHARED)
    assert s.erase_person("guest", include_shared=True) == 2
    assert s.all_facts() == []


def test_erase_person_endpoint(tmp_path, monkeypatch):
    s = _store(tmp_path)
    s.remember("guest", "a fact", tier=memory.TIER_PRIVATE)
    monkeypatch.setattr(memory, "_store", s)
    client = TestClient(llm_app.app)
    r = client.post("/memory/erase_person", json={"person": "guest"})
    assert r.status_code == 200 and r.json() == {"erased": 1}
    r = client.post("/memory/erase_person", json={"person": "   "})
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Memory opt-out (the "don't remember me" flag)
# ---------------------------------------------------------------------------


def _ctx(opt_out: bool):
    sk.begin_actions()
    sk.begin_request(
        {
            "person_id": "john",
            "speaker_tier": "recognized",
            "memory_opt_out": opt_out,
            "channel": "voice",
        }
    )


async def test_opt_out_blocks_writes_and_reads(tmp_path, monkeypatch):
    s = _store(tmp_path)
    monkeypatch.setattr(memory, "_store", s)
    _ctx(opt_out=True)
    reply = await memory_skill.remember("the safe code is 99")
    assert "turned off for you at your request" in reply
    assert s.all_facts() == []  # nothing written

    s.remember("john", "an old fact", tier=memory.TIER_PRIVATE)
    reply = await memory_skill.recall("old fact")
    assert "turned off for you at your request" in reply

    res = await memory_skill.fast_memory("remember that milk is low", None, "John")
    assert res.is_handled and "turned off for you" in res.text
    assert len(s.all_facts()) == 1  # still just the pre-seeded fact


async def test_opt_out_off_behaves_normally(tmp_path, monkeypatch):
    s = _store(tmp_path)
    monkeypatch.setattr(memory, "_store", s)
    _ctx(opt_out=False)
    reply = await memory_skill.remember("the plant needs water on Fridays")
    assert reply.startswith("Remembered")
    assert len(s.all_facts()) == 1


def test_memory_context_empty_when_opted_out(tmp_path, monkeypatch):
    s = _store(tmp_path)
    s.remember("john", "john hates cilantro", tier=memory.TIER_PRIVATE)
    monkeypatch.setattr(memory, "_store", s)
    _ctx(opt_out=True)
    assert llm_app._memory_context("cilantro") == ""
    _ctx(opt_out=False)
    assert "cilantro" in llm_app._memory_context("cilantro")


# ---------------------------------------------------------------------------
# Person record: flag persists; server threads it into the payload
# ---------------------------------------------------------------------------


def test_person_opt_out_persists(tmp_path, monkeypatch):
    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    server = TranscribingServer({})
    pid = server.save_person("", "Guest", [], memory_opt_out=True)
    assert server._people.get(pid).memory_opt_out is True
    # Omitted ⇒ preserved.
    server.save_person(pid, "Guest", [])
    assert server._people.get(pid).memory_opt_out is True
    # Survives a reload.
    server2 = TranscribingServer({})
    assert server2._people.get(pid).memory_opt_out is True

    from kenzy.server.people import Identity

    ident = Identity(display="Guest", tier="recognized", confidence=1.0, person_id=pid)
    assert server._person_memory_opt_out(ident) is True
    assert server._person_memory_opt_out(None) is False


# ---------------------------------------------------------------------------
# Dashboard: export composer + revoke-all
# ---------------------------------------------------------------------------


def _dash(tmp_path, monkeypatch):
    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    server = TranscribingServer({})
    return Dashboard(server, {}, DashboardConfig()), server


async def test_person_export_composes(tmp_path, monkeypatch):
    dash, server = _dash(tmp_path, monkeypatch)
    pid = server.save_person("", "Guest", ["guestvoice"])

    async def fake_speakers(method, path, payload=None):
        return (200, {"speakers": [{"name": "guestvoice", "samples": 5}]})

    async def fake_memory(method, sub_path, payload=None):
        return {"facts": [{"id": "f1", "text": "a fact", "tier": "private"}]}

    monkeypatch.setattr(dash, "_speaker_request", fake_speakers)
    monkeypatch.setattr(dash, "_llm_memory_request", fake_memory)
    body = await dash._person_export(pid)
    assert body["person"]["name"] == "Guest"
    assert body["voice_profiles"] == [{"name": "guestvoice", "samples": 5}]
    assert body["memory"]["available"] and len(body["memory"]["facts"]) == 1
    assert await dash._person_export("nobody") is None


async def test_revoke_person_composes_and_aborts_without_memory(tmp_path, monkeypatch):
    dash, server = _dash(tmp_path, monkeypatch)
    pid = server.save_person("", "Guest", ["guestvoice"])

    # Memory unreachable ⇒ abort, nothing deleted.
    async def mem_down(method, sub_path, payload=None):
        return 0, None

    monkeypatch.setattr(dash, "_llm_memory_status", mem_down)
    ok, err = await dash._revoke_person(pid)
    assert not ok and "nothing was removed" in err
    assert server._people.get(pid) is not None

    # A voiceprint delete failure keeps the record (retryable — the voice
    # must not outlive the person).
    async def mem_ok(method, sub_path, payload=None):
        erased.append(payload)
        return 200, {"erased": 2}

    erased: list[dict] = []

    async def del_fail(name):
        return False, "speaker down"

    monkeypatch.setattr(dash, "_llm_memory_status", mem_ok)
    monkeypatch.setattr(dash, "_delete_speaker", del_fail)
    ok, err = await dash._revoke_person(pid)
    assert not ok and "run Remove again" in err
    assert server._people.get(pid) is not None

    # Full path: memory ok, speaker deletes ok ⇒ person gone.
    deleted: list[str] = []

    async def del_speaker(name):
        deleted.append(name)
        return True, None

    monkeypatch.setattr(dash, "_delete_speaker", del_speaker)
    ok, err = await dash._revoke_person(pid)
    assert ok and err is None
    assert deleted == ["guestvoice"]
    assert server._people.get(pid) is None


async def test_revoke_person_proceeds_when_memory_disabled(tmp_path, monkeypatch):
    # memory.enabled: false answers 503 — that's "no ledger to erase", not an
    # outage; voice + record removal must still work.
    dash, server = _dash(tmp_path, monkeypatch)
    pid = server.save_person("", "Guest", [])

    async def mem_disabled(method, sub_path, payload=None):
        return 503, None

    monkeypatch.setattr(dash, "_llm_memory_status", mem_disabled)
    ok, err = await dash._revoke_person(pid)
    assert ok and err is None
    assert server._people.get(pid) is None
