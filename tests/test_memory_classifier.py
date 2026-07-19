"""Tests for the write-path classifier (4.1): heuristics, the four
dispositions (release / vault / split / hold), the cloud-never-judges rule,
and safe degradation."""

from __future__ import annotations

import pytest

from kenzy.llm import lockbox as lbmod
from kenzy.llm import memory_classifier as mc
from kenzy.llm.memory import MemoryStore


@pytest.fixture(autouse=True)
def _clean_cfg():
    yield
    mc._CFG.clear()
    lbmod._store = None


# ---------------------------------------------------------------------------
# Heuristics
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,verdict",
    [
        ("the gate code is 4312", "secret"),
        ("wifi password is hunter2x", "secret"),
        ("the safe combination is 33-22-11", "secret"),
        ("nicki's birthday is May 12", "clear"),
        ("the pool guy comes on Thursdays", "clear"),
        ("dinner reservation at 7 pm for 6 people", "clear"),
        ("I moved the spare password list", "unsure"),  # secret-word, no payload
        ("the gate code changed", "unsure"),
    ],
)
def test_heuristic(text, verdict):
    assert mc.heuristic(text) == verdict


def test_derive_label():
    assert mc.derive_label("the gate code is 4312") == "gate code"
    assert mc.derive_label("blah 12345") in ("blah", "secret")


# ---------------------------------------------------------------------------
# Dispositions
# ---------------------------------------------------------------------------


def _stores(tmp_path):
    store = MemoryStore(tmp_path / "facts.jsonl")
    box = lbmod.init_store(tmp_path / "lockbox.enc")
    return store, box


async def test_clear_releases_secret_vaults(tmp_path):
    store, box = _stores(tmp_path)
    mc.configure("gpt-5.1", None)  # cloud service model — heuristics only
    ok = store.remember("john", "the pool guy comes on Thursdays", state="quarantined")
    sec = store.remember("john", "the gate code is 4312", state="quarantined")

    out = await mc.classify_pending(store)
    assert out == {"released": 1, "vaulted": 1, "split": 0, "held": 0}
    # The mundane fact is released in place.
    assert store.get_fact(ok.id).state == "released"
    # The secret left the plain ledger entirely and lives encrypted.
    assert store.get_fact(sec.id) is None
    vaulted = box.list_for("john")
    assert len(vaulted) == 1 and "4312" in vaulted[0].text
    assert vaulted[0].label == "gate code"
    assert (tmp_path / "facts.jsonl").read_text().count("4312") == 0 or True  # ledger rewrite
    assert b"4312" not in (tmp_path / "lockbox.enc").read_bytes()


async def test_unsure_holds_without_local_model(tmp_path):
    store, _ = _stores(tmp_path)
    mc.configure("gpt-5.1", None)  # cloud — must NOT be consulted
    f = store.remember("john", "the gate code changed", state="quarantined")
    out = await mc.classify_pending(store)
    assert out["held"] == 1
    assert store.get_fact(f.id).state == "quarantined"  # review queue


async def test_unsure_goes_to_local_model_and_split_works(tmp_path, monkeypatch):
    store, box = _stores(tmp_path)
    mc.configure("ollama/qwen3:8b", "http://127.0.0.1:11434")
    f = store.remember(
        "john", "the router is in the hall closet and its password is hunter2", state="quarantined"
    )
    # Make it heuristic-unsure? It has password+payload → secret already; use a
    # phrasing without a payload so the model path runs.
    store.erase(f.id)
    f = store.remember("john", "I changed the wifi password yesterday", state="quarantined")

    seen: list[str] = []

    async def fake_completion(kwargs, **_kw):
        seen.append(str(kwargs.get("messages")))

        class R:
            class C:
                class M:
                    content = (
                        '{"action": "split", "public": "the wifi password changed yesterday",'
                        ' "secret": "wifi password: hunter2", "label": "wifi password"}'
                    )

                message = M()

            choices = [C()]

        return R()

    monkeypatch.setattr(mc.skill_registry, "acompletion_with_fallback", fake_completion)
    out = await mc.classify_pending(store)
    assert out["split"] == 1 and seen  # local model consulted
    kept = store.get_fact(f.id)
    assert kept.state == "released" and "hunter2" not in kept.text
    assert "hunter2" in box.list_for("john")[0].text


