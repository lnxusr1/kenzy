"""4.4 processing-cue ladder: escalating spoken acknowledgements ("Working on
it." → "Still working on it.") while the LLM stage runs long — armed around the
LLM calls, cancelled when the reply is fast, allowed to finish (session-clean)
when a cue races the reply, and silent after the last rung (restraint). Timing
model (founder-tuned): rung 1 delay is absolute; later rungs' delays are GAPS
from when the previous cue finished playing."""

from __future__ import annotations

import asyncio
from pathlib import Path

from kenzy.server import server as srv
from kenzy.server.server import TranscribingServer


def _server() -> TranscribingServer:
    return TranscribingServer({})


def _capture_cues(monkeypatch, s: TranscribingServer, duration: float = 0.0) -> list[str]:
    played: list[str] = []

    async def fake_cue(node_id: str, key: str, default: str) -> float:
        played.append(key)
        return duration

    monkeypatch.setattr(s, "_play_cue", fake_cue)
    return played


async def test_fast_reply_never_cues(monkeypatch):
    monkeypatch.setattr(srv, "_CUE_LADDER", ((200, "sound_thinking", "thinking.wav"),))
    s = _server()
    played = _capture_cues(monkeypatch, s)

    async def quick() -> str:
        return "done"

    assert await s._with_backchannel("n1", quick()) == "done"
    await asyncio.sleep(0.3)  # past the threshold — the timer must be dead
    assert played == []


async def test_slow_reply_cues_first_rung_only(monkeypatch):
    monkeypatch.setattr(
        srv,
        "_CUE_LADDER",
        ((20, "sound_thinking", "thinking.wav"), (500, "sound_working", "working.wav")),
    )
    s = _server()
    played = _capture_cues(monkeypatch, s)

    async def slow() -> str:
        await asyncio.sleep(0.15)
        return "done"

    assert await s._with_backchannel("n1", slow()) == "done"
    assert played == ["sound_thinking"]
    await asyncio.sleep(0.6)  # the second rung must have been cancelled
    assert played == ["sound_thinking"]


async def test_very_slow_reply_walks_both_rungs_then_stops(monkeypatch):
    monkeypatch.setattr(
        srv,
        "_CUE_LADDER",
        ((20, "sound_thinking", "thinking.wav"), (60, "sound_working", "working.wav")),
    )
    s = _server()
    played = _capture_cues(monkeypatch, s)

    async def very_slow() -> str:
        await asyncio.sleep(0.25)
        return "done"

    assert await s._with_backchannel("n1", very_slow()) == "done"
    assert played == ["sound_thinking", "sound_working"]  # each rung exactly once


async def test_second_rung_gap_counts_from_first_cue_end(monkeypatch):
    # Founder finding (rig, 2026-07-23): absolute deadlines made rung 2 land
    # right after rung 1 finished. The gap must run from the END of the first
    # clip: rung1 at 20ms + clip 300ms + gap 100ms ⇒ rung 2 no earlier than
    # ~420ms — NOT at the naive 20+100=120ms.
    monkeypatch.setattr(
        srv,
        "_CUE_LADDER",
        ((20, "sound_thinking", "thinking.wav"), (100, "sound_working", "working.wav")),
    )
    s = _server()
    stamps: list[float] = []

    async def fake_cue(node_id: str, key: str, default: str) -> float:
        stamps.append(asyncio.get_running_loop().time())
        return 0.3  # pretend the clip is 300ms of audio

    monkeypatch.setattr(s, "_play_cue", fake_cue)

    async def very_slow() -> str:
        await asyncio.sleep(0.8)
        return "done"

    assert await s._with_backchannel("n1", very_slow()) == "done"
    assert len(stamps) == 2
    assert stamps[1] - stamps[0] >= 0.38  # ≥ clip (0.3) + gap (0.1), minus jitter


async def test_first_rung_anchored_to_wait_start(monkeypatch):
    # Founder finding (rig, 2026-07-24): the first cue landed ~8s into the wait
    # for a configured 5s, because STT (~3s dev CPU) ran BEFORE the ladder and
    # the delay counted from LLM dispatch. started_at anchors it to the wait
    # start, so elapsed STT time is subtracted from the first rung.
    import time as _time

    monkeypatch.setattr(srv, "_CUE_LADDER", ((500, "sound_thinking", "thinking.wav"),))
    s = _server()
    fired_at: list[float] = []

    async def fake_cue(node_id, key, default):  # noqa: ANN001, ANN202
        fired_at.append(_time.monotonic())
        return 0.0

    monkeypatch.setattr(s, "_play_cue", fake_cue)

    async def slow() -> str:
        await asyncio.sleep(0.7)
        return "done"

    # Pretend 0.4s of the 0.5s delay was already burned by STT before dispatch.
    started = _time.monotonic() - 0.4
    dispatch = _time.monotonic()
    await s._with_backchannel("n1", slow(), started_at=started)
    assert fired_at, "the cue should have fired"
    # It fires ~0.1s after dispatch (0.5 − 0.4), NOT the full 0.5s.
    assert fired_at[0] - dispatch < 0.3


async def test_first_rung_without_started_at_measures_from_creation(monkeypatch):
    import time as _time

    monkeypatch.setattr(srv, "_CUE_LADDER", ((300, "sound_thinking", "thinking.wav"),))
    s = _server()
    fired_at: list[float] = []

    async def fake_cue(node_id, key, default):  # noqa: ANN001, ANN202
        fired_at.append(_time.monotonic())
        return 0.0

    monkeypatch.setattr(s, "_play_cue", fake_cue)

    async def slow() -> str:
        await asyncio.sleep(0.6)
        return "done"

    dispatch = _time.monotonic()
    await s._with_backchannel("n1", slow())  # no started_at → legacy timing
    assert fired_at
    assert fired_at[0] - dispatch >= 0.3  # full delay, from creation


