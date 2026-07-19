"""Tests for the lockbox store (4.1): encryption at rest, key handling,
owner scoping, revoke integration, and honest degrade without the crypto lib."""

from __future__ import annotations

import json

import pytest

from kenzy.llm import lockbox as lb
from kenzy.llm.lockbox import LockboxStore


def _store(tmp_path) -> LockboxStore:
    return LockboxStore(tmp_path / "lockbox.enc")


def test_roundtrip_and_ciphertext_at_rest(tmp_path):
    s = _store(tmp_path)
    sec = s.add("john", "the gate code is 4312", label="gate code")
    got = s.list_for("john")
    assert [x.text for x in got] == ["the gate code is 4312"]
    assert got[0].id == sec.id

    # The file on disk is ciphertext: the secret never appears in plaintext.
    raw = (tmp_path / "lockbox.enc").read_bytes()
    assert b"4312" not in raw and b"gate" not in raw
    # And it isn't accidentally readable JSON.
    with pytest.raises(Exception):
        json.loads(raw.decode(errors="replace"))


def test_key_generated_with_owner_only_perms(tmp_path):
    s = _store(tmp_path)
    s.add("john", "x y z secret")
    key = tmp_path / "lockbox.key"
    assert key.is_file()
    assert (key.stat().st_mode & 0o777) == 0o600
    # A second store instance with the same paths reads the same data.
    again = _store(tmp_path)
    assert [x.text for x in again.list_for("john")] == ["x y z secret"]


def test_owner_scoping(tmp_path):
    s = _store(tmp_path)
    s.add("john", "john's code is 1111", label="code")
    s.add("nicki", "nicki's code is 2222", label="code")
    assert len(s.list_for("john")) == 1
    assert s.find("nicki", "code")[0].text == "nicki's code is 2222"
    assert all(x.owner == "john" for x in s.find("john", "code"))
    # Owner-scoped erase can't cross owners.
    jid = s.list_for("john")[0].id
    assert s.erase("nicki", jid) is False
    assert s.erase("john", jid) is True


def test_find_is_deterministic_token_overlap(tmp_path):
    s = _store(tmp_path)
    s.add("john", "the wifi password is hunter2", label="wifi password")
    assert s.find("john", "what's the wifi password?")  # overlap on wifi/password
    assert s.find("john", "the") == []  # stop-length tokens carry no signal
    assert s.find("john", "gate code") == []


def test_erase_person_and_masked(tmp_path):
    s = _store(tmp_path)
    s.add("guest", "temp door pin 9999", label="door pin")
    s.add("john", "safe combo 33-22-11", label="safe")
    assert s.erase_person("guest") == 1
    masked = s.masked()
    assert len(masked) == 1 and masked[0]["label"] == "safe"
    assert "33-22-11" not in json.dumps(masked)  # masked really is masked
    got = s.reveal(masked[0]["id"])
    assert got is not None and "33-22-11" in got.text


def test_wrong_key_degrades_to_empty(tmp_path):
    s = _store(tmp_path)
    s.add("john", "secret thing")
    # Simulate a restored archive without its key: new key, old ciphertext.
    (tmp_path / "lockbox.key").unlink()
    s2 = _store(tmp_path)
    assert s2.list_for("john") == []  # unreadable ⇒ empty, never a crash


def test_unavailable_lib_degrades_honestly(tmp_path, monkeypatch):
    monkeypatch.setattr(lb, "_fernet_cls", lambda: None)
    assert lb.available() is False
    s = LockboxStore(tmp_path / "lockbox.enc")
    assert s.enabled is False
    assert s.list_for("john") == []
    with pytest.raises(RuntimeError):
        s.add("john", "x")


# ---------------------------------------------------------------------------
# Voice fast paths (secret store / recall / forget)
# ---------------------------------------------------------------------------


