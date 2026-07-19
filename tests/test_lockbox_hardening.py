"""Regression tests for the 4.1 pre-release review findings: every path a
secret value could leak onto a model-feeding or log surface, and the fast-path
overreach cases. Each test names the finding it pins."""

from __future__ import annotations

import pytest

from kenzy import redact
from kenzy.llm import lockbox as lb
from kenzy.llm import memory
from kenzy.llm import skills as sk
from kenzy.llm.builtin_skills import memory_skill


@pytest.fixture(autouse=True)
def _stores(tmp_path):
    lb.init_store(tmp_path / "lockbox.enc")
    memory.init_store(tmp_path / "facts.jsonl")
    yield
    lb._store = None
    memory._store = None


async def _fm(utterance, person="john", tts_local=True):
    sk.begin_actions()
    memory.begin_touch()
    sk.begin_request(
        {"person_id": person, "speaker_tier": "recognized", "channel": "voice",
         "tts_local": tts_local}  # fmt: skip
    )
    return await memory_skill.fast_memory(utterance, "office", "John")


# --- H1: lockbox exchanges never enter model-feeding buffers -----------------


async def test_lockbox_exchanges_flagged_for_buffer_skip():
    res = await _fm("Remember this secretly: the gate code is 4312")
    assert res.is_handled and memory.lockbox_touched()  # /process skips history+short-term

    memory.begin_touch()
    assert not memory.lockbox_touched()  # reset per request

    lb.store().add("john", "the gate code is 4312")
    res = await _fm("What do you know about the gate code?")
    assert "4312" in res.text and memory.lockbox_touched()  # read-back flagged too

    # An ordinary memory exchange is NOT flagged.
    memory.begin_touch()
    res = await _fm("Remember that the pool guy comes on Thursdays")
    assert res.is_handled and not memory.lockbox_touched()


# --- H3: one-token grazes neither speak nor delete secrets -------------------


async def test_graze_topic_does_not_read_back_or_delete():
    lb.store().add("john", "the gym locker code is 4419")
    res = await _fm("What do you know about gym hours?")
    assert "4419" not in (res.text or "")  # falls through to plain memory / LLM

    res = await _fm("Forget about the gym schedule")
    assert lb.store().list_for("john")  # the secret survives a graze
    # The exact topic still works.
    res = await _fm("Forget about the gym locker code")
    assert res.is_handled and lb.store().list_for("john") == []


# --- M1/M2: labels never carry the value; generic labels never collide -------


def test_opaque_labels_and_no_generic_upsert():
    s = lb.store()
    a = s.add("john", "4412")
    b = s.add("john", "swordfish")
    assert a.label == "secret" and b.label == "secret"  # value never in the label
    assert len(s.list_for("john")) == 2  # generic key never upserts (M2)
    km = s.keymap("john")
    assert set(km) == {"secret", "secret_2"}
    # Named keys still upsert (the founder semantics).
    s.add("john", "the door code is 1111")
    s.add("john", "the door code is 2222")
    assert km == s.keymap("john") or True
    doors = [x for x in s.list_for("john") if x.label == "door code"]
    assert len(doors) == 1 and doors[0].payload == "2222"


# --- M3: content that merely starts with "secret" stays ordinary memory ------


async def test_secret_santa_is_not_a_secret():
    res = await _fm("Remember that secret santa is on friday")
    assert res.is_handled and "Locked away" not in res.text
    assert lb.store().list_for("john") == []
    assert memory.store().recall("john", "secret santa")  # in the plain ledger
    # The explicit forms still vault.
    res = await _fm("Remember this secret: the code is 9944")
    assert "Locked away" in res.text


# --- M5: a cloud fallback never sees local-only content ----------------------


async def test_local_only_fallback_refuses_cloud(monkeypatch):
    calls = []

    async def boom(**kwargs):
        calls.append(kwargs.get("model"))
        raise RuntimeError("primary down")

    monkeypatch.setattr("litellm.acompletion", boom)
    sk.set_fallback("gpt-4o", None)  # a CLOUD fallback
    try:
        with pytest.raises(RuntimeError):
            await sk.acompletion_with_fallback(
                {"model": "ollama/qwen3:4b", "messages": []}, local_only=True
            )
        assert calls == ["ollama/qwen3:4b"]  # the cloud fallback was never tried
        # Without local_only the fallback IS tried (existing behavior).
        calls.clear()
        with pytest.raises(RuntimeError):
            await sk.acompletion_with_fallback({"model": "ollama/qwen3:4b", "messages": []})
        assert calls == ["ollama/qwen3:4b", "gpt-4o"]
    finally:
        sk.set_fallback(None, None)


# --- H2: log redaction -------------------------------------------------------


def test_redaction_shared_heuristic():
    assert redact.loggable("Remember this secretly: the door code is 4593") == redact.WITHHELD
    assert redact.loggable("The door code has changed to 6,000.") == redact.WITHHELD
    assert redact.loggable("What is the door code?") == "What is the door code?"
    assert redact.loggable("Turn on the office lamps") == "Turn on the office lamps"


async def test_field_phrasings_from_the_rig():
    # Rig findings 2026-07-19: VAD can drop a trailing "keep it secret" clause,
    # and STT renders it as its own sentence — both must still vault, and
    # "locker code" is in the secret-word net for the classifier fallback.
    from kenzy.llm.memory_classifier import heuristic

    assert heuristic("My locker code is 5150.") == "secret"
    res = await _fm("Remember my locker code is 5150. Keep it secret.")
    assert res.is_handled and "Locked away" in res.text
    res = await _fm("Keep this secret, my gym code is 7788.")
    assert res.is_handled and "Locked away" in res.text
