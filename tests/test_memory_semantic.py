"""Incremental semantic consolidation: pending mark, neighbor scoping, decision
validation (the model can only supersede, never delete or cross a scope), and
the kicked-job write hook."""

from __future__ import annotations

import json

import pytest

from kenzy.llm import memory_semantic as sem
from kenzy.llm.memory import TIER_SHARED, MemoryStore


@pytest.fixture
def store(tmp_path):
    return MemoryStore(tmp_path / "facts.jsonl")


def _resp(payload):
    """A LiteLLM-shaped response carrying the given decisions JSON."""

    class Msg:
        content = json.dumps(payload)

    class Choice:
        message = Msg()

    class Resp:
        choices = [Choice()]

    return Resp()


# -- pending mark -------------------------------------------------------------


def test_pending_and_mark_roundtrip(store):
    a = store.remember("adam", "fact one")
    assert [f.id for f in sem.pending_facts(store, 0.0)] == [a.id]
    sem._save_mark(store, a.created)
    assert sem._load_mark(store) == a.created
    assert sem.pending_facts(store, a.created) == []
    b = store.remember("adam", "fact two")
    assert [f.id for f in sem.pending_facts(store, a.created)] == [b.id]


def test_missing_state_means_everything_pending(store):
    store.remember("adam", "old fact")
    assert sem._load_mark(store) == 0.0  # no file ⇒ mark 0 ⇒ all pending (idempotent)


# -- neighbor scoping ---------------------------------------------------------


def test_neighbors_stay_within_owner_and_tier(store):
    new = store.remember("adam", "the pool guy comes on Thursdays")
    store.remember("adam", "pool service is on Thursday")  # same scope — candidate
    store.remember("nicki", "the pool guy comes on Thursdays")  # other owner — never
    store.remember("adam", "the pool guy comes on Thursdays", tier=TIER_SHARED)  # other tier
    ns = sem.neighbors_for(store, new)
    assert all(n.owner == "adam" and n.tier == "private" for n in ns)
    assert any("pool service" in n.text for n in ns)
    assert len(ns) == 1


# -- decision validation + application ----------------------------------------


async def test_merge_supersedes_never_deletes(store, monkeypatch):
    old = store.remember("adam", "the pool guy comes on Thursdays")
    sem._save_mark(store, old.created)
    new = store.remember("adam", "pool service is on Thursday")

    async def fake_llm(kwargs, state=None):
        return _resp(
            {
                "decisions": [
                    {
                        "id": new.id,
                        "action": "merge",
                        "text": "Pool service comes on Thursdays",
                        "supersedes": [old.id, new.id],
                    }
                ]
            }
        )

    monkeypatch.setattr(sem.skill_registry, "acompletion_with_fallback", fake_llm)
    summary = await sem.run_pass(store)
    assert summary["merged"] == 1
    # One live fact (the consolidated wording); sources tombstoned, NOT deleted.
    live = store.recall("adam", "pool", limit=10)
    assert [f.text for f in live] == ["Pool service comes on Thursdays"]
    assert live[0].source == "consolidation"
    assert len(store) == 3  # all three physically present
    assert store.get_fact(old.id).superseded_by == live[0].id
    assert store.get_fact(new.id).superseded_by == live[0].id


async def test_update_supersedes_stale_neighbor(store, monkeypatch):
    old = store.remember("adam", "the plumber is Joe")
    sem._save_mark(store, old.created)
    new = store.remember("adam", "the plumber is Sam now")

    async def fake_llm(kwargs, state=None):
        return _resp({"decisions": [{"id": new.id, "action": "update", "supersedes": [old.id]}]})

    monkeypatch.setattr(sem.skill_registry, "acompletion_with_fallback", fake_llm)
    summary = await sem.run_pass(store)
    assert summary["updated"] == 1
    texts = [f.text for f in store.recall("adam", "plumber", limit=10)]
    assert texts == ["the plumber is Sam now"]
    assert store.get_fact(old.id).superseded_by == new.id


async def test_cross_scope_and_bogus_decisions_degrade_to_keep(store, monkeypatch):
    other = store.remember("nicki", "the pool guy comes on Thursdays")
    # A legit same-scope neighbor so the model actually gets consulted
    # (cross-owner facts are never even OFFERED as neighbors).
    mine = store.remember("adam", "the pool needs cleaning")
    sem._save_mark(store, max(other.created, mine.created))
    new = store.remember("adam", "pool service is on Thursday")

    async def fake_llm(kwargs, state=None):
        return _resp(
            {
                "decisions": [
                    # Cross-owner supersede — must be rejected.
                    {"id": new.id, "action": "merge", "text": "x", "supersedes": [other.id]},
                    # Unknown pending id — ignored.
                    {"id": "nope", "action": "keep"},
                ]
            }
        )

    monkeypatch.setattr(sem.skill_registry, "acompletion_with_fallback", fake_llm)
    summary = await sem.run_pass(store)
    assert summary["merged"] == 0 and summary["rejected"] >= 1
    # Nothing was harmed: both facts still live.
    assert store.get_fact(other.id).superseded_by is None
    assert store.get_fact(new.id).superseded_by is None


async def test_unparseable_output_keeps_everything(store, monkeypatch):
    old = store.remember("adam", "the pool guy comes on Thursdays")
    sem._save_mark(store, old.created)
    new = store.remember("adam", "pool service is on Thursday")

    class Msg:
        content = "So I think these are duplicates!"  # prose, not JSON

    class Choice:
        message = Msg()

    class Resp:
        choices = [Choice()]

    async def fake_llm(kwargs, state=None):
        return Resp()

    monkeypatch.setattr(sem.skill_registry, "acompletion_with_fallback", fake_llm)
    summary = await sem.run_pass(store)
    assert summary["kept"] == 1 and summary.get("merged", 0) == 0
    assert store.get_fact(new.id).superseded_by is None
    # The mark still advanced — the fact was processed (as keep), not stuck.
    assert sem.pending_facts(store, sem._load_mark(store)) == []


async def test_no_neighbors_skips_the_model_entirely(store, monkeypatch):
    called = []

    async def fake_llm(kwargs, state=None):  # pragma: no cover - must not run
        called.append(1)
        return _resp({"decisions": []})

    monkeypatch.setattr(sem.skill_registry, "acompletion_with_fallback", fake_llm)
    store.remember("adam", "an utterly unrelated thing")
    summary = await sem.run_pass(store)
    assert summary["kept"] == 1 and not called  # trivially distinct — no model cost


async def test_failed_model_leaves_facts_pending(store, monkeypatch):
    old = store.remember("adam", "the pool guy comes on Thursdays")
    sem._save_mark(store, old.created)
    store.remember("adam", "pool service is on Thursday")

    async def fake_llm(kwargs, state=None):
        raise RuntimeError("model down")

    monkeypatch.setattr(sem.skill_registry, "acompletion_with_fallback", fake_llm)
    with pytest.raises(RuntimeError):
        await sem.run_pass(store)  # the job records the failure; retry_after reschedules
    # Mark did NOT advance — the retry/backstop will reprocess.
    assert len(sem.pending_facts(store, sem._load_mark(store))) == 1


# -- the write hook (kick wiring) ---------------------------------------------


def test_write_hook_fires_and_never_breaks_writes(store):
    kicks = []
    store.on_write = lambda: kicks.append(1)
    store.remember("adam", "hook test")
    assert kicks == [1]
    store.on_write = lambda: (_ for _ in ()).throw(RuntimeError("scheduler down"))
    fact = store.remember("adam", "still stored")  # hook failure must not fail the write
    assert store.get_fact(fact.id) is not None