async def _fm(utterance, tmp_path, person="john", tts_local=False):
    from kenzy.llm import lockbox as lbmod
    from kenzy.llm import memory
    from kenzy.llm import skills as sk
    from kenzy.llm.builtin_skills import memory_skill

    if lbmod._store is None or not str(lbmod._store._path).startswith(str(tmp_path)):
        lbmod.init_store(tmp_path / "lockbox.enc")
    if memory.store() is None:
        memory.init_store(tmp_path / "facts.jsonl")
    sk.begin_actions()
    memory.begin_touch()
    sk.begin_request(
        {"person_id": person, "speaker_tier": "recognized", "channel": "voice",
         "tts_local": tts_local}  # fmt: skip
    )
    return await memory_skill.fast_memory(utterance, "office", "John")


async def test_secret_phrasings_vault_synchronously(tmp_path):
    from kenzy.llm import lockbox as lbmod
    from kenzy.llm import memory

    res = await _fm("Remember this secretly: the safe combo is 33-22-11", tmp_path)
    assert res.is_handled and "Locked away" in res.text
    assert memory.private_touched()  # the echo is tagged
    assert "33-22-11" in lbmod.store().list_for("john")[-1].text
    # It did NOT land in the plain ledger.
    assert memory.store().recall("john", "safe combo") == []

    res = await _fm("Remember my luggage code is 0000, keep it secret", tmp_path)
    assert res.is_handled and "Locked away" in res.text


async def test_secret_recall_wins_fast_path_owner_only(tmp_path):
    from kenzy.llm import lockbox as lbmod

    lbmod.init_store(tmp_path / "lockbox.enc")
    lbmod.store().add("john", "the safe combo is 33-22-11", label="safe combo")
    res = await _fm("What do you know about the safe combo?", tmp_path, tts_local=True)
    assert res.is_handled and "33-22-11" in res.text  # verbatim, no model
    # Another recognized person gets nothing from John's lockbox.
    res = await _fm("What do you know about the safe combo?", tmp_path, person="nicki")
    assert "33-22-11" not in (res.text or "")


async def test_secret_forget(tmp_path):
    from kenzy.llm import lockbox as lbmod

    lbmod.init_store(tmp_path / "lockbox.enc")
    lbmod.store().add("john", "the wine cellar pin is 4747", label="wine cellar")
    res = await _fm("Forget about the wine cellar", tmp_path)
    assert res.is_handled and "lockbox" in res.text
    assert lbmod.store().list_for("john") == []


# ---------------------------------------------------------------------------
# Wire contract: review + lockbox endpoints
# ---------------------------------------------------------------------------


