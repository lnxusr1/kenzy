"""The ask() primitive (4.2): suspended-continuation mechanics through the
real /process → /process/continue → /process/cancel wire."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from kenzy.llm import asking, memory
from kenzy.llm import llm as llm_app
from kenzy.llm import skills as sk


@pytest.fixture(autouse=True)
def _clean(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "_store", memory.MemoryStore(tmp_path / "facts.jsonl"))
    yield
    for cid in list(asking._PENDING):
        asking._PENDING.pop(cid).task.cancel()


@pytest.fixture()
def c():
    # Context-manager use keeps ONE portal event loop across requests — a
    # parked ask() task must survive between /process and /process/continue
    # (as it does under uvicorn's single loop).
    with TestClient(llm_app.app) as client:
        yield client


def _fast_asker(script):
    """A fake dispatch_fast whose matcher asks per `script` (a coroutine fn)."""

    async def dispatch(utterance, room_id, speaker):
        return await script(utterance)

    return dispatch


async def test_ask_roundtrip_fast(monkeypatch, c):
    seen = {}

    async def script(utterance):
        reply = await sk.ask("Should I create a list called Groceries?")
        seen["reply"] = reply
        return sk.FastResult.handled(f"Created. You said {reply}.")

    monkeypatch.setattr(sk, "dispatch_fast", _fast_asker(script))
    r = c.post("/process", json={"text": "add milk", "room_id": "office"}).json()
    assert r["text"] == "Should I create a list called Groceries?"
    assert r["expect_response"] is True and r["continuation"]
    assert asking.pending_count() == 1

    r2 = c.post(
        "/process/continue",
        json={"continuation": r["continuation"], "text": "yes please", "speaker": "John"},
    ).json()
    assert seen["reply"] == "yes please"
    assert r2["text"] == "Created. You said yes please."
    assert r2["fast"] is True and r2.get("continuation") is None
    assert asking.pending_count() == 0


async def test_chained_asks(monkeypatch, c):
    async def script(utterance):
        a = await sk.ask("First question?")
        b = await sk.ask("Second question?")
        return sk.FastResult.handled(f"{a}+{b}")

    monkeypatch.setattr(sk, "dispatch_fast", _fast_asker(script))
    r = c.post("/process", json={"text": "go", "room_id": "office"}).json()
    assert r["text"] == "First question?"
    r = c.post("/process/continue", json={"continuation": r["continuation"], "text": "one"}).json()
    assert r["text"] == "Second question?" and r["continuation"]
    r = c.post("/process/continue", json={"continuation": r["continuation"], "text": "two"}).json()
    assert r["text"] == "one+two"
    assert asking.pending_count() == 0


async def test_cancel_returns_none_and_discards(monkeypatch, c):
    seen = {}

    async def script(utterance):
        reply = await sk.ask("Really?")
        seen["reply"] = reply
        return sk.FastResult.handled("You never hear this.")

    monkeypatch.setattr(sk, "dispatch_fast", _fast_asker(script))
    r = c.post("/process", json={"text": "go", "room_id": "office"}).json()
    r2 = c.post("/process/cancel", json={"continuation": r["continuation"], "reason": "wakeword"})
    assert r2.json()["ok"] is True
    assert seen["reply"] is None  # the skill saw the cancel
    assert asking.pending_count() == 0
    # Unknown id: fine (answer won the race).
    assert c.post("/process/cancel", json={"continuation": "nope"}).json()["ok"] is True


async def test_answerer_identity_reaches_resumed_skill(monkeypatch, c):
    seen = {}

    async def script(utterance):
        before = sk.get_request("person_id")
        await sk.ask("Who goes there?")
        seen["before"] = before
        seen["after"] = sk.get_request("person_id")
        seen["tier"] = sk.get_request("speaker_tier")
        return sk.FastResult.handled("ok")

    monkeypatch.setattr(sk, "dispatch_fast", _fast_asker(script))
    r = c.post(
        "/process",
        json={"text": "go", "room_id": "office", "person_id": "john",
              "speaker_tier": "recognized"},  # fmt: skip
    ).json()
    c.post(
        "/process/continue",
        json={"continuation": r["continuation"], "text": "it is nicki",
              "person_id": "nicki", "speaker_tier": "recognized"},  # fmt: skip
    )
    assert seen["before"] == "john" and seen["after"] == "nicki"
    assert seen["tier"] == "recognized"


async def test_actions_after_resume_ride_the_final_response(monkeypatch, c):
    async def script(utterance):
        reply = await sk.ask("Which room?")
        sk.add_action({"type": "announce", "room": reply})
        return sk.FastResult.handled(f"Announcing in {reply}.")

    monkeypatch.setattr(sk, "dispatch_fast", _fast_asker(script))
    r = c.post("/process", json={"text": "go", "room_id": "office"}).json()
    assert r["actions"] == []  # nothing queued before the ask
    r2 = c.post(
        "/process/continue", json={"continuation": r["continuation"], "text": "kitchen"}
    ).json()
    assert r2["actions"] == [{"type": "announce", "room": "kitchen"}]


async def test_continue_unknown_404(monkeypatch, c):
    r = c.post("/process/continue", json={"continuation": "ghost", "text": "hello"})
    assert r.status_code == 404


async def test_lockbox_touch_flows_out_of_parked_task(monkeypatch, c):
    # The touch markers are dict-based precisely so a parked task's marks are
    # visible to the continue handler (contextvar sets wouldn't be).
    async def script(utterance):
        await sk.ask("Confirm?")
        memory.mark_lockbox_touch()
        return sk.FastResult.handled("the code is 1234")

    monkeypatch.setattr(sk, "dispatch_fast", _fast_asker(script))
    r = c.post("/process", json={"text": "go", "room_id": "office"}).json()
    r2 = c.post(
        "/process/continue", json={"continuation": r["continuation"], "text": "yes"}
    ).json()
    assert r2["secret"] is True  # the server will redact this turn


async def test_ask_outside_context_raises():
    with pytest.raises(RuntimeError):
        await asking.ask("no context")

async def test_pre_ask_actions_ship_once_on_the_prompt_turn(monkeypatch, c):
    # Review finding M1: an action queued BEFORE the first ask() must ride the
    # parked prompt response and NEVER re-ship on the finished turn.
    async def script(utterance):
        sk.add_action({"type": "set_volume", "level": 20})
        await sk.ask("Quieter — OK?")
        return sk.FastResult.handled("Done.")

    monkeypatch.setattr(sk, "dispatch_fast", _fast_asker(script))
    r = c.post("/process", json={"text": "go", "room_id": "office"}).json()
    assert r["actions"] == [{"type": "set_volume", "level": 20}]
    r2 = c.post("/process/continue", json={"continuation": r["continuation"], "text": "yes"}).json()
    assert r2["actions"] == []  # not dispatched a second time


async def test_mid_chain_actions_ship_on_the_next_prompt_turn(monkeypatch, c):
    # Review finding M2 (the enrollment adopt_voice shape): an action queued on
    # a RESUMED turn that parks again must ride that turn's prompt response —
    # a later cancel must not lose it.
    async def script(utterance):
        await sk.ask("Sample one?")
        sk.add_action({"type": "adopt_voice", "name": "alice"})
        await sk.ask("Sample two?")
        return sk.FastResult.handled("Enrolled.")

    monkeypatch.setattr(sk, "dispatch_fast", _fast_asker(script))
    r = c.post("/process", json={"text": "go", "room_id": "office"}).json()
    assert r["actions"] == []
    r = c.post("/process/continue", json={"continuation": r["continuation"], "text": "one"}).json()
    assert r["text"] == "Sample two?"
    assert r["actions"] == [{"type": "adopt_voice", "name": "alice"}]
    # Abandoning here loses nothing — the adopt already shipped.
    c.post("/process/cancel", json={"continuation": r["continuation"], "reason": "wakeword"})
    assert asking.pending_count() == 0
