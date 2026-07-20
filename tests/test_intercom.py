"""Intercom on cross-room ask() (4.2): the skill's consent conversation, the
server's cross-room delivery/resolution rules, the connect_call bridge, the
relay, and teardown."""

from __future__ import annotations

import asyncio
import json

import pytest

from kenzy import protocol
from kenzy.llm import asking
from kenzy.llm import skills as sk
from kenzy.llm.builtin_skills import intercom
from kenzy.server.server import LlmReply, NodeSession, TranscribingServer, _is_affirmative


class _WS:
    """Records frames sent to a node (str control frames or binary audio)."""

    def __init__(self) -> None:
        self.sent: list[object] = []

    async def send(self, m: object) -> None:
        self.sent.append(m)


def _types(ws: _WS) -> list[str]:
    return [json.loads(m)["type"] for m in ws.sent if isinstance(m, str)]


def _server_with_two(monkeypatch, tmp_path):
    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    s = TranscribingServer(
        {
            "stt": {"url": "http://x/transcribe"},
            "speaker": {"url": "http://x/identify"},
            "llm": {"url": "http://x/process"},
        }
    )
    s._nodes["c"] = NodeSession(ws=_WS(), node_id="c", room_id="kitchen")
    s._nodes["r"] = NodeSession(ws=_WS(), node_id="r", room_id="living room")
    says: list[tuple[str, str]] = []

    async def fake_tts(node_id, room, sid, text, vp, **kw):  # no TTS service in tests
        says.append((node_id, text))
        return True

    monkeypatch.setattr(s, "_run_tts", fake_tts)
    return s, says


@pytest.fixture(autouse=True)
def _clean():
    yield
    for cid in list(asking._PENDING):
        asking._PENDING.pop(cid).task.cancel()


# ---------------------------------------------------------------------------
# Consent classifier + the skill's conversation
# ---------------------------------------------------------------------------


def test_is_affirmative_default_deny():
    assert _is_affirmative("yes")
    assert _is_affirmative("Yeah, sure.")
    assert _is_affirmative("go ahead")
    assert not _is_affirmative("no")
    assert not _is_affirmative("")
    assert not _is_affirmative("hey kenzy what time is it")
    # The skill's mirror agrees (wire-contract duplication).
    for t in ("yes", "Yeah, sure.", "", "no", "maybe later"):
        assert intercom._is_affirmative(t) == _is_affirmative(t)


async def _drive_skill(utter_room, answers, *, rooms=("kitchen", "living room")):
    sk.begin_actions()
    sk.begin_request(
        {"channel": "voice", "room_id": "kitchen", "rooms": list(rooms), "no_aec_rooms": []}
    )
    outcome = await asking.run_askable(intercom.connect_room(utter_room), kind="llm")
    asked = []
    while not outcome.finished:
        ch = outcome.parked.channel
        asked.append((ch.room, ch.announce, ch.prompt))
        if answers is None:
            await asking.cancel(outcome.parked.id)
            return None, asked
        outcome = await asking.resume(outcome.parked.id, answers.pop(0))
    return outcome.value, asked


async def test_consent_yes_connects():
    text, asked = await _drive_skill("living room", ["yes"])
    assert text == "Connecting you now."
    assert asked == [
        (
            "living room",
            "Calling the living room.",
            "The kitchen would like to start a voice chat. Say yes to accept, or no to decline.",
        )
    ]
    assert {"type": "connect_call", "room": "living room"} in sk.take_actions()


async def test_consent_decline_and_no_answer():
    text, _ = await _drive_skill("living room", ["no thanks"])
    assert text == "The living room declined."
    assert sk.take_actions() == []  # default-deny: no bridge action

    text, _ = await _drive_skill("living room", [""])  # empty = no answer/wake/lost
    assert text == "No answer from the living room."
    assert sk.take_actions() == []


async def test_skill_refusals_never_ask():
    text, asked = await _drive_skill("attic", ["yes"])  # not a connected room
    assert text == "I couldn't reach the attic." and asked == []

    sk.begin_actions()
    sk.begin_request(
        {"channel": "voice", "room_id": "kitchen", "rooms": ["kitchen", "living room"],
         "no_aec_rooms": ["living room"]}  # fmt: skip
    )
    out = await asking.run_askable(intercom.connect_room("living room"), kind="llm")
    assert out.finished and "echo-cancelling" in out.value


# ---------------------------------------------------------------------------
# Server: cross-room delivery + resolution rules
# ---------------------------------------------------------------------------


def _cross_reply(cont="c1"):
    return LlmReply(
        "Calling the living room.",
        "vp",
        expect_response=True,
        continuation=cont,
        ask_room="living room",
        ask_prompt="The kitchen would like to start a voice chat. Say yes or no.",
    )


async def test_cross_ask_delivers_prompt_at_target(monkeypatch, tmp_path):
    s, says = _server_with_two(monkeypatch, tmp_path)
    await s._deliver_reply("c", "kitchen", None, _cross_reply())
    # Announcement at the caller, question at the target.
    assert ("c", "Calling the living room.") in says
    assert any(nid == "r" and "voice chat" in t for nid, t in says)
    # Pending ask keyed at the TARGET with the caller as origin.
    assert s._pending_ask["r"]["id"] == "c1"
    assert s._pending_ask["r"]["origin_node"] == "c"
    # Ringback to the caller; floor held at the target.
    assert protocol.MSG_CALL_RINGING in _types(s._nodes["c"].ws)
    assert any(protocol.MSG_EXPECT_UTTERANCE in str(m) for m in s._nodes["r"].ws.sent)


