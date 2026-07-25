"""Knock-knock skill: telling and playing along, both as chained ask() flows —
and the canonical proof of ask(busy_cues=False) keeping conversational
turnarounds ladder-free."""

from __future__ import annotations

import pytest

from kenzy.llm import asking
from kenzy.llm.builtin_skills import knock_knock as kk
from kenzy.llm.skills import FastResult


@pytest.fixture(autouse=True)
def _sweep_pending():
    yield
    for cid in list(asking._PENDING):
        asking._PENDING.pop(cid).task.cancel()


async def _run(utterance: str) -> asking.AskOutcome:
    return await asking.run_askable(
        kk.fast_knock_knock(utterance, "office", "adam"), kind="fast"
    )


# ---------------------------------------------------------------------------
# Matchers (anchored whole-utterance — the "time for dinner" discipline)
# ---------------------------------------------------------------------------


async def test_matcher_negatives_miss():
    for phrase in (
        "Tell me a joke.",  # ordinary jokes stay with the LLM
        "Knock it off.",
        "There was a knock knock at the door.",
        "Who's there?",
        "Tell me about knock knock jokes.",
    ):
        outcome = await _run(phrase)
        assert outcome.finished, phrase
        assert isinstance(outcome.value, FastResult) and outcome.value.status == "miss", phrase


async def test_tell_phrasings_match():
    for phrase in (
        "Tell me a knock knock joke.",
        "tell us a knock-knock joke",
        "Can you tell me another knock knock joke?",
        "Do you know any knock knock jokes?",
    ):
        outcome = await _run(phrase)
        assert not outcome.finished, phrase  # parked on "Knock knock!"
        assert outcome.parked is not None
        assert outcome.parked.channel.prompt == "Knock knock!"
        await asking.cancel(outcome.parked.id)


async def test_knock_knock_variants_match():
    for phrase in ("Knock knock.", "knock, knock!", "Knock-knock"):
        outcome = await _run(phrase)
        assert not outcome.finished, phrase  # parked on "Who's there?"
        assert outcome.parked is not None
        assert outcome.parked.channel.prompt == "Who's there?"
        await asking.cancel(outcome.parked.id)


# ---------------------------------------------------------------------------
# Telling: Knock knock! → (who's there) → Setup. → (setup who?) → punchline
# ---------------------------------------------------------------------------


async def test_tell_joke_full_exchange():
    outcome = await _run("Tell me a knock knock joke.")
    assert not outcome.finished
    parked = outcome.parked
    assert parked is not None
    # EVERY question in the exchange opts out of the processing-cue ladder —
    # the whole point of busy_cues: a canned "Working on it." mid-joke is a barge.
    assert parked.channel.busy_cues is False

    outcome = await asking.resume(parked.id, "Who's there?")
    assert not outcome.finished
    parked = outcome.parked
    assert parked is not None
    setup = parked.channel.prompt.rstrip(".")
    assert any(setup == s for s, _ in kk._JOKES)
    assert parked.channel.busy_cues is False

    outcome = await asking.resume(parked.id, f"{setup} who?")
    assert outcome.finished
    result = outcome.value
    assert isinstance(result, FastResult) and result.status == "handled"
    assert result.text == dict(kk._JOKES)[setup]
    assert result.expect_response is False  # the punchline ends the exchange


async def test_tell_joke_never_repeats_back_to_back():
    setups = []
    for _ in range(8):
        outcome = await _run("Tell me a knock knock joke.")
        parked = outcome.parked
        assert parked is not None
        outcome = await asking.resume(parked.id, "Who's there?")
        assert outcome.parked is not None
        setups.append(outcome.parked.channel.prompt.rstrip("."))
        await asking.cancel(outcome.parked.id)
    assert all(a != b for a, b in zip(setups, setups[1:]))


async def test_tell_joke_wake_cancel_mid_exchange():
    outcome = await _run("Tell me a knock knock joke.")
    parked = outcome.parked
    assert parked is not None
    await asking.cancel(parked.id, "wakeword")  # skill sees None, result discarded
    assert asking.pending_count() == 0


# ---------------------------------------------------------------------------
# Playing along: (knock knock) → Who's there? → <name> who? → reaction
# ---------------------------------------------------------------------------


async def test_play_along_full_exchange():
    outcome = await _run("Knock knock.")
    parked = outcome.parked
    assert parked is not None
    assert parked.channel.prompt == "Who's there?"
    assert parked.channel.busy_cues is False

    outcome = await asking.resume(parked.id, "Boo.")
    assert not outcome.finished
    parked = outcome.parked
    assert parked is not None
    assert parked.channel.prompt == "Boo who?"  # STT punctuation trimmed
    assert parked.channel.busy_cues is False

    outcome = await asking.resume(parked.id, "Don't cry, it's only a joke!")
    assert outcome.finished
    result = outcome.value
    assert isinstance(result, FastResult) and result.status == "handled"
    assert result.text in kk._REACTIONS


async def test_play_along_empty_answer_ends_quietly():
    outcome = await _run("Knock knock.")
    parked = outcome.parked
    assert parked is not None
    outcome = await asking.resume(parked.id, "...")
    assert outcome.finished
    assert isinstance(outcome.value, FastResult)
    assert outcome.value.text == ""  # nothing worth saying — quiet end


# ---------------------------------------------------------------------------
# The LLM-tier tool (fuzzy phrasings; typed channels get the flat rendition)
# ---------------------------------------------------------------------------


async def test_tool_types_flat_joke_on_assist(monkeypatch):
    from kenzy.llm import skills as sk

    monkeypatch.setattr(sk, "get_request", lambda key, default=None: "assist"
                        if key == "channel" else default)
    text = await kk.knock_knock_joke()
    assert "Knock knock." in text and "(Who's there?)" in text
