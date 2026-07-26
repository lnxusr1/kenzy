"""Server-side ask() wiring (4.2): a continuation-bearing reply stores the
pending ask and holds the floor; the next utterance routes to
/process/continue with the ANSWERER's identity; wake / silence / stop /
window-expiry / disconnect all cancel."""

from __future__ import annotations

import asyncio

from kenzy import protocol
from kenzy.server.server import LlmReply, NodeSession, TranscribingServer


class _RecordingWS:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, m):  # noqa: ANN001, ANN201
        self.sent.append(m)


def _server_with_node(node_id: str = "k") -> tuple[TranscribingServer, _RecordingWS]:
    srv = TranscribingServer(
        {
            "stt": {"url": "http://x/transcribe"},
            "speaker": {"url": "http://x/identify"},
            "llm": {"url": "http://x/process"},
        }
    )
    ws = _RecordingWS()
    srv._nodes[node_id] = NodeSession(ws=ws, node_id=node_id, room_id="kitchen")  # type: ignore[arg-type]
    # These tests mock _call_llm (the buffered stage), not the /process/stream
    # endpoint. Streaming defaults ON since 4.4.2, so leaving it enabled makes
    # every dispatch attempt a real HTTP stream against a dead URL first — slow,
    # and its cue ladder pollutes what the cue assertions are measuring. Tests
    # that want the streaming path turn it back on explicitly.
    srv._streaming_enabled = False
    return srv, ws


def _wire(srv, monkeypatch, *, stt_text: str, replies: list[LlmReply], continues: list) -> None:
    async def stt(pcm, room, sid):  # noqa: ANN001, ANN202
        return stt_text

    async def spk(pcm, room):  # noqa: ANN001, ANN202
        return "alice", 0.9

    async def llm(text, room, sid, speaker, node_id=None, identity=None):  # noqa: ANN001, ANN202
        return replies.pop(0)

    async def llm_continue(cont_id, text, identity):  # noqa: ANN001, ANN202
        continues.append((cont_id, text, identity.display if identity else None))
        return replies.pop(0)

    async def tts(*a, **k):  # noqa: ANN002, ANN003, ANN202
        return True

    cancels: list = []
    srv._test_cancels = cancels  # type: ignore[attr-defined]

    async def cancel_now(cont_id, reason):  # noqa: ANN001, ANN202
        cancels.append((cont_id, reason))

    monkeypatch.setattr(srv, "_call_stt", stt)
    monkeypatch.setattr(srv, "_call_speaker", spk)
    monkeypatch.setattr(srv, "_call_llm", llm)
    monkeypatch.setattr(srv, "_call_llm_continue", llm_continue)
    monkeypatch.setattr(srv, "_cancel_continuation_now", cancel_now)
    monkeypatch.setattr(srv, "_run_tts", tts)


async def test_ask_reply_holds_floor_and_routes_answer(monkeypatch):
    srv, ws = _server_with_node()
    continues: list = []
    _wire(
        srv,
        monkeypatch,
        stt_text="add milk to the list",
        replies=[
            LlmReply("Should I create a list?", "vp", fast=True,
                     expect_response=True, continuation="c1"),  # fmt: skip
            LlmReply("Created it.", "vp", fast=True),
        ],
        continues=continues,
    )
    await srv._transcribe("k", "kitchen", "s1", b"pcm")
    assert srv._pending_ask.get("k") == {
        "id": "c1", "capture": "text", "origin_node": "k", "origin_room": "kitchen",
        "busy_cues": True,
    }  # fmt: skip
    assert any(protocol.MSG_EXPECT_UTTERANCE in m for m in ws.sent)  # floor held

    # The next captured utterance is the ANSWER — routed to continue, not dispatch.
    await srv._transcribe("k", "kitchen", "s1", b"pcm2")
    assert continues == [("c1", "add milk to the list", "alice")]
    assert "k" not in srv._pending_ask


