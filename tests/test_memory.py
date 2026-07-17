"""F2 memory: the JSONL fact ledger (tiers-as-ACL, tolerant loading, atomic
persistence) and kenzy-llm's /memory wire contract."""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from kenzy.llm import memory
from kenzy.llm.memory import (
    TIER_PERSONAL,
    TIER_PRIVATE,
    TIER_SHARED,
    MemoryStore,
)


@pytest.fixture
def store(tmp_path):
    return MemoryStore(tmp_path / "facts.jsonl")


def _seed(store):
    a = store.remember("adam", "The gate code is 4312", tier=TIER_PRIVATE)
    b = store.remember("adam", "Adam's birthday is March 3rd", tier=TIER_PERSONAL)
    c = store.remember("nicki", "Trash day is Tuesday", tier=TIER_SHARED)
    d = store.remember("nicki", "Nicki's dentist is Dr. Patel", tier=TIER_PRIVATE)
    return a, b, c, d


# -- tiers are the ACL -------------------------------------------------------


def test_recall_scopes_by_tier(store):
    _seed(store)
    # Adam sees: his private, all personal-public, all shared — never Nicki's private.
    texts = {f.text for f in store.recall("adam", "", limit=10)}
    assert "The gate code is 4312" in texts
    assert "Adam's birthday is March 3rd" in texts
    assert "Trash day is Tuesday" in texts
    assert "Nicki's dentist is Dr. Patel" not in texts
    # Nicki sees her own private but not Adam's.
    texts = {f.text for f in store.recall("nicki", "", limit=10)}
    assert "Nicki's dentist is Dr. Patel" in texts
    assert "The gate code is 4312" not in texts
    assert "Adam's birthday is March 3rd" in texts  # personal-public is readable


def test_empty_asker_sees_nothing(store):
    """Fail closed: no person id (unrecognized voice) ⇒ no memory at all."""
    _seed(store)
    assert store.recall("", "gate code", limit=10) == []
    assert store.recall("", "", limit=10) == []


def test_recall_keyword_scoring(store):
    _seed(store)
    hits = store.recall("adam", "what is the gate code?")
    assert hits and hits[0].text == "The gate code is 4312"
    # Stopwords alone can't match anything.
    assert store.recall("adam", "what is the the of") == []


# -- forget / promote rights -------------------------------------------------


def test_forget_rights(store):
    a, b, c, d = _seed(store)
    assert store.forget("nicki", a.id) is False  # not hers, not shared
    assert store.forget("adam", a.id) is True  # own fact
    assert store.forget("adam", c.id) is True  # shared: household-writable
    assert store.forget("adam", d.id) is False  # someone else's private
    assert store.forget("", b.id) is False  # fail closed


def test_set_tier_owner_only(store):
    a, *_ = _seed(store)
    assert store.set_tier("nicki", a.id, TIER_SHARED) is None  # only the owner promotes
    fact = store.set_tier("adam", a.id, TIER_SHARED)
    assert fact is not None and fact.tier == TIER_SHARED
    # Now visible to Nicki.
    assert any(f.id == a.id for f in store.recall("nicki", "gate code"))
    with pytest.raises(ValueError):
        store.set_tier("adam", a.id, "secret")


def test_export_is_ownership_not_visibility(store):
    _seed(store)
    owned = {f.text for f in store.export("adam")}
    assert owned == {"The gate code is 4312", "Adam's birthday is March 3rd"}


# -- persistence: tolerant loading, atomic rewrites, no migration chain -------


def test_roundtrip_and_tolerant_load(tmp_path):
    path = tmp_path / "facts.jsonl"
    s1 = MemoryStore(path)
    kept = s1.remember("adam", "The Wi-Fi password is on the fridge")
    s1.remember("adam", "To be forgotten")
    # Corrupt line + an old/foreign record shape + junk — all must be survivable.
    with path.open("a") as f:
        f.write("{not json\n")
        f.write(json.dumps({"text": "orphan with no owner"}) + "\n")
        f.write(json.dumps({"owner": "adam", "text": "old shape, no id/tier/v"}) + "\n")
    s2 = MemoryStore(path)
    texts = {f.text for f in s2.recall("adam", "", limit=10)}
    assert kept.text in texts
    assert "old shape, no id/tier/v" in texts  # up-converted (defaults: private, new id)
    assert "orphan with no owner" not in texts  # unowned records are dropped
    # A mutation triggers the atomic rewrite; the corrupt line is gone for good.
    victim = next(f for f in s2.recall("adam", "forgotten"))
    assert s2.forget("adam", victim.id)
    s3 = MemoryStore(path)
    assert "{not json" not in path.read_text()
    assert kept.text in {f.text for f in s3.recall("adam", "", limit=10)}


