"""One-breath commands: "Hey Kenzy turn on the lights", no pause.

The wake gate holds the ready chime for wake_onset_ms after a hit. Speech that
keeps going means the command is already underway — the chime would land on top
of it, so the session opens silently and the buffered onset (including a sliver
of pre-roll around the hit) is flushed so the first word survives whole. Silence
for the whole window is the classic pause flow: chime, then listen, exactly as
before — just a window later. The server never hears about a gate that produced
nothing, and a leaked wake-phrase tail is stripped from the transcript before
fast-intent matching sees it.
"""

from __future__ import annotations

import json
from typing import Any

import numpy as np

from kenzy import protocol
from kenzy.node.client import _STATE_IDLE, _STATE_STREAMING, NodeClient
from kenzy.server.server import _strip_wake_prefix


class _WS:
    def __init__(self) -> None:
        self.sent: list[Any] = []

    async def send(self, data: Any) -> None:
        self.sent.append(data)


class _Player:
    def __init__(self) -> None:
        self.chimes = 0
        self.active = False
        self.looping = False
        self.bed_active = False
        self.cue_remaining_s = 0.0

    def play(self) -> None:
        self.chimes += 1

    def play_pcm(self, audio: Any, **kwargs: Any) -> None:
        pass

    def abort(self) -> None:
        pass


def _client(**cfg: Any) -> tuple[NodeClient, _WS, _Player]:
    c = NodeClient({"node_id": "n1", "room_id": "office", **cfg})
    ws = _WS()
    c._ws = ws  # type: ignore[assignment]
    player = _Player()
    c._player = player  # type: ignore[assignment]
    return c, ws, player


def _texts(ws: _WS) -> list[dict[str, Any]]:
    return [json.loads(m) for m in ws.sent if isinstance(m, str)]


def _binaries(ws: _WS) -> list[bytes]:
    return [m for m in ws.sent if isinstance(m, bytes)]


FRAME = np.zeros(1280, dtype=np.int16)  # silence: RMS 0
LOUD = np.full(1280, 3000, dtype=np.int16)  # speech-level energy
# The wake gate classifies by RMS against the calibrated silence threshold —
# deliberately NOT Silero, whose cold-start lag ruled a sentence in full
# flight "silent" on the rig. These tests drive real energy levels.


# ---------------------------------------------------------------------------
# Arming
# ---------------------------------------------------------------------------


async def test_gated_wake_opens_silently_and_sends_nothing():
    c, ws, player = _client()
    await c._begin_streaming("sid1", wake_gated=True, gate_preroll=[FRAME, FRAME])
    assert player.chimes == 0  # the chime is held, not played
    assert ws.sent == []  # the server hasn't heard about this session
    assert c._wake_gate is True and c._onset_pending is True
    assert len(c._onset_buf) == 2  # pre-roll rides into the gate buffer


async def test_gate_disabled_is_the_pre_gate_behavior():
    c, ws, player = _client(wake_onset_ms=0)
    await c._begin_streaming("sid1", wake_gated=True, gate_preroll=[FRAME])
    assert player.chimes == 1  # instant chime, exactly as before
    starts = [m for m in _texts(ws) if m.get("type") == protocol.MSG_AUDIO_START]
    assert len(starts) == 1  # immediate audio_start, no gate
    assert c._wake_gate is False


async def test_server_trigger_never_gates():
    # A `trigger` has no wake phrase the user might be talking through; the
    # call sites that aren't wake paths don't pass wake_gated at all.
    c, ws, player = _client()
    await c._begin_streaming("sid1")
    assert player.chimes == 1
    assert c._wake_gate is False


# ---------------------------------------------------------------------------
# The two outcomes
# ---------------------------------------------------------------------------


async def test_continued_speech_opens_silently_with_flush():
    c, ws, player = _client()
    await c._begin_streaming("sid1", wake_gated=True, gate_preroll=[FRAME, FRAME])
    for _ in range(c._dialog_onset_frames):
        await c._handle_onset_frame(LOUD)
    assert player.chimes == 0  # never chimed — the sentence was in flight
    starts = [m for m in _texts(ws) if m.get("type") == protocol.MSG_AUDIO_START]
    assert len(starts) == 1
    # pre-roll + every gate frame flushed: the first word survives whole
    assert len(_binaries(ws)) == 2 + c._dialog_onset_frames
    assert c._state == _STATE_STREAMING and c._onset_pending is False
    assert c._wake_gate is False