async def _run_ask_answer(monkeypatch, *, busy_cues: bool) -> list[str]:
    """Drive question turn + a SLOW answer turn under a shortened cue ladder;
    return the cue keys that fired during the answer's processing."""
    from kenzy.server import server as srvmod

    monkeypatch.setattr(
        srvmod, "_CUE_LADDER", ((10, "sound_thinking", "thinking.wav"),)
    )
    srv, ws = _server_with_node()
    continues: list = []
    _wire(
        srv,
        monkeypatch,
        stt_text="yes please",
        replies=[
            LlmReply("Should I create a list?", "vp", fast=True,
                     expect_response=True, continuation="c1",
                     ask_busy_cues=busy_cues),  # fmt: skip
            LlmReply("Created it.", "vp", fast=True),
        ],
        continues=continues,
    )
    cues: list[str] = []

    async def fake_cue(node_id, key, default):  # noqa: ANN001, ANN202
        cues.append(key)
        return 0.0

    monkeypatch.setattr(srv, "_play_cue", fake_cue)

    # Make the continuation SLOW — well past the (shortened) first rung.
    real_continue = srv._call_llm_continue

    async def slow_continue(cont_id, text, identity):  # noqa: ANN001, ANN202
        await asyncio.sleep(0.1)
        return await real_continue(cont_id, text, identity)

    monkeypatch.setattr(srv, "_call_llm_continue", slow_continue)

    await srv._transcribe("k", "kitchen", "s1", b"pcm")  # question turn
    await srv._transcribe("k", "kitchen", "s1", b"pcm2")  # slow ANSWER turn
    assert continues == [("c1", "yes please", "alice")]
    await asyncio.sleep(0.05)
    return cues


async def test_ask_answer_cues_by_default(monkeypatch):
    # The ladder applies to ask() answer turns by default: a skill can do real
    # work after your "yes" (list creation, enrollment upload), and mid-dialog
    # there is no bed — the cue is the only feedback that work would get.
    cues = await _run_ask_answer(monkeypatch, busy_cues=True)
    assert cues == ["sound_thinking"]


async def test_ask_busy_cues_false_keeps_answer_turn_silent(monkeypatch):
    # ask(busy_cues=False): a conversational skill (knock-knock) keeps its
    # turnarounds clean — a canned "Working on it." mid-joke reads as a barge.
    cues = await _run_ask_answer(monkeypatch, busy_cues=False)
    assert cues == []


async def _run_plain_dispatch(monkeypatch, *, held_floor: bool) -> list[str]:
    """Drive a FRESH (non-ask) dispatch through _transcribe with a slow buffered
    LLM under a shortened ladder; held_floor simulates a mid-dialog follow-up."""
    from kenzy.server import server as srvmod

    monkeypatch.setattr(srvmod, "_CUE_LADDER", ((10, "sound_thinking", "thinking.wav"),))
    srv, ws = _server_with_node()  # buffered path (see the fixture)
    cues: list[str] = []

    async def stt(pcm, room, sid):  # noqa: ANN001, ANN202
        return "what's the weather"

    async def spk(pcm, room):  # noqa: ANN001, ANN202
        return "alice", 0.9

    async def slow_llm(text, room, sid, speaker, node_id=None, identity=None):  # noqa: ANN001, ANN202
        await asyncio.sleep(0.1)
        return LlmReply("It's sunny.", "vp", fast=False)

    async def tts(*a, **k):  # noqa: ANN002, ANN003, ANN202
        return True

    async def fake_cue(node_id, key, default):  # noqa: ANN001, ANN202
        cues.append(key)
        return 0.0

    monkeypatch.setattr(srv, "_call_stt", stt)
    monkeypatch.setattr(srv, "_call_speaker", spk)
    monkeypatch.setattr(srv, "_call_llm", slow_llm)
    monkeypatch.setattr(srv, "_run_tts", tts)
    monkeypatch.setattr(srv, "_play_cue", fake_cue)
    if held_floor:
        srv._followup_turns["k"] = 1  # a prior reply is awaiting this answer
    await srv._transcribe("k", "kitchen", "s1", b"pcm")
    await asyncio.sleep(0.05)
    return cues


