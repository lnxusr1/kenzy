"""Multi-turn dialog: the server re-arms the mic (expect_utterance) when a reply
holds the floor, and ends the dialog on completion / silence / stop / cap.

Also covers the LLM service's expect_response parsing + closer suppression."""

from __future__ import annotations

from kenzy import protocol
from kenzy.server.server import _MAX_FOLLOWUP_TURNS, NodeSession, TranscribingServer


class _RecordingWS:
    """Stub node websocket that records the control frames the server sends."""

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
    return srv, ws


def _mock_pipeline(srv, monkeypatch, *, response: str, expect: bool) -> None:
    async def stt(pcm, room, sid):  # noqa: ANN001, ANN202
        return "tell me a knock knock joke"

    async def spk(pcm, room):  # noqa: ANN001, ANN202
        return "alice"

    async def llm(text, room, sid, speaker, node_id=None):  # noqa: ANN001, ANN202
        return (response, "vp", [], False, expect)

    async def tts(*a):  # noqa: ANN002, ANN202
        return True

    monkeypatch.setattr(srv, "_call_stt", stt)
    monkeypatch.setattr(srv, "_call_speaker", spk)
    monkeypatch.setattr(srv, "_call_llm", llm)
    monkeypatch.setattr(srv, "_run_tts", tts)


def _expect_utterance_count(ws: _RecordingWS) -> int:
    return sum(1 for m in ws.sent if protocol.MSG_EXPECT_UTTERANCE in m)


def _end_dialog_count(ws: _RecordingWS) -> int:
    return sum(1 for m in ws.sent if protocol.MSG_END_DIALOG in m)


# ---------------------------------------------------------------------------
# Server re-arm / exit behaviour
# ---------------------------------------------------------------------------


async def test_holds_floor_when_expect_response(monkeypatch):
    srv, ws = _server_with_node()
    _mock_pipeline(srv, monkeypatch, response="Knock knock.", expect=True)
    await srv._transcribe("k", "kitchen", "sid", b"1234")
    assert _expect_utterance_count(ws) == 1  # mic re-armed for the follow-up
    assert srv._followup_turns["k"] == 1


async def test_no_hold_when_not_expect_response(monkeypatch):
    srv, ws = _server_with_node()
    _mock_pipeline(srv, monkeypatch, response="The lights are on.", expect=False)
    await srv._transcribe("k", "kitchen", "sid", b"1234")
    assert _expect_utterance_count(ws) == 0
    assert "k" not in srv._followup_turns


async def test_no_end_cue_on_single_turn(monkeypatch):
    # A plain single-turn reply must never play the end-of-dialog cue.
    srv, ws = _server_with_node()
    _mock_pipeline(srv, monkeypatch, response="The lights are on.", expect=False)
    await srv._transcribe("k", "kitchen", "sid", b"1234")
    assert _end_dialog_count(ws) == 0


async def test_natural_close_is_silent_no_end_cue(monkeypatch):
    """Stage 1 sound language: a dialog that ends with a final spoken reply gets
    NO end cue — the reply is the closure. (The cue now means only "I stopped
    waiting" and is played by the NODE when a follow-up window expires.)"""
    srv, ws = _server_with_node()
    _mock_pipeline(srv, monkeypatch, response="Knock knock.", expect=True)
    await srv._transcribe("k", "kitchen", "sid", b"1234")
    assert _end_dialog_count(ws) == 0
    _mock_pipeline(srv, monkeypatch, response="Lettuce in, it's cold!", expect=False)
    await srv._transcribe("k", "kitchen", "sid", b"1234")
    assert _end_dialog_count(ws) == 0  # natural close: silent
    assert "k" not in srv._followup_turns


async def test_followup_arms_with_silent_cue(monkeypatch):
    """Dialog follow-ups open silently: expect_utterance carries cue=false."""
    import json

    srv, ws = _server_with_node()
    _mock_pipeline(srv, monkeypatch, response="Knock knock.", expect=True)
    await srv._transcribe("k", "kitchen", "sid", b"1234")
    arms = [
        json.loads(m)
        for m in ws.sent
        if isinstance(m, str) and '"expect_utterance"' in m
    ]
    assert arms and arms[-1]["cue"] is False