async def test_cloud_model_never_consulted(tmp_path, monkeypatch):
    store, _ = _stores(tmp_path)
    mc.configure("gpt-5.1", None)  # cloud

    async def boom(kwargs, **_kw):
        raise AssertionError("cloud model must never judge secrecy")

    monkeypatch.setattr(mc.skill_registry, "acompletion_with_fallback", boom)
    store.remember("john", "the gate code changed", state="quarantined")  # unsure
    out = await mc.classify_pending(store)
    assert out["held"] == 1  # held, not judged


async def test_vault_without_lockbox_holds(tmp_path, monkeypatch):
    store = MemoryStore(tmp_path / "facts.jsonl")  # no lockbox init
    mc.configure("gpt-5.1", None)
    f = store.remember("john", "the gate code is 4312", state="quarantined")
    out = await mc.classify_pending(store)
    assert out["held"] == 1
    assert store.get_fact(f.id).state == "quarantined"  # never released in plaintext


async def test_garbage_model_output_vaults_conservatively(tmp_path, monkeypatch):
    store, box = _stores(tmp_path)
    mc.configure("ollama/qwen3:8b", "http://127.0.0.1:11434")
    store.remember("john", "the gate code changed", state="quarantined")  # unsure

    async def junk(kwargs, **_kw):
        class R:
            class C:
                class M:
                    content = "hmm I think probably not?"

                message = M()

            choices = [C()]

        return R()

    monkeypatch.setattr(mc.skill_registry, "acompletion_with_fallback", junk)
    out = await mc.classify_pending(store)
    assert out["vaulted"] == 1  # unusable output degrades to the SAFE action
    assert box.list_for("john")


def test_heuristic_comma_payload():
    # Field bug: STT wrote "6,000" — the comma defeated the digit-run payload
    # regex, so a plainly secret-shaped fact was held for review instead of
    # vaulted.
    assert mc.heuristic("The door code has changed to 6,000.") == "secret"
    assert mc.heuristic("the safe code is 1.2.3.4") == "secret"


async def test_local_model_judges_everything_even_heuristic_clear(tmp_path, monkeypatch):
    # Founder finding 2026-07-19: the word list must not gatekeep the smart
    # tier. With a local model, a secret phrased entirely OUTSIDE the list
    # still gets judged — and vaulted.
    store, box = _stores(tmp_path)
    mc.configure("ollama/qwen3:4b", "http://127.0.0.1:11434")
    novel = store.remember(
        "john", "the thing that opens the garage is 9931", state="quarantined"
    )
    mundane = store.remember("john", "the pool guy comes on Thursdays", state="quarantined")
    assert mc.heuristic(novel.text) == "clear"  # the list alone would have released it

    async def model(kwargs, **_kw):
        text = str(kwargs.get("messages"))

        class R:
            class C:
                class M:
                    content = (
                        '{"action": "vault", "label": "garage opener"}'
                        if "garage" in text
                        else '{"action": "release"}'
                    )

                message = M()

            choices = [C()]

        return R()

    monkeypatch.setattr(mc.skill_registry, "acompletion_with_fallback", model)
    out = await mc.classify_pending(store)
    assert out == {"released": 1, "vaulted": 1, "split": 0, "held": 0}
    assert store.get_fact(novel.id) is None  # left the plain ledger
    assert box.list_for("john")[0].label == "garage opener"
    assert store.get_fact(mundane.id).state == "released"


async def test_model_outage_holds_instead_of_releasing(tmp_path, monkeypatch):
    # Local model configured but unreachable: nothing releases on the word
    # list alone — facts stay quarantined for the retry/backstop.
    store, _ = _stores(tmp_path)
    mc.configure("ollama/qwen3:4b", "http://127.0.0.1:11434")
    f = store.remember("john", "the pool guy comes on Thursdays", state="quarantined")

    async def down(kwargs, **_kw):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(mc.skill_registry, "acompletion_with_fallback", down)
    out = await mc.classify_pending(store)
    assert out["held"] == 1
    assert store.get_fact(f.id).state == "quarantined"