async def test_fresh_command_gets_cues(monkeypatch):
    # A fresh command into the void: the cue acknowledges it.
    cues = await _run_plain_dispatch(monkeypatch, held_floor=False)
    assert cues == ["sound_thinking"]


async def test_streaming_fallback_does_not_replay_the_cue_ladder(monkeypatch):
    """4.4.2: with streaming on by default, a stream that hangs past a rung and
    THEN falls back used to hand the buffered path a fresh ladder — you heard
    "Working on it." twice. The buffered path continues, it doesn't restart."""
    from kenzy.server import server as srvmod

    monkeypatch.setattr(srvmod, "_CUE_LADDER", ((10, "sound_thinking", "thinking.wav"),))
    srv, ws = _server_with_node()
    srv._streaming_enabled = True
    cues: list[str] = []

    async def stt(pcm, room, sid):  # noqa: ANN001, ANN202
        return "what's the weather"

    async def spk(pcm, room):  # noqa: ANN001, ANN202
        return "alice", 0.9

    async def slow_stream(*a, **k):  # noqa: ANN002, ANN003, ANN202
        # Outlive the rung, then fall back (the reachable-but-failing provider).
        played: list[int] | None = k.get("cue_played")
        ladder = srvmod._CueLadder(srv, "k", started_at=k.get("started_at"))
        await asyncio.sleep(0.08)
        await ladder.finish()
        if played is not None:
            played.append(ladder.played)
        return None

    async def slow_llm(text, room, sid, speaker, node_id=None, identity=None):  # noqa: ANN001, ANN202
        await asyncio.sleep(0.1)
        return LlmReply("It's sunny.", "vp", fast=False)

    async def tts(*a, **k):  # noqa: ANN002, ANN003, ANN202
        return True

    async def fake_cue(node_id, key, default):  # noqa: ANN001, ANN202
        cues.append(key)
        return 0.0

    monkeypatch.setattr(srv, "_call_stt", stt)
    monkeypatch.setattr(srv, "_call_speaker", spk)
    monkeypatch.setattr(srv, "_call_llm_stream", slow_stream)
    monkeypatch.setattr(srv, "_call_llm", slow_llm)
    monkeypatch.setattr(srv, "_run_tts", tts)
    monkeypatch.setattr(srv, "_play_cue", fake_cue)
    await srv._transcribe("k", "kitchen", "s1", b"pcm")
    await asyncio.sleep(0.05)
    assert cues == ["sound_thinking"]  # spoken once, by the streaming attempt


async def test_followup_turn_suppresses_cues(monkeypatch):
    # Mid-dialog follow-up (Kenzy held the floor, user is answering): no cue —
    # "Working on it." between the answer and her next line breaks the rhythm.
    cues = await _run_plain_dispatch(monkeypatch, held_floor=True)
    assert cues == []


async def test_wakeword_cancels_pending_ask(monkeypatch):
    srv, ws = _server_with_node()
    posted: list = []

    def cancel(node_id, reason):  # noqa: ANN001, ANN202
        posted.append((node_id, reason))
        srv._pending_ask.pop(node_id, None)

    monkeypatch.setattr(srv, "_cancel_pending_ask", cancel)
    srv._pending_ask["k"] = {"id": "c9", "capture": "text"}
    await srv.on_wakeword(srv._nodes["k"], "hey_ken_zee", 0.9)
    assert posted and posted[0][1] == "wakeword"


async def test_stop_phrase_and_silence_cancel(monkeypatch):
    for stt_text, reason in (("stop", "stop phrase"), ("", "silence")):
        srv, ws = _server_with_node()
        continues: list = []
        _wire(srv, monkeypatch, stt_text=stt_text, replies=[], continues=continues)
        srv._pending_ask["k"] = {"id": "c2", "capture": "text"}
        canceled: list = []
        monkeypatch.setattr(
            srv,
            "_cancel_pending_ask",
            lambda node_id, r: (canceled.append(r), srv._pending_ask.pop(node_id, None)),
        )
        await srv._transcribe("k", "kitchen", "s1", b"pcm")
        assert canceled == [reason]
        assert continues == []  # never routed as an answer