async def test_cues_false_runs_no_ladder(monkeypatch):
    # A mid-dialog follow-up turn suppresses the cue ladder entirely (founder:
    # "Working on it." between the user's answer and Kenzy's next line is noise).
    monkeypatch.setattr(srv, "_CUE_LADDER", ((10, "sound_thinking", "thinking.wav"),))
    s = _server()
    played = _capture_cues(monkeypatch, s)

    async def slow() -> str:
        await asyncio.sleep(0.15)
        return "done"

    assert await s._with_backchannel("n1", slow(), cues=False) == "done"
    await asyncio.sleep(0.05)
    assert played == []  # no cue, even though the stage ran long


async def test_mid_play_cue_finishes_before_return(monkeypatch):
    # The reply lands while a cue is streaming: the clip must complete (its
    # TTS session closes) before _with_backchannel returns.
    monkeypatch.setattr(srv, "_CUE_LADDER", ((10, "sound_thinking", "thinking.wav"),))
    s = _server()
    finished: list[bool] = []

    async def slow_cue(node_id: str, key: str, default: str) -> None:
        await asyncio.sleep(0.1)
        finished.append(True)

    monkeypatch.setattr(s, "_play_cue", slow_cue)

    async def reply() -> str:
        await asyncio.sleep(0.05)  # cue already started, still playing
        return "done"

    assert await s._with_backchannel("n1", reply()) == "done"
    assert finished == [True]


async def test_llm_error_still_cancels_pending_cue(monkeypatch):
    monkeypatch.setattr(srv, "_CUE_LADDER", ((200, "sound_thinking", "thinking.wav"),))
    s = _server()
    played = _capture_cues(monkeypatch, s)

    async def boom() -> str:
        raise RuntimeError("llm down")

    try:
        await s._with_backchannel("n1", boom())
        raise AssertionError("should have raised")
    except RuntimeError:
        pass
    await asyncio.sleep(0.3)
    assert played == []


async def test_cue_resolves_default_sound_and_flags_session(monkeypatch):
    s = _server()
    monkeypatch.setattr(s, "_effective_node_config", lambda node_id: {})
    specs: list[str] = []
    streamed: list[tuple[bytes, bool]] = []

    from kenzy.server import tones

    def fake_load(spec: str) -> bytes:
        specs.append(spec)
        return b"\x00\x01"

    async def fake_stream(node_id: str, pcm: bytes, **kw) -> None:
        streamed.append((pcm, bool(kw.get("cue"))))

    monkeypatch.setattr(tones, "load_tone", fake_load)
    monkeypatch.setattr(s, "_stream_pcm", fake_stream)
    await s._play_cue("n1", "sound_thinking", "thinking.wav")
    assert specs == ["thinking.wav"]
    # Cue sessions ride tts_start(cue=True) so the node duck-mixes over the bed.
    assert streamed == [(b"\x00\x01", True)]


async def test_empty_sound_key_stays_silent(monkeypatch):
    s = _server()
    monkeypatch.setattr(s, "_effective_node_config", lambda node_id: {"sound_thinking": ""})
    streamed: list[bytes] = []

    async def fake_stream(node_id: str, pcm: bytes, **kw) -> None:
        streamed.append(pcm)

    monkeypatch.setattr(s, "_stream_pcm", fake_stream)
    await s._play_cue("n1", "sound_thinking", "thinking.wav")
    assert streamed == []


# ---------------------------------------------------------------------------
# Cue pools (Slice 1): string-or-list config, random pick, no immediate repeat
# ---------------------------------------------------------------------------


def test_pick_cue_plain_string():
    s = _server()
    assert s._pick_cue("n1", "sound_thinking", "thinking.wav") == "thinking.wav"
    assert s._pick_cue("n1", "sound_thinking", "") == ""
    assert s._pick_cue("n1", "sound_thinking", None) == ""


def test_pick_cue_pool_never_repeats_back_to_back():
    s = _server()
    pool = ["a.wav", "b.wav", "c.wav"]
    picks = [s._pick_cue("n1", "sound_thinking", pool) for _ in range(50)]
    assert all(p in pool for p in picks)
    assert all(x != y for x, y in zip(picks, picks[1:]))  # no immediate repeat


def test_pick_cue_pool_single_item_and_empties():
    s = _server()
    # A one-item pool always plays that item (repeat guard must not starve it).
    assert [s._pick_cue("n1", "k", ["only.wav"]) for _ in range(3)] == ["only.wav"] * 3
    assert s._pick_cue("n1", "k", []) == ""
    assert s._pick_cue("n1", "k", ["", "  "]) == ""


def test_pick_cue_no_repeat_is_per_node():
    s = _server()
    pool = ["a.wav", "b.wav"]
    a = s._pick_cue("n1", "k", pool)
    # A different node's pick is independent — n1's guard doesn't constrain n2.
    assert s._pick_cue("n2", "k", pool) in pool
    assert s._pick_cue("n1", "k", pool) != a


def test_cue_file_spec_prefers_library_render(tmp_path: Path):
    s = _server()
    root = tmp_path / "sounds"
    root.mkdir()
    (root / "thinking.wav").write_bytes(b"riff")
    s._sound_roots = [root]
    # Library render shadows the bundled name...
    assert s._cue_file_spec("thinking.wav") == str(root / "thinking.wav")
    # ...anything not in the library passes through untouched.
    assert s._cue_file_spec("working.wav") == "working.wav"
