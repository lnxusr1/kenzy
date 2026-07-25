"""4.4 presence audio: the looping waiting bed, the duck-under cue overlay, the
node's cue-flagged tts sessions, and voice-matched cue regeneration.

Player tests drive the RT callback directly on a hardware-less _SoundPlayer
(the test_volume.py pattern); node tests use a _FakePlayer-style harness."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from kenzy.node.client import (
    _STATE_IDLE,
    _STATE_TTS,
    NodeClient,
    _SoundPlayer,
    _StreamBuffer,
)
from kenzy.server.server import TranscribingServer

# ---------------------------------------------------------------------------
# Hardware-less player
# ---------------------------------------------------------------------------


def _bare_player(audio: np.ndarray | None = None) -> _SoundPlayer:
    p = _SoundPlayer.__new__(_SoundPlayer)
    p._volume = 1.0
    p._duck = 1.0
    p._muted = False
    p._streaming = False
    p._ring = _StreamBuffer()
    p._restart = False
    p._interrupt = False
    p._alert = False
    p._pending_alert = False
    p._loop = False
    p._loop_left = 0
    p._overlay_backup = None
    p._overlay_next = None
    p._sample_rate = 24000
    sample = (
        audio.reshape(-1, 1)
        if audio is not None
        else np.full((4, 1), 1000, dtype=np.int16)
    )
    p._audio = sample
    p._chime = sample
    p._pending = sample
    p._pos = len(sample)
    return p


def _tick(p: _SoundPlayer, frames: int) -> np.ndarray:
    out = np.zeros((frames, 1), dtype=np.int16)
    p._callback(out, frames, None, None)
    return out[:, 0]


# ---------------------------------------------------------------------------
# Looping bed
# ---------------------------------------------------------------------------


def test_bed_loops_seamlessly_across_the_wrap():
    bed = np.arange(1, 7, dtype=np.int16)  # 6 samples: 1..6
    p = _bare_player()
    p.play_pcm(bed, loop=True)
    # 8 frames spans the wrap: 1..6 then 1,2 again — no silence gap.
    assert list(_tick(p, 8)) == [1, 2, 3, 4, 5, 6, 1, 2]
    assert list(_tick(p, 4)) == [3, 4, 5, 6]


def test_non_loop_playback_still_ends():
    bed = np.arange(1, 5, dtype=np.int16)
    p = _bare_player()
    p.play_pcm(bed)
    assert list(_tick(p, 6)) == [1, 2, 3, 4, 0, 0]
    assert list(_tick(p, 4)) == [0, 0, 0, 0]


def test_loop_is_bounded_by_max_wraps():
    bed = np.ones(4, dtype=np.int16)
    p = _bare_player()
    p.play_pcm(bed, loop=True)
    p._loop_left = 2  # tighten the _LOOP_MAX_S bound for the test
    # First pass + 2 wraps = 12 samples of audio, then silence forever.
    assert list(_tick(p, 12)) == [1] * 12
    assert list(_tick(p, 4)) == [0, 0, 0, 0]


def test_every_replacement_clears_the_loop():
    bed = np.ones(4, dtype=np.int16)
    for clear in (
        lambda p: p.play_pcm(np.zeros(2, dtype=np.int16)),
        lambda p: p.play(),
        lambda p: p.abort(),
        lambda p: p.start_stream(),
        lambda p: p.stop_stream(),
    ):
        p = _bare_player()
        p.play_pcm(bed, loop=True)
        assert p.looping
        clear(p)
        assert not p.looping
        assert p._overlay_backup is None and p._overlay_next is None


# ---------------------------------------------------------------------------
# Duck-under overlay
# ---------------------------------------------------------------------------


def test_overlay_mixes_ducked_bed_under_cue_then_restores():
    bed = np.full(100, 1000, dtype=np.int16)
    cue = np.full(10, 8000, dtype=np.int16)
    p = _bare_player()
    p.play_pcm(bed, loop=True)
    _tick(p, 10)  # cursor at 10
    assert p.overlay(cue, duck=0.25, lead=4)
    out = _tick(p, 30)  # covers lead(4) + cue(10) + tail
    assert list(out[:4]) == [1000] * 4  # lead: untouched bed
    assert list(out[4:14]) == [8250] * 10  # ducked bed (250) + cue (8000)
    assert list(out[14:]) == [1000] * 16  # bed back at full level
    # After the wrap, the clean bed is restored — the cue region never replays.
    _tick(p, 60)  # drain exactly to the seam (wrap fires on the next read)
    assert list(_tick(p, 30)) == [1000] * 30  # wrapped: pristine bed, no remnant
    assert p._overlay_backup is None


def test_overlay_straddling_the_seam_plays_the_tail_then_restores():
    bed = np.full(20, 1000, dtype=np.int16)
    cue = np.full(8, 4000, dtype=np.int16)
    p = _bare_player()
    p.play_pcm(bed, loop=True)
    _tick(p, 14)  # cursor at 14; lead 2 ⇒ overlay at 16..24 → straddles 20
    assert p.overlay(cue, duck=0.5, lead=2)
    out = _tick(p, 12)  # 14..26 (wraps at 20)
    assert list(out[:2]) == [1000] * 2  # 14..16 lead
    assert list(out[2:6]) == [4500] * 4  # 16..20: ducked (500) + cue
    assert list(out[6:10]) == [4500] * 4  # 0..4 after the wrap: cue tail
    assert list(out[10:]) == [1000] * 2  # bed continues clean
    # Second wrap restores the pristine bed — the seam region never replays.
    _tick(p, 20)
    assert p._overlay_backup is None and p._overlay_next is None
    assert list(_tick(p, 20)) == [1000] * 20


def test_overlay_refuses_without_a_bed_or_with_an_oversized_cue():
    p = _bare_player()
    assert not p.overlay(np.ones(4, dtype=np.int16))  # nothing looping
    bed = np.ones(10, dtype=np.int16)
    p.play_pcm(bed, loop=True)
    _tick(p, 2)
    assert not p.overlay(np.ones(10, dtype=np.int16))  # cue >= bed length
    assert not p.overlay(np.zeros(0, dtype=np.int16))  # empty cue
    p.play_pcm(np.ones(20, dtype=np.int16), loop=True)
    p.start_stream()
    assert not p.overlay(np.ones(4, dtype=np.int16))  # streaming mode


def test_overlay_clips_instead_of_wrapping():
    bed = np.full(50, 20000, dtype=np.int16)
    cue = np.full(5, 20000, dtype=np.int16)
    p = _bare_player()
    p.play_pcm(bed, loop=True)
    _tick(p, 5)
    assert p.overlay(cue, duck=1.0, lead=2)  # 20000+20000 would overflow int16
    out = _tick(p, 12)
    assert out.max() == 32767  # clipped, not wrapped to negative


# ---------------------------------------------------------------------------
# Node cue sessions (tts_start cue=True)
# ---------------------------------------------------------------------------


class _CuePlayer:
    """Player stub recording overlay/play_pcm calls, with a controllable bed."""

    def __init__(self) -> None:
        self.looping = False
        self.streaming = False
        self.overlays: list[Any] = []
        self.played: list[Any] = []
        self.overlay_ok = True
        self.aborted = False

    def play_pcm(self, audio, interrupt=False, alert=False, loop=False):  # noqa: ANN001
        self.played.append(audio)
        self.looping = loop

    def overlay(self, cue, duck=0.25, lead=2400):  # noqa: ANN001
        if not (self.looping and self.overlay_ok):
            return False
        self.overlays.append(cue)
        return True

    def play(self):
        pass

    def abort(self):
        self.aborted = True
        self.looping = False

    def start_stream(self):
        self.streaming = True

    def stop_stream(self):
        self.streaming = False

    @property
    def stream_pending(self):
        return False

    @property
    def active(self):
        return False


def _node() -> tuple[NodeClient, _CuePlayer]:
    client = NodeClient({"node_id": "n1", "room_id": "office"})
    player = _CuePlayer()
    client._player = player  # type: ignore[assignment]
    return client, player


async def test_cue_session_overlays_onto_looping_bed():
    client, player = _node()
    player.looping = True  # the waiting bed is playing
    await client._begin_tts("c1", 24000, 1, cue=True)
    assert client._state == _STATE_TTS and client._tts_cue
    client._tts_q.put_nowait(b"\x00\x01" * 480)
    await client._end_tts(reason="complete")
    assert len(player.overlays) == 1  # duck-mixed over the bed...
    assert player.played == []  # ...never a hard replacement
    assert client._state == _STATE_IDLE  # session over immediately
    assert not client._tts_cue


async def test_cue_session_without_bed_falls_back_to_plain_playback():
    client, player = _node()
    assert not player.looping  # no waiting bed (dialog turn / disabled)
    await client._begin_tts("c1", 24000, 1, cue=True)
    client._tts_q.put_nowait(b"\x00\x01" * 480)
    await client._end_tts(reason="complete")
    assert player.overlays == []
    assert len(player.played) == 1  # normal interrupt playback
    # The normal wait-done path owns the IDLE transition here.
    await client._tts_task
    assert client._state == _STATE_IDLE


async def test_reply_session_is_never_cue_flagged():
    client, player = _node()
    player.looping = True
    await client._begin_tts("r1", 24000, 1)  # a real reply
    client._tts_q.put_nowait(b"\x00\x01" * 480)
    await client._end_tts(reason="complete")
    assert player.overlays == []  # replies replace the bed outright
    assert len(player.played) == 1
    await client._tts_task


async def test_reset_tts_state_aborts_a_looping_bed():
    client, player = _node()
    player.looping = True  # bed mid-loop when the connection tears down
    await client._reset_tts_state()
    assert player.aborted and not player.looping


# ---------------------------------------------------------------------------
# Cue regeneration (Slice 2)
# ---------------------------------------------------------------------------


def _regen_server(tmp_path: Path) -> TranscribingServer:
    s = TranscribingServer({"tts": {"url": "http://127.0.0.1:1/speak"}})
    s._sound_roots = [tmp_path / "sounds"]
    return s


async def test_regenerate_cues_renders_pools_and_returns_keys(tmp_path: Path):
    s = _regen_server(tmp_path)
    s._cue_texts = {
        "error": ["Sorry."],
        "thinking": ["On it.", "One sec."],
        "working": ["Still going."],
    }
    synth: list[str] = []

    async def fake_synth(text: str, vp: str, *, sensitive: bool = False) -> bytes:
        synth.append(text)
        return b"\x00\x01" * 100

    s._synthesize = fake_synth  # type: ignore[method-assign]
    result = await s.regenerate_cues()
    assert result["count"] == 4
    assert synth == ["Sorry.", "On it.", "One sec.", "Still going."]
    keys = result["keys"]
    assert keys["sound_error"] == "cues/error-1.wav"
    assert keys["sound_thinking"] == ["cues/thinking-1.wav", "cues/thinking-2.wav"]
    assert keys["sound_working"] == "cues/working-1.wav"
    outdir = tmp_path / "sounds" / "cues"
    assert sorted(p.name for p in outdir.glob("*.wav")) == [
        "error-1.wav",
        "thinking-1.wav",
        "thinking-2.wav",
        "working-1.wav",
    ]
    # And the renders resolve through the library-first cue lookup.
    assert s._cue_file_spec("cues/thinking-2.wav") == str(outdir / "thinking-2.wav")


async def test_regenerate_replaces_a_previously_larger_pool(tmp_path: Path):
    s = _regen_server(tmp_path)
    stale = tmp_path / "sounds" / "cues"
    stale.mkdir(parents=True)
    (stale / "thinking-9.wav").write_bytes(b"old")

    async def fake_synth(text: str, vp: str, *, sensitive: bool = False) -> bytes:
        return b"\x00\x01" * 10

    s._synthesize = fake_synth  # type: ignore[method-assign]
    await s.regenerate_cues()
    assert not (stale / "thinking-9.wav").exists()  # stale render gone


async def test_regenerate_failure_leaves_previous_renders_intact(tmp_path: Path):
    s = _regen_server(tmp_path)
    outdir = tmp_path / "sounds" / "cues"
    outdir.mkdir(parents=True)
    (outdir / "error-1.wav").write_bytes(b"previous-voice")

    async def failing_synth(text: str, vp: str, *, sensitive: bool = False) -> bytes | None:
        return None  # TTS down

    s._synthesize = failing_synth  # type: ignore[method-assign]
    try:
        await s.regenerate_cues()
        raise AssertionError("should have raised")
    except RuntimeError:
        pass
    # All-or-nothing: the old render is still there.
    assert (outdir / "error-1.wav").read_bytes() == b"previous-voice"


async def test_regenerate_without_tts_refuses(tmp_path: Path):
    s = TranscribingServer({})
    try:
        await s.regenerate_cues()
        raise AssertionError("should have raised")
    except RuntimeError as exc:
        assert "TTS" in str(exc)


def test_apply_node_defaults_takes_effect_immediately():
    s = TranscribingServer({})
    s.apply_node_defaults({"sound_thinking": ["cues/thinking-1.wav"]})
    assert s._effective_node_config("nX")["sound_thinking"] == ["cues/thinking-1.wav"]


def test_cue_texts_default_and_config_override():
    s = TranscribingServer({})
    assert s._cue_texts["thinking"] == ["Working on it."]
    s2 = TranscribingServer({"cues": {"thinking": ["A.", "B."], "error": "Oops."}})
    assert s2._cue_texts["thinking"] == ["A.", "B."]
    assert s2._cue_texts["error"] == ["Oops."]
    assert s2._cue_texts["working"] == ["Still working on it."]
    # Malformed/empty entries fall back to the shipped defaults.
    s3 = TranscribingServer({"cues": {"thinking": [], "working": 42}})
    assert s3._cue_texts["thinking"] == ["Working on it."]
    assert s3._cue_texts["working"] == ["Still working on it."]
