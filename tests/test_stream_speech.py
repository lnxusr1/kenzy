"""4.4 server streaming: the sentence splitter's exact-join invariant and the
_StreamSpeech session state machine (lazy start, trailing-off, remainder,
divergence, silence-by-choice)."""

from __future__ import annotations

from kenzy.server.server import LlmReply, _split_sentences, _StreamSpeech

# --- _split_sentences -------------------------------------------------------


def test_slices_join_back_exactly():
    buf = "First one.  Second — with a dash! Third? And a tail without end"
    sentences, rest = _split_sentences(buf)
    assert "".join(sentences) + rest == buf
    assert [s.strip() for s in sentences] == [
        "First one.",
        "Second — with a dash!",
        "Third?",
    ]
    assert rest == "And a tail without end"


def test_decimals_do_not_split():
    sentences, rest = _split_sentences("It is 93.5 degrees outside. More later")
    assert [s.strip() for s in sentences] == ["It is 93.5 degrees outside."]
    assert rest == "More later"


def test_no_terminator_is_all_remainder():
    sentences, rest = _split_sentences("no end in sight")
    assert sentences == []
    assert rest == "no end in sight"


def test_terminator_without_trailing_space_stays_pending():
    # The stream may pause exactly at "…blue." — without the following space we
    # can't yet know the sentence ended (could be "blue.5" or '."').
    sentences, rest = _split_sentences("The sky is blue.")
    assert sentences == []
    assert rest == "The sky is blue."


def test_closing_quote_rides_with_the_sentence():
    sentences, rest = _split_sentences('She said "stop." Then left. ')
    assert [s.strip() for s in sentences] == ['She said "stop."', "Then left."]
    assert rest == ""


# --- _StreamSpeech ----------------------------------------------------------


class _FakeServer:
    def __init__(self, fail_synth_on: set[str] | None = None):
        self._tts_chunk_size = 8
        self._tts_active: set[str] = set()
        self.synth_calls: list[tuple[str, str, bool]] = []
        self.frames: list[bytes] = []
        self.starts: list[str] = []
        self.ends: list[str] = []
        self.stops: list[str] = []
        self._fail_on = fail_synth_on or set()

    async def _synthesize(self, text, voice_prompt, *, sensitive=False):
        self.synth_calls.append((text, voice_prompt, sensitive))
        if text in self._fail_on:
            return None
        return b"\x01\x02" * 12  # 24 bytes → 3 frames at chunk size 8

    async def send_tts_start(self, node_id, sid, sample_rate=24000, channels=1, stream=False):
        assert stream is True  # streamed replies always flag the session
        self.starts.append(sid)
        return True

    async def send_tts_frame(self, node_id, chunk):
        self.frames.append(chunk)
        return True

    async def send_tts_end(self, node_id, sid):
        self.ends.append(sid)
        return True

    async def stop_node(self, node_id):
        self.stops.append(node_id)


def _speech(srv, **kw):
    return _StreamSpeech(srv, "n1", "sid-1", **kw)


async def test_lazy_start_frames_and_exact_prefix_tracking():
    srv = _FakeServer()
    sp = _speech(srv)
    await sp.speak("Hello there. ")
    await sp.speak("Second one! ")
    assert srv.starts == ["sid-1"]  # started once, on first audio
    assert len(srv.frames) == 6  # 2 sentences × 3 chunks
    assert sp.spoken == "Hello there. Second one! "  # raw slices, exact
    assert srv.synth_calls[0][0] == "Hello there."  # stripped for TTS


async def test_close_speaks_only_the_remainder():
    srv = _FakeServer()
    sp = _speech(srv)
    await sp.speak("Blue scatters. ")
    reply = LlmReply(text="Blue scatters. That is why.", voice_prompt="Calm.")
    assert await sp.close(reply) is True
    # remainder synthesized with the reply's authoritative voice_prompt
    assert srv.synth_calls[-1] == ("That is why.", "Calm.", False)
    assert srv.ends == ["sid-1"]


async def test_close_with_nothing_streamed_speaks_everything():
    srv = _FakeServer()
    sp = _speech(srv)
    reply = LlmReply(text="Short answer.", voice_prompt="Flat.")
    assert await sp.close(reply) is True
    assert [c[0] for c in srv.synth_calls] == ["Short answer."]


async def test_diverged_final_text_adds_nothing():
    srv = _FakeServer()
    sp = _speech(srv)
    await sp.speak("What was heard. ")
    reply = LlmReply(text="Entirely different.", voice_prompt="")
    assert await sp.close(reply) is True  # spoken stands; no double-speak
    assert [c[0] for c in srv.synth_calls] == ["What was heard."]


async def test_synth_failure_trails_off_honestly():
    srv = _FakeServer(fail_synth_on={"Second."})
    sp = _speech(srv)
    await sp.speak("First. ")
    await sp.speak("Second. ")
    assert sp.failed
    await sp.speak("Third. ")  # ignored after failure
    reply = LlmReply(text="First. Second. Third.", voice_prompt="")
    assert await sp.close(reply) is True  # partial speech ⇒ no error cue
    assert [c[0] for c in srv.synth_calls] == ["First.", "Second."]
    assert srv.ends == ["sid-1"]  # session still closed cleanly


async def test_total_failure_reports_false_for_the_error_cue():
    srv = _FakeServer(fail_synth_on={"Only sentence."})
    sp = _speech(srv)
    reply = LlmReply(text="Only sentence.", voice_prompt="")
    assert await sp.close(reply) is False  # nothing spoken ⇒ caller cues
    assert srv.starts == []  # session never opened


async def test_empty_reply_is_silence_by_choice():
    srv = _FakeServer()
    sp = _speech(srv)
    assert await sp.close(LlmReply(text="", voice_prompt="")) is True


async def test_on_first_audio_fires_before_session_start():
    order: list[str] = []
    srv = _FakeServer()
    orig = srv.send_tts_start

    async def start(node_id, sid, **kw):
        order.append("start")
        return await orig(node_id, sid, **kw)

    srv.send_tts_start = start

    async def first():
        order.append("first_audio")

    sp = _speech(srv, on_first_audio=first)
    await sp.speak("Hi there. ")
    assert order == ["first_audio", "start"]


async def test_abort_closes_session_once():
    srv = _FakeServer()
    sp = _speech(srv)
    await sp.speak("Something. ")
    await sp.abort()
    await sp.abort()  # idempotent
    assert srv.ends == ["sid-1"]
    # closed: further speech and close() are no-ops
    await sp.speak("More. ")
    assert [c[0] for c in srv.synth_calls] == ["Something."]


def test_stream_buffer_pending_tracks_drain():
    import numpy as np

    from kenzy.node.client import _StreamBuffer

    buf = _StreamBuffer()
    assert not buf.pending
    buf.feed(np.ones(10, dtype=np.int16))
    assert buf.pending
    buf.read(4)
    assert buf.pending  # partially drained
    buf.read(6)
    assert not buf.pending  # fully consumed
    buf.feed(np.ones(3, dtype=np.int16))
    buf.clear()
    assert not buf.pending