async def test_cross_ask_unreachable_resolves_empty(monkeypatch, tmp_path):
    s, says = _server_with_two(monkeypatch, tmp_path)
    del s._nodes["r"]
    resolved = []

    async def fake_resume(cont_id, origin_node, origin_room):
        resolved.append((cont_id, origin_node))

    monkeypatch.setattr(s, "_resume_ask_empty", fake_resume)
    await s._deliver_reply("c", "kitchen", None, _cross_reply())
    await asyncio.sleep(0.02)
    assert resolved == [("c1", "c")]
    assert "r" not in s._pending_ask


async def test_answer_at_target_speaks_outcome_at_origin(monkeypatch, tmp_path):
    s, says = _server_with_two(monkeypatch, tmp_path)
    continues = []

    async def stt(pcm, room, sid):
        return "yes"

    async def spk(pcm, room):
        return "unknown", 0.0

    async def cont(cont_id, text, identity):
        continues.append((cont_id, text))
        return LlmReply(
            "Connecting you now.", "vp",
            actions=[{"type": "connect_call", "room": "living room"}],
        )  # fmt: skip

    connected = []

    async def fake_connect(caller_id, caller_room, room):
        connected.append((caller_id, room))

    monkeypatch.setattr(s, "_call_stt", stt)
    monkeypatch.setattr(s, "_call_speaker", spk)
    monkeypatch.setattr(s, "_call_llm_continue", cont)
    monkeypatch.setattr(s, "_action_connect_call", fake_connect)
    s._pending_ask["r"] = {
        "id": "c1", "capture": "text", "origin_node": "c", "origin_room": "kitchen"
    }  # fmt: skip
    await s._transcribe("r", "living room", "s1", b"pcm")
    assert continues == [("c1", "yes")]
    # The outcome ("Connecting you now.") spoke at the CALLER, and the bridge
    # action ran with the caller as source.
    assert ("c", "Connecting you now.") in says
    assert connected == [("c", "living room")]


async def test_target_wake_resolves_empty_not_cancel(monkeypatch, tmp_path):
    s, _ = _server_with_two(monkeypatch, tmp_path)
    resolved = []

    async def fake_resume(cont_id, origin_node, origin_room):
        resolved.append(cont_id)

    canceled = []
    monkeypatch.setattr(s, "_resume_ask_empty", fake_resume)
    monkeypatch.setattr(s, "_cancel_pending_ask", lambda n, r: canceled.append(n))
    s._pending_ask["r"] = {
        "id": "c1", "capture": "text", "origin_node": "c", "origin_room": "kitchen"
    }  # fmt: skip
    await s.on_wakeword(s._nodes["r"], "hey_ken_zee", 0.9)
    await asyncio.sleep(0.02)
    assert resolved == ["c1"] and canceled == []  # the caller still hears the outcome


async def test_origin_disconnect_cancels_cross_ask(monkeypatch, tmp_path):
    s, _ = _server_with_two(monkeypatch, tmp_path)
    canceled = []
    monkeypatch.setattr(s, "_cancel_by_id", lambda cid, r: canceled.append((cid, r)))
    s._pending_ask["r"] = {
        "id": "c1", "capture": "text", "origin_node": "c", "origin_room": "kitchen"
    }  # fmt: skip
    s._cleanup_on_disconnect("c")
    assert ("c1", "asker disconnected") in canceled
    assert "r" not in s._pending_ask


# ---------------------------------------------------------------------------
# Bridge + relay + teardown (unchanged server ownership)
# ---------------------------------------------------------------------------


async def test_action_connect_call_bridges_both(monkeypatch, tmp_path):
    s, says = _server_with_two(monkeypatch, tmp_path)
    await s._action_connect_call("c", "kitchen", "living room")
    assert s._nodes["c"].intercom_peer == "r"
    assert s._nodes["r"].intercom_peer == "c"
    assert protocol.MSG_INTERCOM_START in _types(s._nodes["c"].ws)
    assert protocol.MSG_INTERCOM_START in _types(s._nodes["r"].ws)


async def test_action_connect_call_rechecks(monkeypatch, tmp_path):
    s, says = _server_with_two(monkeypatch, tmp_path)
    s._nodes["r"].intercom_peer = "x"  # busy
    await s._action_connect_call("c", "kitchen", "living room")
    assert s._nodes["c"].intercom_peer is None
    assert any(nid == "c" and "busy" in t for nid, t in says)

    says.clear()
    await s._action_connect_call("c", "kitchen", "attic")  # unreachable
    assert any(nid == "c" and "couldn't reach" in t for nid, t in says)


async def test_relay_forwards_to_peer(monkeypatch, tmp_path):
    s, _ = _server_with_two(monkeypatch, tmp_path)
    await s._connect_call("c", "r")
    await s._relay_intercom(s._nodes["c"], b"\x01\x02")
    assert b"\x01\x02" in s._nodes["r"].ws.sent


async def test_end_intercom_tears_down_both(monkeypatch, tmp_path):
    s, _ = _server_with_two(monkeypatch, tmp_path)
    await s._connect_call("c", "r")
    await s.end_intercom("c")
    assert s._nodes["c"].intercom_peer is None
    assert s._nodes["r"].intercom_peer is None
    assert protocol.MSG_INTERCOM_END in _types(s._nodes["c"].ws)
    assert protocol.MSG_INTERCOM_END in _types(s._nodes["r"].ws)