async def test_followup_timeout_message_clears_floor():
    srv, ws = _server_with_node()
    srv._followup_turns["k"] = 2
    srv._followup_timed_out("k")
    assert "k" not in srv._followup_turns


async def test_dialog_ends_when_reply_stops_holding(monkeypatch):
    srv, ws = _server_with_node()
    # Turn 1 holds the floor.
    _mock_pipeline(srv, monkeypatch, response="Knock knock.", expect=True)
    await srv._transcribe("k", "kitchen", "sid", b"1234")
    assert srv._followup_turns["k"] == 1
    # Turn 2 completes (punchline) — no more holding → dialog ends, counter cleared.
    _mock_pipeline(srv, monkeypatch, response="Lettuce in, it's cold!", expect=False)
    await srv._transcribe("k", "kitchen", "sid", b"1234")
    assert "k" not in srv._followup_turns


async def test_turn_cap_stops_runaway_hold(monkeypatch):
    srv, ws = _server_with_node()
    _mock_pipeline(srv, monkeypatch, response="And another?", expect=True)
    # A model that asks to hold every turn re-arms at most _MAX_FOLLOWUP_TURNS times…
    for _ in range(_MAX_FOLLOWUP_TURNS):
        await srv._transcribe("k", "kitchen", "sid", b"1234")
    assert _expect_utterance_count(ws) == _MAX_FOLLOWUP_TURNS
    assert srv._followup_turns["k"] == _MAX_FOLLOWUP_TURNS
    # …then the next turn does NOT re-arm and ends the dialog (counter cleared). In
    # production the mic isn't reopened at the cap, so a further turn needs a fresh
    # wake word — which legitimately starts a new dialog.
    await srv._transcribe("k", "kitchen", "sid", b"1234")
    assert _expect_utterance_count(ws) == _MAX_FOLLOWUP_TURNS
    assert "k" not in srv._followup_turns


async def test_silence_ends_held_dialog(monkeypatch):
    srv, ws = _server_with_node()
    _mock_pipeline(srv, monkeypatch, response="Knock knock.", expect=True)
    await srv._transcribe("k", "kitchen", "sid", b"1234")
    assert srv._followup_turns["k"] == 1

    # Follow-up capture returns empty (user went silent) → dialog ends.
    async def empty_stt(pcm, room, sid):  # noqa: ANN001, ANN202
        return ""

    async def noop_stop(nid):  # noqa: ANN001, ANN202
        return None

    monkeypatch.setattr(srv, "_call_stt", empty_stt)
    monkeypatch.setattr(srv, "stop_node", noop_stop)
    await srv._transcribe("k", "kitchen", "sid", b"1234")
    assert "k" not in srv._followup_turns


async def test_disconnect_clears_followup(monkeypatch):
    srv, ws = _server_with_node()
    _mock_pipeline(srv, monkeypatch, response="Knock knock.", expect=True)
    await srv._transcribe("k", "kitchen", "sid", b"1234")
    assert srv._followup_turns["k"] == 1
    srv._cleanup_on_disconnect("k")
    assert "k" not in srv._followup_turns


# ---------------------------------------------------------------------------
# LLM service: expect_response parsing + closer suppression
# ---------------------------------------------------------------------------


def test_parse_response_reads_expect_response():
    from kenzy.llm import llm

    _t, _v, expect = llm._parse_response('{"text": "Knock knock.", "expect_response": true}')
    assert expect is True
    _t, _v, expect = llm._parse_response('{"text": "The lights are on."}')
    assert expect is False


def test_suppress_floor_hold_for_closers():
    from kenzy.llm import llm

    # Reflexive closers are suppressed even if the model set the flag.
    assert llm._suppress_floor_hold("Done. Is there anything else?", True) is False
    assert llm._suppress_floor_hold("Sorry, I didn't quite understand.", True) is False
    # A genuine floor-hold is preserved.
    assert llm._suppress_floor_hold("Who's there?", True) is True
    # Never promotes false → true.
    assert llm._suppress_floor_hold("Is there anything else?", False) is False