def test_unknown_tier_degrades_to_private(tmp_path):
    path = tmp_path / "facts.jsonl"
    path.write_text(json.dumps({"owner": "adam", "text": "weird tier", "tier": "sudo"}) + "\n")
    s = MemoryStore(path)
    (fact,) = s.recall("adam", "weird")
    assert fact.tier == TIER_PRIVATE  # most restrictive wins
    assert s.recall("nicki", "weird") == []


def test_expired_and_superseded_are_dead(store):
    a, *_ = _seed(store)
    a.expires = time.time() - 1
    assert not any(f.id == a.id for f in store.recall("adam", "gate code"))
    b = store.remember("adam", "The gate code is 9999")
    b.superseded_by = "xyz"
    assert not any(f.id == b.id for f in store.recall("adam", "gate code"))


def test_remember_validation(store):
    with pytest.raises(ValueError):
        store.remember("", "no owner")
    with pytest.raises(ValueError):
        store.remember("adam", "   ")
    with pytest.raises(ValueError):
        store.remember("adam", "x", tier="sudo")


# -- the /memory wire contract -----------------------------------------------


@pytest.fixture
def client(tmp_path):
    from kenzy.llm import llm as llm_app

    memory.init_store(tmp_path / "facts.jsonl")
    try:
        yield TestClient(llm_app.app)
    finally:
        memory._store = None


def test_memory_endpoints(client):
    r = client.post(
        "/memory/remember",
        json={"owner": "adam", "text": "The spare key is in the shed", "tier": "shared"},
    )
    assert r.status_code == 200
    fid = r.json()["fact"]["id"]

    r = client.get("/memory/recall", params={"asker": "nicki", "q": "spare key"})
    assert [f["id"] for f in r.json()["facts"]] == [fid]  # shared: visible to anyone

    r = client.get("/memory/export", params={"person": "adam"})
    assert [f["id"] for f in r.json()["facts"]] == [fid]

    r = client.get("/memory")
    assert r.json()["count"] == 1

    assert client.post("/memory/forget", json={"asker": "x", "id": "nope"}).status_code == 404
    assert client.post("/memory/forget", json={"asker": "nicki", "id": fid}).status_code == 200
    assert client.get("/memory").json()["count"] == 0

    # Validation surfaces as 400, not a 500.
    r = client.post("/memory/remember", json={"owner": "", "text": "x"})
    assert r.status_code == 400


def test_memory_disabled_is_503(tmp_path):
    from kenzy.llm import llm as llm_app

    memory._store = None
    c = TestClient(llm_app.app)
    assert c.get("/memory").status_code == 503


# -- F2.1 short-term per-person context + F2.5 auto-injection ------------------


def test_short_term_context_rolls_and_expires(monkeypatch):
    st = memory.ShortTermContext()
    st.add("", "ignored", "no person, no trail")  # F1.3: unrecognized leaves nothing
    assert st.recent("") == []
    st.add("adam", "u1", "a1")
    st.add("adam", "u2", "a2")
    assert st.recent("adam") == [("u1", "a1"), ("u2", "a2")]
    assert st.recent("nicki") == []  # strictly per-person
    # Expiry: age the entries past MAX_AGE.
    for e in st._by_person["adam"]:
        e.ts -= memory.ShortTermContext.MAX_AGE + 1
    assert st.recent("adam") == []
    # Rolling cap.
    for i in range(memory.ShortTermContext.MAX_EXCHANGES + 5):
        st.add("adam", f"u{i}", f"a{i}")
    assert len(st._by_person["adam"]) == memory.ShortTermContext.MAX_EXCHANGES


def test_memory_context_injection(tmp_path):
    from kenzy.llm import llm as llm_app
    from kenzy.llm import skills as reg

    store = memory.init_store(tmp_path / "facts.jsonl")
    try:
        store.remember("adam", "The gate code is 4312")
        store.remember("nicki", "Nicki's pin is 7777")  # private to someone else
        llm_app._short_term.add("adam", "how are you", "Doing great.")

        reg.begin_request({"person_id": "adam", "speaker_tier": "recognized"})
        block = llm_app._memory_context("what's the gate code?")
        assert "4312" in block  # relevant fact injected
        assert "7777" not in block  # never someone else's private fact
        assert "how are you" in block  # short-term exchange rides along

        # Unrecognized voices get NO memory block at all (F1.3 contract).
        reg.begin_request({"person_id": None, "speaker_tier": "unknown"})
        assert llm_app._memory_context("what's the gate code?") == ""
        reg.begin_request({"person_id": "adam", "speaker_tier": "unknown"})
        assert llm_app._memory_context("what's the gate code?") == ""
    finally:
        memory._store = None
        llm_app._short_term = memory.ShortTermContext()


# -- history tag & filter: private echoes never replay to another voice --------


