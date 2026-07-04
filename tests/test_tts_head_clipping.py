"""Regression: the first word of an announcement must never be clipped.

_recv_loop enqueues binary TTS frames the moment they arrive, while tts_start is
processed later by the cmd loop — so a session's own head frames can already be
in _tts_q when _begin_tts runs. _begin_tts used to drain the queue at that point,
eating the head of the audio (the intermittent clipped-first-word bug on
announcements). Cleanup now happens only where stale data can exist: session
abort and connection teardown.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from kenzy.node.client import NodeClient


class _FakePlayer:
    def __init__(self) -> None:
        self.played: list[np.ndarray[Any, Any]] = []
        self.aborted = False

    def play_pcm(self, audio: np.ndarray[Any, Any], interrupt: bool = False) -> None:
        self.played.append(audio)

    def abort(self) -> None:
        self.aborted = True

    @property
    def active(self) -> bool:
        return False


def _client() -> tuple[NodeClient, _FakePlayer]:
    c = NodeClient({"node_id": "n1"})
    player = _FakePlayer()
    c._player = player  # type: ignore[assignment]
    c._playback_rate = 24000
    return c, player


async def test_frames_arriving_before_tts_start_survive():
    """The race: PCM frames land in _tts_q before the cmd loop runs _begin_tts."""
    c, player = _client()
    head = (b"\x01\x00" * 1200, b"\x02\x00" * 1200)
    for f in head:  # frames beat tts_start's processing — the announcement burst
        c._tts_q.put_nowait(f)

    await c._begin_tts("sid1", 24000, 1)
    c._tts_q.put_nowait(b"\x03\x00" * 1200)  # the rest of the stream
    await c._end_tts(reason="complete")

    (audio,) = player.played
    expected = np.frombuffer(b"".join([*head, b"\x03\x00" * 1200]), dtype=np.int16)
    assert audio.size == expected.size  # nothing eaten from the head
    assert np.array_equal(audio, expected)  # ...and order preserved from sample 0


async def test_aborted_session_frames_do_not_leak_into_next():
    """Inverse guard: an interrupted session's leftovers are cleared at abort."""
    c, player = _client()
    await c._begin_tts("sid1", 24000, 1)
    c._tts_q.put_nowait(b"\x09\x00" * 1200)  # stale — session gets cut
    await c._stop_tts_playback()
    assert c._tts_q.empty()  # abort cleans up

    await c._begin_tts("sid2", 24000, 1)
    c._tts_q.put_nowait(b"\x05\x00" * 1200)
    await c._end_tts(reason="complete")

    (audio,) = player.played
    assert np.array_equal(audio, np.frombuffer(b"\x05\x00" * 1200, dtype=np.int16))


async def test_non_complete_end_discards_frames():
    """server_stop mid-stream: nothing plays, nothing remains queued."""
    c, player = _client()
    await c._begin_tts("sid1", 24000, 1)
    c._tts_q.put_nowait(b"\x07\x00" * 1200)
    await c._end_tts(reason="server_stop")
    assert player.played == []
    assert c._tts_q.empty()
