"""F2.5 memory skills + fast intents: the voice surface over the fact ledger,
including the F1.3 gate (min_tier=recognized) and person-id ownership."""

from __future__ import annotations

import pytest

from kenzy.llm import memory
from kenzy.llm import skills as reg
from kenzy.llm.builtin_skills import memory_skill as ms


@pytest.fixture
def store(tmp_path):
    s = memory.init_store(tmp_path / "facts.jsonl")

    # These tests exercise SETTLED semantics (what holds after the release
    # job clears quarantine, seconds after each write in production) — so
    # auto-release every write. Quarantine's own behavior is tested in
    # test_memory.py / test_quarantine_pipeline.
    orig_remember = s.remember

    def _settled_remember(*a, **k):
        f = orig_remember(*a, **k)
        if f.state == "quarantined":
            s.release(f.id)
            f = s.get_fact(f.id)
        return f

    s.remember = _settled_remember  # type: ignore[method-assign]
    try:
        yield s
    finally:
        memory._store = None


def _as(person_id, tier="recognized"):
    reg.begin_request({"person_id": person_id, "speaker_tier": tier})


# -- skills ------------------------------------------------------------------


async def test_remember_recall_forget_roundtrip(store):
    _as("adam")
    out = await ms.remember("the gate code is 4312")
    assert "Remembered" in out
    out = await ms.recall("gate code")
    assert "4312" in out
    out = await ms.forget("gate code")
    assert "Forgotten" in out
    assert "Nothing remembered" in await ms.recall("gate code")


async def test_shared_write_and_cross_person_visibility(store):
    _as("adam")
    await ms.remember("trash day is Tuesday", shared=True)
    await ms.remember("my pin is 9999")  # private
    _as("nicki")
    assert "Tuesday" in await ms.recall("trash day")
    assert "Nothing remembered" in await ms.recall("pin")


async def test_promote_and_demote(store):
    _as("adam")
    await ms.remember("the wifi password is hunter2")
    _as("nicki")
    assert "Nothing remembered" in await ms.recall("wifi password")
    _as("adam")
    assert "Shared" in await ms.share_memory("wifi password")
    _as("nicki")
    assert "hunter2" in await ms.recall("wifi password")
    _as("adam")
    assert "private" in (await ms.make_memory_private("wifi password")).lower()
    _as("nicki")
    assert "Nothing remembered" in await ms.recall("wifi password")


async def test_forget_lists_ambiguous_matches(store):
    _as("adam")
    await ms.remember("the front gate code is 1111")
    await ms.remember("the back gate code is 2222")
    out = await ms.forget("gate code")
    assert "Several facts match" in out and "1111" in out and "2222" in out


async def test_recognized_voice_without_person_record(store):
    _as(None)  # recognized tier, but no person record
    assert "People tab" in await ms.remember("anything")
    assert "People tab" in await ms.recall("anything")


async def test_memory_disabled(store):
    memory._store = None
    _as("adam")
    assert "turned off" in await ms.remember("x")


# -- the F1.3 gate on the registry paths --------------------------------------


async def test_memory_tools_gated_by_tier(store):
    _as("adam", tier="unknown")
    names = [t["function"]["name"] for t in reg.get_tools()]
    assert "remember" not in names and "recall" not in names
    out = await reg.execute("remember", {"fact": "x"})
    assert "Refused" in out
    _as("adam", tier="recognized")
    assert "remember" in [t["function"]["name"] for t in reg.get_tools()]


async def test_fast_memory_never_runs_for_unknown(store):
    _as("adam", tier="unknown")
    res = await reg.dispatch_fast("remember that the gate code is 4312", "office", None)
    assert res is None  # gated matcher skipped entirely
    assert len(store) == 0  # and nothing was written


# -- fast intents -------------------------------------------------------------


async def test_fast_remember_and_recall(store):
    _as("adam")
    res = await ms.fast_memory("Remember that the gate code is 4312", "office", "Adam")
    assert res.is_handled
    res = await ms.fast_memory("What do you know about the gate code?", "office", "Adam")
    assert res.is_handled and "4312" in res.text