async def test_window_expiry_and_disconnect_cancel(monkeypatch):
    srv, _ = _server_with_node()
    canceled: list = []
    monkeypatch.setattr(
        srv,
        "_cancel_pending_ask",
        lambda node_id, r: (canceled.append(r), srv._pending_ask.pop(node_id, None)),
    )
    srv._pending_ask["k"] = {"id": "c3", "capture": "text"}
    srv._followup_timed_out("k")
    assert canceled == ["reply window expired"]

    srv._pending_ask["k"] = {"id": "c4", "capture": "text"}
    srv._cleanup_on_disconnect("k")
    assert canceled[-1] == "node disconnected"


async def test_cancel_pending_ask_posts_to_llm(monkeypatch):
    # The real _cancel_pending_ask fires a background POST /process/cancel.
    srv, _ = _server_with_node()
    posted: list = []

    async def fake_post(url, json=None, timeout=None, headers=None):  # noqa: ANN001, ANN202
        posted.append((url, json))

        class R:
            status_code = 200

        return R()

    class FakeClient:
        def __init__(self, **kw):  # noqa: ANN003
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):  # noqa: ANN002
            return False

        post = staticmethod(fake_post)

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    srv._pending_ask["k"] = {"id": "c5", "capture": "text"}
    srv._cancel_pending_ask("k", "wakeword")
    await asyncio.sleep(0.05)  # let the fire-and-forget task run
    assert posted and posted[0][0].endswith("/process/cancel")
    assert posted[0][1]["continuation"] == "c5"


async def test_notify_route_pokes_memory_listeners(monkeypatch):
    # /notify?what=memory (kenzy-llm's debounced poke) → dashboard listeners.
    srv, _ = _server_with_node()
    seen = []
    srv.add_memory_listener(lambda: seen.append(1))
    monkeypatch.setattr(srv, "_authorize_service", lambda req, m, p: (True, None))

    class _Req:
        path = "/notify?what=memory"

    resp = await srv._process_config_request(None, _Req())
    assert resp is not None and seen == [1]

    class _Other:
        path = "/notify?what=nothing"

    await srv._process_config_request(None, _Other())
    assert seen == [1]  # unknown kinds are a polite no-op


async def test_notify_route_requires_token(monkeypatch):
    srv, _ = _server_with_node()
    monkeypatch.setattr(srv, "_authorize_service", lambda req, m, p: (False, None))

    class _Req:
        path = "/notify?what=memory"

    resp = await srv._process_config_request(None, _Req())
    assert resp is not None and getattr(resp, "status_code", None) == 401


async def test_ask_chain_outlives_dialog_turn_cap(monkeypatch):
    # Review follow-up: an ask() chain (enrollment = 5 prompts + retries) must
    # not be cut off by dialog.max_turns; plain dialog holds still are.
    srv, ws = _server_with_node()
    replies = [
        LlmReply(f"Prompt {i}?", "vp", expect_response=True, continuation="c1")
        for i in range(12)
    ]
    continues: list = []
    _wire(srv, monkeypatch, stt_text="answer", replies=replies, continues=continues)
    await srv._transcribe("k", "kitchen", "s1", b"pcm")
    for _ in range(11):
        assert "k" in srv._pending_ask, "ask chain was cut off early"
        await srv._transcribe("k", "kitchen", "s1", b"pcm")
    assert srv._followup_turns["k"] == 12  # well past the dialog cap of 6
    assert srv._test_cancels == []  # never "floor not held"


async def test_plain_dialog_hold_still_capped(monkeypatch):
    srv, ws = _server_with_node()
    replies = [
        LlmReply(f"Chatty {i}.", "vp", expect_response=True) for i in range(7)
    ]
    _wire(srv, monkeypatch, stt_text="go on", replies=replies, continues=[])
    for _ in range(6):
        await srv._transcribe("k", "kitchen", "s1", b"pcm")
    assert srv._followup_turns["k"] == 6
    # The 7th hold attempt hits the cap and ends the dialog (counter cleared).
    await srv._transcribe("k", "kitchen", "s1", b"pcm")
    assert "k" not in srv._followup_turns
