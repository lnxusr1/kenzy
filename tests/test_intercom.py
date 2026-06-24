"""Tests for the intercom call setup, consent gate, relay, and teardown (server side)."""

from __future__ import annotations

import asyncio
import json

from kenzy import protocol
from kenzy.llm import skills as sk
from kenzy.llm.builtin_skills.intercom import connect_room
from kenzy.server.server import NodeSession, TranscribingServer, _is_affirmative


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
    s = TranscribingServer({})
    s._nodes["c"] = NodeSession(ws=_WS(), node_id="c", room_id="kitchen")
    s._nodes["r"] = NodeSession(ws=_WS(), node_id="r", room_id="living room")
    says: list[tuple[str, str]] = []

    async def fake_tts(node_id, room, sid, text, vp):  # no TTS service in tests
        says.append((node_id, text))

    monkeypatch.setattr(s, "_run_tts", fake_tts)
    return s, says


# ---------------------------------------------------------------------------
# Consent classifier + skill
# ---------------------------------------------------------------------------


def test_is_affirmative_default_deny():
    assert _is_affirmative("yes")
    assert _is_affirmative("Yeah, sure.")
    assert _is_affirmative("go ahead")
    assert not _is_affirmative("no")
    assert not _is_affirmative("")
    assert not _is_affirmative("hey kenzy what time is it")


async def test_connect_room_skill_queues_action():
    sk.begin_actions()
    out = await connect_room("living room")
    assert "living room" in out.lower()
    assert sk.take_actions() == [{"type": "start_intercom", "room": "living room"}]


# ---------------------------------------------------------------------------
# Call setup / ringing
# ---------------------------------------------------------------------------


async def test_start_intercom_rings_and_pends(tmp_path, monkeypatch):
    s, says = _server_with_two(monkeypatch, tmp_path)
    await s.start_intercom("c", "kitchen", "living room")
    assert protocol.MSG_CALL_REQUEST in _types(s._nodes["r"].ws)  # receiver rung
    assert "r" in s._pending_calls  # pending consent
    assert any(nid == "r" for nid, _ in says)  # consent prompt spoken to receiver
    assert s._nodes["c"].intercom_peer is None  # not bridged yet
    s._pending_calls["r"][2].cancel()


async def test_start_intercom_unreachable(tmp_path, monkeypatch):
    s, says = _server_with_two(monkeypatch, tmp_path)
    await s.start_intercom("c", "kitchen", "bedroom")  # no such room
    assert "r" not in s._pending_calls
    assert any(nid == "c" and "couldn't reach" in t.lower() for nid, t in says)


async def test_start_intercom_busy(tmp_path, monkeypatch):
    s, says = _server_with_two(monkeypatch, tmp_path)
    s._nodes["r"].intercom_peer = "someone"  # receiver already in a call
    await s.start_intercom("c", "kitchen", "living room")
    assert "r" not in s._pending_calls
    assert any(nid == "c" and "busy" in t.lower() for nid, t in says)


# ---------------------------------------------------------------------------
# Consent decision
# ---------------------------------------------------------------------------


async def test_consent_accept_bridges_both(tmp_path, monkeypatch):
    s, says = _server_with_two(monkeypatch, tmp_path)
    await s.start_intercom("c", "kitchen", "living room")
    await s._resolve_call("r", accepted=True)
    assert s._nodes["c"].intercom_peer == "r"
    assert s._nodes["r"].intercom_peer == "c"
    assert protocol.MSG_INTERCOM_START in _types(s._nodes["c"].ws)
    assert protocol.MSG_INTERCOM_START in _types(s._nodes["r"].ws)
    assert "r" not in s._pending_calls


async def test_consent_decline_notifies_caller(tmp_path, monkeypatch):
    s, says = _server_with_two(monkeypatch, tmp_path)
    await s.start_intercom("c", "kitchen", "living room")
    says.clear()
    await s._resolve_call("r", accepted=False)
    assert s._nodes["c"].intercom_peer is None
    assert s._nodes["r"].intercom_peer is None
    assert any(nid == "c" and "declined" in t.lower() for nid, t in says)


# ---------------------------------------------------------------------------
# Relay + teardown
# ---------------------------------------------------------------------------


async def test_relay_forwards_to_peer(tmp_path, monkeypatch):
    s, _ = _server_with_two(monkeypatch, tmp_path)
    s._nodes["c"].intercom_peer = "r"
    s._nodes["r"].intercom_peer = "c"
    await s._relay_intercom(s._nodes["c"], b"\x01\x02\x03")
    assert b"\x01\x02\x03" in s._nodes["r"].ws.sent


async def test_end_intercom_tears_down_both(tmp_path, monkeypatch):
    s, _ = _server_with_two(monkeypatch, tmp_path)
    s._nodes["c"].intercom_peer = "r"
    s._nodes["r"].intercom_peer = "c"
    assert await s.end_intercom("c", reason="test") is True
    assert s._nodes["c"].intercom_peer is None
    assert s._nodes["r"].intercom_peer is None
    assert protocol.MSG_INTERCOM_END in _types(s._nodes["c"].ws)
    assert protocol.MSG_INTERCOM_END in _types(s._nodes["r"].ws)


async def test_connect_call_does_not_self_cancel(tmp_path, monkeypatch):
    # Regression: _connect_call runs inside the receiver's consent-capture transcribe
    # task. It must NOT cancel that task (which would abort itself and leave one
    # intercom_start unsent → a one-way call). Both ends must get intercom_start.
    s, _ = _server_with_two(monkeypatch, tmp_path)

    async def run():
        s._stt_tasks["r"] = asyncio.current_task()  # mimic the running transcribe task
        await s._connect_call("c", "r")
        return True

    assert await asyncio.create_task(run()) is True  # not cancelled mid-way
    assert s._nodes["c"].intercom_peer == "r"
    assert s._nodes["r"].intercom_peer == "c"
    assert protocol.MSG_INTERCOM_START in _types(s._nodes["c"].ws)
    assert protocol.MSG_INTERCOM_START in _types(s._nodes["r"].ws)


async def test_dispatch_start_intercom_action(tmp_path, monkeypatch):
    s, _ = _server_with_two(monkeypatch, tmp_path)
    calls: list[tuple[str, str, str]] = []

    async def fake_start(cid, croom, target):
        calls.append((cid, croom, target))

    monkeypatch.setattr(s, "start_intercom", fake_start)
    await s._dispatch_actions([{"type": "start_intercom", "room": "den"}], "c", "kitchen")
    assert calls == [("c", "kitchen", "den")]