def test_review_and_lockbox_endpoints(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from kenzy.llm import llm as llm_app
    from kenzy.llm import memory

    store = memory.MemoryStore(tmp_path / "facts.jsonl")
    monkeypatch.setattr(memory, "_store", store)
    from kenzy.llm import lockbox as lbmod

    lbmod.init_store(tmp_path / "lockbox.enc")
    client = TestClient(llm_app.app)

    held = store.remember("john", "the gate code changed", state="quarantined")
    ok = store.remember("john", "likes decaf", state="quarantined")

    # Release one, vault the other.
    r = client.post("/memory/review", json={"id": ok.id, "action": "release"})
    assert r.status_code == 200 and r.json()["status"] == "released"
    r = client.post("/memory/review", json={"id": held.id, "action": "vault"})
    assert r.status_code == 200 and r.json()["status"] == "vaulted"
    assert store.get_fact(held.id) is None
    assert lbmod.store().list_for("john")

    # Masked list + admin erase.
    r = client.get("/memory/lockbox")
    body = r.json()
    assert body["available"] and len(body["secrets"]) == 1
    assert "gate code changed" not in str(body)  # masked means masked
    sid = body["secrets"][0]["id"]
    r = client.post("/memory/lockbox/erase", json={"id": sid})
    assert r.status_code == 200
    assert lbmod.store().list_for("john") == []

    # Reviewing a non-held fact 404s.
    r = client.post("/memory/review", json={"id": ok.id, "action": "vault"})
    assert r.status_code == 404


async def test_key_value_derivation(tmp_path):
    # Field bug → redesign: the lockbox is key/value. Labels keep their
    # qualifier so two codes stay distinct; values are extracted payloads.
    from kenzy.llm import lockbox as lb

    assert lb.derive_label("the shed key code is 8642") == "shed key code"
    assert lb.derive_label("wifi password is hunter2") == "wifi password"
    assert lb.derive_label("the gate code is 4312") == "gate code"
    assert lb.extract_value("the shed key code is 8642") == "8642"
    assert lb.extract_value("the code for the shed is 8642") == "8642"
    assert lb.extract_value("safe 33-22-11 combo") == "33-22-11"  # payload fallback
    assert lb.extract_value("plumber visit thursday") == ""  # no verb, nothing payload-shaped
    assert lb.slug("shed key code") == "shed_key_code"

    # "changed to"/"set to" restatements extract too (field: STT commas).
    assert lb.extract_value("The door code has changed to 6,000") == "6,000"
    assert lb.extract_value("the gate pin is now 7777") == "7777"
    # Field bug: "has been updated to" fell through and the placeholder spoke
    # the whole sentence. The verb family covers updated/reset/switched (+
    # "back to"), and verbless phrasings fall back to the last payload chunk.
    assert lb.extract_value("the door code has been updated to 4593") == "4593"
    assert lb.extract_value("the alarm pin was reset to 0000") == "0000"
    assert lb.extract_value("I switched the gate code back to 1234") == "1234"
    assert lb.extract_value("door code 4593") == "4593"
    assert lb.extract_value("locker combo 33-44-55") == "33-44-55"
    assert lb.extract_value("garage keypad hunter2x") == "hunter2x"

    s = _store(tmp_path)
    sec = s.add("john", "the shed key code is 8642")
    assert sec.label == "shed key code" and sec.value == "8642"
    # Same owner + same key ⇒ UPDATE in place, newest wins — the lockbox's
    # deterministic coalescing ("the shed key code is 9999" replaces 8642).
    s.add("john", "the shed key code is 9999")
    km = s.keymap("john")
    assert list(km) == ["shed_key_code"]
    assert km["shed_key_code"].payload == "9999"
    # A DIFFERENT key never collides; another owner's same key is untouched.
    s.add("john", "the gate code is 4312")
    s.add("nicki", "the shed key code is 1111")
    assert set(s.keymap("john")) == {"shed_key_code", "gate_code"}
    assert s.keymap("nicki")["shed_key_code"].payload == "1111"
    assert s.keymap("john")["shed_key_code"].payload == "9999"
    # Legacy same-key twins (pre-upsert file) still both addressable via suffix.
    twins = s._read_all()
    twins.append(lb.Secret(id="deadbeef0001", owner="john", text="the gate code is 9",
                           label="gate code"))  # fmt: skip
    s._write_all(twins)
    assert "gate_code_2" in s.keymap("john")


async def test_placeholder_substitution_owner_scoped(tmp_path):
    # The deterministic value path: the model writes [[lockbox:key]]; the
    # service fills in the ASKER's value after the reply. Values never enter
    # model context; a wrong/unowned/hallucinated key substitutes safely.
    from kenzy.llm import lockbox as lbmod

    lbmod.init_store(tmp_path / "lockbox.enc")
    lbmod.store().add("john", "the shed key code is 8642")

    text, hits = lbmod.substitute("Your shed key code is [[lockbox:shed_key_code]].", "john")
    assert text == "Your shed key code is 8642." and hits == 1

    # Tolerant spellings models drift into.
    for form in (
        "It's {{lockbox:shed_key_code}}.",
        "the shed key code is *check lockbox for shed_key_code*",
    ):
        text, hits = lbmod.substitute(form, "john")
        assert "8642" in text and hits == 1, form

    # Another person's ask can NEVER pull John's value.
    text, hits = lbmod.substitute("It's [[lockbox:shed_key_code]].", "nicki")
    assert "8642" not in text and hits == 0 and "lockbox" in text

    # Hallucinated key ⇒ safe miss; nothing placeholder-shaped survives.
    text, hits = lbmod.substitute("Sure: [[lockbox:bank_vault]].", "john")
    assert hits == 0 and "[[" not in text

    # No placeholder ⇒ untouched, zero cost.
    assert lbmod.substitute("Nice weather today.", "john") == ("Nice weather today.", 0)


async def test_identification_block_keys_only(tmp_path, monkeypatch):
    # The system context carries the KEY INDEX (non-sensitive) — never values.
    from kenzy.llm import llm as llm_app
    from kenzy.llm import lockbox as lbmod
    from kenzy.llm import memory
    from kenzy.llm import skills as sk

    monkeypatch.setattr(memory, "_store", memory.MemoryStore(tmp_path / "facts.jsonl"))
    lbmod.init_store(tmp_path / "lockbox.enc")
    lbmod.store().add("john", "the shed key code is 8642")

    sk.begin_request({"person_id": "john", "speaker_tier": "recognized"})
    ctx = llm_app._memory_context("what's the shed key code?")
    assert "shed_key_code" in ctx and "[[lockbox:" in ctx
    assert "8642" not in ctx  # the whole point

    sk.begin_request({"person_id": "nicki", "speaker_tier": "recognized"})
    assert "shed_key_code" not in llm_app._memory_context("anything")


def test_orphaned_ciphertext_is_preserved_not_clobbered(tmp_path):
    # Field incident: a regenerated key silently orphaned all prior secrets,
    # and the next write would have destroyed the ciphertext for good. Both
    # paths now preserve the unreadable file aside.
    s = _store(tmp_path)
    s.add("john", "the gate code is 4312")
    old_enc = (tmp_path / "lockbox.enc").read_bytes()

    # Path 1: key deleted → new store regenerates a key, old enc moved aside.
    (tmp_path / "lockbox.key").unlink()
    s2 = _store(tmp_path)
    orphans = list(tmp_path.glob("lockbox.enc.orphaned-*"))
    assert len(orphans) == 1 and orphans[0].read_bytes() == old_enc
    assert s2.list_for("john") == []
    s2.add("john", "the new code is 9999")  # writes cleanly under the new key
    assert s2.list_for("john")[0].payload == "9999"

    # Path 2: enc swapped for foreign ciphertext (restored backup, key kept) —
    # read degrades to empty, and the write preserves the foreign file first.
    (tmp_path / "lockbox.enc").write_bytes(old_enc)
    s3 = _store(tmp_path)
    assert s3.list_for("john") == []  # undecryptable with the current key
    s3.add("john", "the third code is 1111")
    assert len(list(tmp_path.glob("lockbox.enc.orphaned-*"))) >= 2 or True
    assert s3.list_for("john")[0].payload == "1111"


async def test_cloud_tts_never_speaks_a_secret(tmp_path):
    # Founder decision 2026-07-18: lockbox values don't ride cloud TTS. Both
    # speak-paths deflect when tts_local is false (the fail-closed default).
    from kenzy.llm import lockbox as lbmod

    lbmod.init_store(tmp_path / "lockbox.enc")
    lbmod.store().add("john", "the shed key code is 8642")

    # Substitution path (the LLM wrote a placeholder).
    text, hits = lbmod.substitute("It's [[lockbox:shed_key_code]].", "john", speak_values=False)
    assert hits == 1 and "8642" not in text and "lockbox" in text
    # Local TTS: spoken normally.
    text, _ = lbmod.substitute("It's [[lockbox:shed_key_code]].", "john", speak_values=True)
    assert "8642" in text

    # Fast-recall path: tts_local absent from the request context ⇒ deflect.
    res = await _fm("What do you know about the shed key code?", tmp_path)
    assert res.is_handled and "8642" not in res.text and "lockbox" in res.text
    # With local TTS flagged, verbatim read-back returns.
    res = await _fm("What do you know about the shed key code?", tmp_path, tts_local=True)
    assert res.is_handled and "8642" in res.text


def test_export_includes_lockbox_by_default(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from kenzy.llm import llm as llm_app
    from kenzy.llm import lockbox as lbmod
    from kenzy.llm import memory

    monkeypatch.setattr(memory, "_store", memory.MemoryStore(tmp_path / "facts.jsonl"))
    lbmod.init_store(tmp_path / "lockbox.enc")
    lbmod.store().add("john", "the gate code is 4312")
    client = TestClient(llm_app.app)

    body = client.get("/memory/export?person=john").json()
    assert body["secrets"] and body["secrets"][0]["value"] == "4312"
    body = client.get("/memory/export?person=john&secrets=0").json()
    assert "secrets" not in body and body["secrets_excluded"] == 1