async def test_fast_remember_shared_signal(store):
    _as("adam")
    res = await ms.fast_memory("Everyone should know the wifi password is hunter2", "o", None)
    assert res.is_handled and "everyone" in res.text.lower()
    _as("nicki")
    assert "hunter2" in await ms.recall("wifi password")


async def test_fast_remember_to_misses_to_llm(store):
    """ "remember to …" is usually a reminder — the fast path must not eat it."""
    _as("adam")
    res = await ms.fast_memory("Remember to take the bins out at 7", "office", None)
    assert res.status == "miss"
    assert len(store) == 0


async def test_fast_recall_misses_when_nothing_known(store):
    """ "what do you know about Paris" with no facts must fall to the LLM."""
    _as("adam")
    res = await ms.fast_memory("What do you know about Paris?", "office", None)
    assert res.status == "miss"


async def test_fast_forget(store):
    _as("adam")
    await ms.remember("the gate code is 4312")
    res = await ms.fast_memory("Forget the gate code", "office", None)
    assert res.is_handled and "Forgotten" in res.text
    # Bare colloquial "forget it" is a bail-out, not an erase.
    res = await ms.fast_memory("Forget it", "office", None)
    assert res.status == "miss"


async def test_inspection_summaries(store):
    # F2.6: "what do you know about me" / "about the house" give honest
    # summaries instead of keyword misses.
    from kenzy.llm import lockbox as lbmod

    lbmod._store = None  # no lockbox in this test
    _as("adam")
    res = await ms.fast_memory("What do you know about me?", None, "Adam")
    assert res.is_handled and "Nothing yet" in res.text
    store.remember("adam", "adam prefers decaf")
    res = await ms.fast_memory("What do you know about me?", None, "Adam")
    assert res.is_handled and "1 memory" in res.text and "decaf" in res.text
    res = await ms.fast_memory("What do you know about the house?", None, "Adam")
    assert res.is_handled and "No household memories" in res.text
    store.remember("adam", "everyone should know the bins go out Tuesday", tier=memory.TIER_SHARED)
    res = await ms.fast_memory("What do you know about the house?", None, "Adam")
    assert res.is_handled and "1 memory" in res.text and "bins" in res.text


async def test_offer_to_remember_suggest_flow(tmp_path):
    # Suggest capture (4.2): the model offers, the USER decides by voice.
    # (No `store` fixture: its settle-wrapper would release the quarantine we
    # assert on — init the raw store directly.)
    from kenzy.llm import asking

    memory.init_store(tmp_path / "facts.jsonl")
    try:

        async def run(answers):
            reg.begin_actions()
            reg.begin_request({"person_id": "john", "speaker_tier": "recognized",
                               "channel": "voice"})  # fmt: skip
            outcome = await asking.run_askable(
                ms.offer_to_remember("your dentist is Dr. Marsh"), kind="llm"
            )
            prompts = []
            while not outcome.finished:
                prompts.append(outcome.parked.channel.prompt)
                if answers is None:
                    await asking.cancel(outcome.parked.id)
                    return None, prompts
                outcome = await asking.resume(outcome.parked.id, answers.pop(0))
            return outcome.value, prompts

        out, prompts = await run(["yes please"])
        assert out == "Remembered."
        assert "Want me to remember that your dentist is Dr. Marsh?" in prompts[0]
        facts = memory.store().quarantined()
        assert facts and facts[0].source == "suggested"  # provenance + quarantine

        out, _ = await run(["no"])
        assert out == "Okay, I won't remember it."
        out, _ = await run(None)  # wake cancel
        assert out is None
        assert len(memory.store().quarantined()) == 1  # still just the one
    finally:
        memory._store = None


async def test_offer_to_remember_refuses_off_voice(tmp_path):
    memory.init_store(tmp_path / "facts.jsonl")
    try:
        reg.begin_actions()
        reg.begin_request({"person_id": "john", "speaker_tier": "recognized",
                           "channel": "assist"})  # fmt: skip
        out = await ms.offer_to_remember("something")
        assert out == ""  # silent on channels with no held mic
    finally:
        memory._store = None