async def test_silent_window_falls_back_to_chime_and_listen():
    c, ws, player = _client()
    await c._begin_streaming("sid1", wake_gated=True, gate_preroll=[FRAME])
    for _ in range(c._wake_onset_frames):
        await c._handle_onset_frame(FRAME)
    assert player.chimes == 1  # the held chime plays now
    starts = [m for m in _texts(ws) if m.get("type") == protocol.MSG_AUDIO_START]
    assert len(starts) == 1  # a normal session opens
    assert _binaries(ws) == []  # the buffered silence is dropped, not sent
    assert c._state == _STATE_STREAMING and c._onset_pending is False


async def test_speech_starting_at_the_window_edge_still_confirms():
    # Silence right up to the edge, then speech mid-window-boundary: a burst in
    # flight at expiry time is allowed to finish and confirm — expiry requires
    # run == 0, not merely elapsed ≥ window.
    c, ws, player = _client()
    await c._begin_streaming("sid1", wake_gated=True, gate_preroll=[])
    frames = [FRAME] * (c._wake_onset_frames - 1) + [LOUD] * c._dialog_onset_frames
    for f in frames:
        await c._handle_onset_frame(f)
    assert player.chimes == 0
    starts = [m for m in _texts(ws) if m.get("type") == protocol.MSG_AUDIO_START]
    assert len(starts) == 1
    assert c._state == _STATE_STREAMING and c._wake_gate is False


# ---------------------------------------------------------------------------
# Teardown honesty
# ---------------------------------------------------------------------------


async def test_ending_a_pending_gate_tells_the_server_nothing():
    # A held follow-up floor needs releasing on expiry; a wake gate was never
    # announced, so a stop during it must not invent a followup_timeout.
    c, ws, player = _client()
    await c._begin_streaming("sid1", wake_gated=True, gate_preroll=[FRAME])
    await c._end_streaming("stop")
    assert ws.sent == []
    assert c._state == _STATE_IDLE and c._wake_gate is False


async def test_wake_onset_tunes_live():
    c, _, _ = _client()
    assert c._wake_onset_frames == 400 // protocol.FRAME_MS
    c._apply_pulled_config({"wake_onset_ms": 0})
    assert c._wake_onset_frames == 0
    c._apply_pulled_config({"wake_onset_ms": 800})
    assert c._wake_onset_frames == 800 // protocol.FRAME_MS


# ---------------------------------------------------------------------------
# The transcript strip (server-side belt)
# ---------------------------------------------------------------------------


def test_leading_wake_phrase_is_stripped():
    assert _strip_wake_prefix("Hey Kenzy, turn on the lights") == "turn on the lights"
    assert _strip_wake_prefix("hey kenzy turn on the lights") == "turn on the lights"
    assert _strip_wake_prefix("Hey Kenzie! What's the weather") == "What's the weather"
    assert _strip_wake_prefix("Hey, Kenzy, what's up") == "what's up"
    # Whisper's rendering varies by SPEAKER: a real voice produced "Kinsey"
    # where the rig's synth voice yields "Kenzy" — and a stop command must
    # never ride past the strip to the model (found live, 2026-08-01).
    assert _strip_wake_prefix("Hey Kinsey, stop.") == "stop."
    assert _strip_wake_prefix("Hey, Kinsey, never mind.") == "never mind."
    assert _strip_wake_prefix("Hey Kinzie, what time is it?") == "what time is it?"


def test_leaked_tail_fragment_is_stripped():
    assert _strip_wake_prefix("Zee, turn on the lights") == "turn on the lights"
    assert _strip_wake_prefix("Z turn on the lights") == "turn on the lights"
    # Pre-roll starting mid-"Hey": a bare or mangled-hey leading name is still
    # the wake phrase. Judged tradeoff (see _WAKE_PREFIX_RE): a transcript-
    # leading "Kenzy" followed by more words is address, not content.
    assert _strip_wake_prefix("Kenzy, turn on the lights") == "turn on the lights"
    assert _strip_wake_prefix("A Kenzy turn on the lights") == "turn on the lights"


def test_content_is_never_mistaken_for_the_wake_phrase():
    # Bare phrase: nothing follows, so nothing is stripped (she answers "yes?").
    assert _strip_wake_prefix("Hey Kenzy") == "Hey Kenzy"
    assert _strip_wake_prefix("Kenzy") == "Kenzy"
    # Words that merely start with z, or contain her name later on.
    assert _strip_wake_prefix("Zebra facts please") == "Zebra facts please"
    assert _strip_wake_prefix("turn on the lights") == "turn on the lights"
    # Only the FIRST occurrence, only at the start.
    assert _strip_wake_prefix("play hey kenzy the song") == "play hey kenzy the song"
    # "Kensington" must not read as "Kenzy" + separator.
    assert _strip_wake_prefix("Kensington station is closed") == "Kensington station is closed"