def test_history_private_turns_filtered_by_viewer():
    from kenzy.llm.llm import ConversationHistory

    h = ConversationHistory()
    h.add("office", "Adam", "what's the gate code?", "It's 4312.", private_to="adam")
    h.add("office", "Adam", "what time is it?", "3pm.")  # untagged — public chat

    # The owner sees everything.
    msgs = h.get_messages("office", "adam")
    assert any("4312" in m["content"] for m in msgs)
    # A different person and an unrecognized voice never see the private echo.
    for viewer in ("nicki", None):
        msgs = h.get_messages("office", viewer)
        assert not any("4312" in m["content"] for m in msgs)
        assert any("3pm" in m["content"] for m in msgs)  # public turns still flow


def test_private_touch_marker(tmp_path):
    store = MemoryStore(tmp_path / "facts.jsonl")
    private = store.remember("adam", "my pin is 1234")
    shared = store.remember("adam", "trash day is Tuesday", tier=TIER_SHARED)

    memory.begin_touch()
    memory.mark_if_sensitive([shared])
    assert memory.private_touched() is False  # shared facts don't taint the turn
    memory.mark_if_sensitive([shared, private])
    assert memory.private_touched() is True

    memory.begin_touch()  # next request starts clean
    assert memory.private_touched() is False


async def test_fast_recall_marks_private(tmp_path):
    from kenzy.llm import skills as reg
    from kenzy.llm.builtin_skills import memory_skill as ms

    memory.init_store(tmp_path / "facts.jsonl")
    try:
        reg.begin_request({"person_id": "adam", "speaker_tier": "recognized"})
        memory.begin_touch()
        await ms.remember("the gate code is 4312")  # private write
        assert memory.private_touched() is True

        memory.begin_touch()
        res = await ms.fast_memory("What do you know about the gate code?", "o", None)
        assert res.is_handled and memory.private_touched() is True

        # A shared-only exchange leaves the turn public.
        await ms.remember("trash day is Tuesday", shared=True)
        memory.begin_touch()
        res = await ms.fast_memory("What do you know about trash day?", "o", None)
        assert res.is_handled and memory.private_touched() is False
    finally:
        memory._store = None


# -- admin erase (the dashboard manager path) ---------------------------------


def test_admin_erase_ignores_ownership(store):
    a, b, c, d = _seed(store)
    assert store.erase(d.id) is True  # any fact, no asker scoping
    assert store.erase(d.id) is False  # already gone


def test_forget_endpoint_admin_mode(client):
    r = client.post("/memory/remember", json={"owner": "adam", "text": "secret thing"})
    fid = r.json()["fact"]["id"]
    # No asker ⇒ admin erase (the surface is token-gated; the dashboard uses this).
    assert client.post("/memory/forget", json={"id": fid}).status_code == 200
    assert client.get("/memory").json()["count"] == 0


# -- dashboard Memory manager (F7.2 thin) -------------------------------------


async def test_dashboard_memory_state_and_forget(monkeypatch):
    from kenzy.server.dashboard import Dashboard, DashboardConfig
    from kenzy.server.server import AudioServer

    server = AudioServer({})
    monkeypatch.setattr(
        server, "list_people", lambda: [{"id": "adam", "name": "Adam", "voiceprints": ["adam"]}]
    )
    dash = Dashboard(server, {}, DashboardConfig(controls=True))

    calls: list[tuple[str, str]] = []

    async def fake_req(method, sub_path, payload=None):
        calls.append((method, sub_path))
        if method == "GET":
            return {"facts": [{"id": "f1", "owner": "adam", "tier": "private", "text": "x"}]}
        return {"status": "ok"}

    monkeypatch.setattr(dash, "_llm_memory_request", fake_req)
    state = await dash._memory_state()
    assert state["reachable"] is True
    assert state["facts"][0]["owner_name"] == "Adam"  # person id joined to display name

    ok, err = await dash._forget_memory("f1")
    assert ok and err is None
    assert ("POST", "/forget") in calls

    async def unreachable(method, sub_path, payload=None):
        return None

    monkeypatch.setattr(dash, "_llm_memory_request", unreachable)
    state = await dash._memory_state()
    assert state["reachable"] is False
    ok, err = await dash._forget_memory("f1")
    assert not ok and "not reachable" in err


# -- normalized recall tokens (field findings: "wifi" vs "Wi-Fi"; stopwords) ---


def test_recall_normalizes_punctuated_words(store):
    store.remember("adam", "The Wi-Fi password is on the fridge")
    # All three spellings find the fact.
    for q in ("wifi", "wi-fi", "wi fi"):
        assert store.recall("adam", q), f"query {q!r} should match"
    # And a punctuated query finds a plain fact.
    store.remember("adam", "the wifi extender is upstairs")
    assert len(store.recall("adam", "wi-fi", limit=10)) == 2


def test_recall_stopwords_carry_no_signal(store):
    store.remember("adam", "The trash goes out on Tuesday")
    # Classic short words alone match nothing (they'd cover every fact).
    for q in ("is", "on", "the", "a", "an", "in", "is on the"):
        assert store.recall("adam", q) == [], f"query {q!r} should match nothing"
    # But they don't poison a real query either.
    assert store.recall("adam", "what day is the trash on?")
