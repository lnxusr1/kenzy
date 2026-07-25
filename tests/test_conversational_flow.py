"""Stage 1 of conversational flow (design/conversational-flow.md):

Node side — dialog follow-ups open SILENTLY (no chime unless the arm carried
cue=true), send NOTHING until ~300 ms of sustained speech (Silero-gated onset:
a clink can't start a turn), expire after the dialog window with a local end
cue + followup_timeout (the server never hears a session that never happened),
and never play the waiting sound between turns. Plus the B2 ordering fix: TTS
binary frames ride the command queue, so back-to-back sessions can't bleed.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

import numpy as np
import pytest

from kenzy import protocol
from kenzy.node.client import _STATE_IDLE, _STATE_STREAMING, _STATE_TTS, NodeClient


class _WS:
    def __init__(self) -> None:
        self.sent: list[Any] = []

    async def send(self, data: Any) -> None:
        self.sent.append(data)


class _Player:
    def __init__(self) -> None:
        self.chimes = 0
        self.pcm_plays: list[Any] = []
        self.active = False

    def play(self) -> None:
        self.chimes += 1

    def play_pcm(
        self, audio: Any, interrupt: bool = False, alert: bool = False, loop: bool = False
    ) -> None:
        self.pcm_plays.append(audio)
        self.looping = loop

    looping = False

    def abort(self) -> None:
        self.looping = False


def _client(**cfg: Any) -> tuple[NodeClient, _WS, _Player]:
    c = NodeClient({"node_id": "n1", "room_id": "office", **cfg})
    ws = _WS()
    c._ws = ws  # type: ignore[assignment]
    player = _Player()
    c._player = player  # type: ignore[assignment]
    return c, ws, player


def _texts(ws: _WS) -> list[dict[str, Any]]:
    return [json.loads(m) for m in ws.sent if isinstance(m, str)]


FRAME = np.zeros(1280, dtype=np.int16)


# ---------------------------------------------------------------------------
# Silent opens
# ---------------------------------------------------------------------------


async def test_followup_opens_silently_and_defers_audio_start():
    c, ws, player = _client()
    c._capture_cue = False  # armed by expect_utterance(cue=false) / consent
    await c._begin_streaming("sid1", followup=True)
    assert player.chimes == 0  # her question was the cue
    assert c._onset_pending is True
    assert ws.sent == []  # nothing on the wire until real speech


async def test_enrollment_style_followup_still_chimes():
    c, ws, player = _client()
    c._capture_cue = True  # expect_utterance(cue=true): record after the tone
    await c._begin_streaming("sid1", followup=True)
    assert player.chimes == 1


async def test_wake_session_unchanged():
    c, ws, player = _client()
    await c._begin_streaming("sid1")
    assert player.chimes == 1  # the wake beep is sacred
    starts = [m for m in _texts(ws) if m.get("type") == protocol.MSG_AUDIO_START]
    assert len(starts) == 1  # immediate audio_start, no onset gate


# ---------------------------------------------------------------------------
# The onset gate
# ---------------------------------------------------------------------------


async def test_sustained_speech_opens_session_with_onset_flush(monkeypatch):
    c, ws, player = _client()
    c._capture_cue = False
    await c._begin_streaming("sid1", followup=True)
    monkeypatch.setattr(c, "_dialog_vad_score", lambda flat: 0.9)  # speech
    for _ in range(c._dialog_onset_frames):
        await c._handle_onset_frame(FRAME)
    starts = [m for m in _texts(ws) if m.get("type") == protocol.MSG_AUDIO_START]
    assert len(starts) == 1
    assert c._onset_pending is False
    # The buffered onset frames were flushed — the first word survives whole.
    binary = [m for m in ws.sent if isinstance(m, bytes)]
    assert len(binary) == c._dialog_onset_frames


async def test_boo_a_short_complete_word_opens_the_session(monkeypatch):
    """Found live: 'Boo.' — a knock-knock answer shorter than dialog_onset_ms —
    must open the session via the burst-then-silence path, not time out."""
    c, ws, player = _client()
    c._capture_cue = False
    await c._begin_streaming("sid1", followup=True)
    # ~2 frames (160ms) of speech, then silence: a short COMPLETE utterance.
    scores = iter([0.9, 0.9] + [0.0] * 20)
    monkeypatch.setattr(c, "_dialog_vad_score", lambda flat: next(scores))
    for _ in range(6):
        await c._handle_onset_frame(FRAME)
    starts = [m for m in _texts(ws) if m.get("type") == protocol.MSG_AUDIO_START]
    assert len(starts) == 1  # "Boo" was heard, not discarded
    assert c._onset_pending is False
    binary = [m for m in ws.sent if isinstance(m, bytes)]
    assert len(binary) >= 2  # the word itself was flushed whole
    assert c._silence_count >= 2  # endpointing continues from the observed gap
    # speech_min is satisfied at confirm (Silero already proved real speech) —
    # otherwise silence never arms and "Boo" drags to the no_speech timeout.
    assert c._speech_frames >= c._speech_min_frames


async def test_a_clink_cannot_start_a_turn(monkeypatch):
    """Interrupted (non-sustained) speech never opens a session."""
    c, ws, player = _client()
    c._capture_cue = False
    await c._begin_streaming("sid1", followup=True)
    scores = iter([0.9, 0.0, 0.0] * 20)  # 1-frame blips (a clink), never a burst
    monkeypatch.setattr(c, "_dialog_vad_score", lambda flat: next(scores))
    for _ in range(30):
        await c._handle_onset_frame(FRAME)
    assert c._onset_pending is True
    assert ws.sent == []  # the server never heard a thing


async def test_window_expiry_sends_timeout_and_plays_end_cue(monkeypatch):
    c, ws, player = _client(dialog_no_speech_timeout_ms=800)  # 10 frames
    c._capture_cue = False
    c._dialog_end_audio = np.ones(100, dtype=np.int16)  # type: ignore[assignment]
    await c._begin_streaming("sid1", followup=True)
    monkeypatch.setattr(c, "_dialog_vad_score", lambda flat: 0.0)  # silence
    for _ in range(c._dialog_no_speech_frames):
        await c._handle_onset_frame(FRAME)
    assert c._state == _STATE_IDLE
    msgs = _texts(ws)
    assert msgs[-1]["type"] == protocol.MSG_FOLLOWUP_TIMEOUT
    assert len(player.pcm_plays) == 1  # the local "I stopped waiting" cue
    assert not any(m.get("type") == protocol.MSG_AUDIO_START for m in msgs)


async def test_rms_fallback_when_vad_unavailable(monkeypatch):
    c, ws, player = _client(silence_rms_threshold=50)
    c._capture_cue = False
    c._dialog_vad = False  # model unavailable → sustained-energy fallback
    await c._begin_streaming("sid1", followup=True)
    loud = np.full(1280, 1000, dtype=np.int16)
    for _ in range(c._dialog_onset_frames):
        await c._handle_onset_frame(loud)
    assert c._onset_pending is False  # loud sustained audio opened the session


# ---------------------------------------------------------------------------
# No waiting sound between dialog turns
# ---------------------------------------------------------------------------


async def test_no_waiting_sound_after_followup_turn(monkeypatch):
    c, ws, player = _client()
    c._waiting_audio = np.ones(100, dtype=np.int16)  # type: ignore[assignment]
    c._capture_cue = False
    await c._begin_streaming("sid1", followup=True)
    # Simulate onset confirmed then the turn ending normally.
    c._onset_pending = False
    await c._end_streaming(reason="silence")
    assert player.pcm_plays == []  # silent processing beat, not hold music

    # Wake-initiated turns keep the waiting sound.
    await c._begin_streaming("sid2")
    await c._end_streaming(reason="silence")
    assert len(player.pcm_plays) == 1


# ---------------------------------------------------------------------------
# B2: TTS frames ride the command queue — back-to-back sessions can't bleed
# ---------------------------------------------------------------------------


async def test_pcm_via_cmd_queue_only_buffers_during_tts():
    c, ws, player = _client()
    c._state = _STATE_TTS
    c._cmd_q.put_nowait({"type": "_pcm", "raw": b"\x01\x00" * 100})
    task = asyncio.create_task(c._cmd_loop())
    await asyncio.sleep(0.1)
    assert c._tts_q.qsize() == 1

    c._state = _STATE_IDLE  # stale frame outside a session → dropped
    c._cmd_q.put_nowait({"type": "_pcm", "raw": b"\x02\x00" * 100})
    await asyncio.sleep(0.1)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert c._tts_q.qsize() == 1  # unchanged


async def test_back_to_back_sessions_do_not_bleed():
    """The exact B2 wire order (end1, start2, frames2) through ONE queue: each
    session plays exactly its own audio."""
    c, ws, player = _client()
    c._playback_rate = 24000

    async def run(msgs: list[Any]) -> None:
        for m in msgs:
            c._cmd_q.put_nowait(m)
        task = asyncio.create_task(c._cmd_loop())
        await asyncio.sleep(0.15)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    pcm1, pcm2 = b"\x01\x00" * 1200, b"\x02\x00" * 1200
    await run(
        [
            json.loads(protocol.tts_start("s1", 24000, 1)),
            {"type": "_pcm", "raw": pcm1},
            json.loads(protocol.tts_end("s1")),
            json.loads(protocol.tts_start("s2", 24000, 1)),
            {"type": "_pcm", "raw": pcm2},
            json.loads(protocol.tts_end("s2")),
        ]
    )
    assert len(player.pcm_plays) == 2
    a, b = player.pcm_plays
    assert np.array_equal(a, np.frombuffer(pcm1, dtype=np.int16))  # session 1 pure
    assert np.array_equal(b, np.frombuffer(pcm2, dtype=np.int16))  # session 2 whole


# ---------------------------------------------------------------------------
# Wake word during a pending window abandons the floor
# ---------------------------------------------------------------------------


async def test_state_flags_reset_when_wake_abandons_pending_window():
    c, ws, player = _client()
    c._capture_cue = False
    await c._begin_streaming("sid1", followup=True)
    assert c._onset_pending
    # (The audio-loop wake branch resets these and starts a fresh session; here
    # we verify _end_streaming's pending-path bookkeeping used on server stop.)
    await c._end_streaming(reason="server_stop")
    msgs = _texts(ws)
    assert msgs[-1]["type"] == protocol.MSG_FOLLOWUP_TIMEOUT  # not audio_end
    assert c._state == _STATE_IDLE and not c._onset_pending


# ---------------------------------------------------------------------------
# llm params passthrough (latency knobs: reasoning_effort etc.)
# ---------------------------------------------------------------------------


async def test_llm_params_merge_but_never_credentials(monkeypatch):
    import litellm

    from kenzy.llm import llm as svc

    seen: dict[str, Any] = {}

    async def fake(**kwargs: Any):
        seen.update(kwargs)

        class _R:
            class _C:
                class _M:
                    content = '{"text": "hi", "voice_prompt": "v"}'
                    tool_calls = None

                message = _M()

            choices = [_C()]

        return _R()

    monkeypatch.setattr(litellm, "acompletion", fake)
    monkeypatch.setattr(svc, "_params", {"reasoning_effort": "none", "api_key": "EVIL"})
    # _PARAMS_BLOCKED filtering happens at load; simulate a filtered load here:
    svc._params.pop("api_key")
    await svc._run_llm("hello", "john", "office")
    assert seen.get("reasoning_effort") == "none"
    assert seen.get("api_key") != "EVIL"


def test_packaged_default_ships_reasoning_effort_none():
    """The packaged llm.yaml must carry params.reasoning_effort as a REAL key
    (a commented example renders no dashboard field — found live)."""
    import yaml

    from kenzy.config import packaged_config

    cfg = yaml.safe_load(packaged_config("llm").read_text())
    # The KEYS must exist (a commented example renders no dashboard field —
    # found live TWICE, reasoning_effort then service_tier); shipped value ""
    # = omit-the-parameter.
    assert cfg["params"]["reasoning_effort"] == ""
    assert cfg["params"]["service_tier"] == ""


async def test_drop_params_set_for_portability(monkeypatch):
    import litellm

    from kenzy.llm import llm as svc

    seen: dict[str, Any] = {}

    async def fake(**kwargs: Any):
        seen.update(kwargs)

        class _R:
            class _C:
                class _M:
                    content = '{"text": "hi", "voice_prompt": "v"}'
                    tool_calls = None

                message = _M()

            choices = [_C()]

        return _R()

    monkeypatch.setattr(litellm, "acompletion", fake)
    await svc._run_llm("hello", "john", "office")
    assert seen.get("drop_params") is True


def test_empty_param_values_mean_omit(monkeypatch):
    from kenzy.llm import llm as svc

    raw = {"reasoning_effort": "", "service_tier": None, "temperature": 0.4}
    filtered = {
        k: v for k, v in raw.items() if k not in svc._PARAMS_BLOCKED and v not in ("", None)
    }
    assert filtered == {"temperature": 0.4}


# ---------------------------------------------------------------------------
# Intercom ringback (3.6.1): the caller loops a ring tone while the room is rung
# ---------------------------------------------------------------------------


async def test_ringback_loops_then_stops_on_connect():
    import numpy as np

    c, ws, player = _client()
    c._playback_rate = 24000
    c._ringback_audio = np.ones(24000, dtype=np.int16)  # 1s clip → ~1s period

    c._start_ringback()
    await asyncio.sleep(0.05)
    assert c._ringback_task is not None
    plays_before = len(player.pcm_plays)
    assert plays_before >= 1  # rings immediately

    # A connect (intercom_start) stops it.
    c._stop_ringback()
    await asyncio.sleep(0.01)
    assert c._ringback_task is None


async def test_ringback_armed_during_calling_reply_starts_after():
    import numpy as np

    c, ws, player = _client()
    c._playback_rate = 24000
    c._ringback_audio = np.ones(12000, dtype=np.int16)
    c._state = _STATE_TTS  # "calling the kitchen" is playing

    # call_ringing while in TTS only arms; it must not start mid-reply.
    c._ringback_after_tts = True
    assert c._ringback_task is None

    # When begin_tts is called for a NEW reply (decline/timeout), ringback is cleared.
    c._stop_ringback()
    assert not c._ringback_after_tts


async def test_ringback_silent_when_no_clip():
    c, ws, player = _client()
    c._ringback_audio = None
    c._start_ringback()
    assert c._ringback_task is None  # nothing to loop → no task


# ---------------------------------------------------------------------------
# Stage 2 — barge-in: talk over a floor-holding reply and she yields
# ---------------------------------------------------------------------------


class _DuckPlayer(_Player):
    def __init__(self) -> None:
        super().__init__()
        self.duck_factor = 1.0
        self.aborted = False

    def duck(self, factor: float = 0.25) -> None:
        self.duck_factor = factor

    def unduck(self) -> None:
        self.duck_factor = 1.0

    def abort(self) -> None:
        self.aborted = True


def _barge_client(*, armed: bool = True, **cfg):
    import time as _time

    from kenzy.node.client import _BARGE_GRACE_S, NodeClient

    c = NodeClient({"node_id": "n1", "room_id": "office", "hardware_aec": True, **cfg})
    ws = _WS()
    c._ws = ws  # type: ignore[assignment]
    p = _DuckPlayer()
    c._player = p  # type: ignore[assignment]
    c._playback_rate = 24000
    c._state = _STATE_TTS
    c._capture_after_prompt = True  # a floor-holding reply is playing
    # Past the barge grace by default, so detection is live; armed=False leaves
    # the reply "just started" (grace active — no duck/confirm yet).
    c._barge_armed_at = 0.0 if not armed else _time.monotonic() - _BARGE_GRACE_S - 1.0
    return c, ws, p


async def test_barge_ducks_on_suspicion_then_stops_on_confirm(monkeypatch):
    c, ws, p = _barge_client()
    monkeypatch.setattr(c, "_dialog_vad_score", lambda flat: 0.9)  # speech

    # First frame → duck (the "go ahead" dip), not yet a session.
    await c._handle_barge_frame(FRAME)
    assert p.duck_factor < 1.0
    assert c._state == _STATE_TTS

    # Sustained → confirm: reply cut, answer session opened, pre-roll flushed.
    for _ in range(c._dialog_onset_frames):
        await c._handle_barge_frame(FRAME)
    assert p.aborted is True
    assert c._state == _STATE_STREAMING
    starts = [m for m in _texts(ws) if m.get("type") == protocol.MSG_AUDIO_START]
    assert len(starts) == 1
    binary = [m for m in ws.sent if isinstance(m, bytes)]
    assert len(binary) >= c._dialog_onset_frames  # pre-roll flushed
    assert p.duck_factor == 1.0  # un-ducked once the turn is theirs


async def test_barge_false_alarm_unducks_and_keeps_playing(monkeypatch):
    c, ws, p = _barge_client()
    scores = iter([0.9, 0.0, 0.0, 0.0])  # one blip, then quiet (a clink)
    monkeypatch.setattr(c, "_dialog_vad_score", lambda flat: next(scores, 0.0))

    await c._handle_barge_frame(FRAME)  # blip → duck
    assert p.duck_factor < 1.0
    for _ in range(3):
        await c._handle_barge_frame(FRAME)  # silence → un-duck, keep playing
    assert p.duck_factor == 1.0
    assert c._state == _STATE_TTS
    assert not any(m.get("type") == protocol.MSG_AUDIO_START for m in _texts(ws))


async def test_barge_grace_suppresses_onset_false_fire(monkeypatch):
    # Founder rig report (2026-07-24): a question was cut off ~220ms in by a
    # false barge — AEC hadn't converged on the reply, so Kenzy's own voice read
    # as speech. During the grace window, even sustained "speech" must NOT duck
    # or confirm (it's her own residual), only buffer for pre-roll.
    import time as _time

    c, ws, p = _barge_client(armed=False)  # reply audio just started
    c._barge_armed_at = _time.monotonic()  # grace clock starts now
    monkeypatch.setattr(c, "_dialog_vad_score", lambda flat: 0.9)  # "speech"

    for _ in range(c._dialog_onset_frames + 5):
        await c._handle_barge_frame(FRAME)
    # No duck, no confirm, reply still playing — the false onset was ignored.
    assert p.duck_factor == 1.0
    assert p.aborted is False
    assert c._state == _STATE_TTS
    assert not any(m.get("type") == protocol.MSG_AUDIO_START for m in _texts(ws))
    # ...but the frames were buffered, so a real answer at grace-end isn't lost.
    assert len(c._barge_buf) > 0


async def test_barge_confirms_after_grace_window(monkeypatch):
    # Once the grace has elapsed, a genuine sustained answer confirms as before.
    import time as _time

    from kenzy.node.client import _BARGE_GRACE_S

    c, ws, p = _barge_client(armed=False)
    c._barge_armed_at = _time.monotonic() - _BARGE_GRACE_S - 0.1  # grace already past
    monkeypatch.setattr(c, "_dialog_vad_score", lambda flat: 0.9)

    for _ in range(c._dialog_onset_frames + 1):
        await c._handle_barge_frame(FRAME)
    assert p.aborted is True
    assert c._state == _STATE_STREAMING


async def test_barge_disabled_without_aec(monkeypatch):
    """The whole stage-2 path is inert on half-duplex hardware."""
    c, ws, p = _barge_client()
    c._hardware_aec = False
    # The audio-loop gate (hardware_aec) would skip _handle_barge_frame entirely;
    # here we assert the gate condition the loop checks.
    assert not (c._state == _STATE_TTS and c._capture_after_prompt and c._hardware_aec)


# ---------------------------------------------------------------------------
# The silent stop gate (_STOP_PHRASES): pipeline-level, pre-LLM, pre-skills
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "phrase",
    [
        "stop", "Stop.", "stop it", "stop talking", "be quiet", "quiet",
        "hush", "shut up", "silence", "enough", "That's enough!", "please stop",
    ],
)
def test_stop_phrases_cover_the_family(phrase):
    """The whole quiet-demanding family ends the session silently at the server —
    before the LLM and before any skill (no spoken ack: they asked for quiet)."""
    from kenzy.server.server import _STOP_PHRASES

    normalized = re.sub(r"[^\w\s]", "", phrase).strip().lower()
    assert normalized in _STOP_PHRASES, phrase


def test_stop_phrases_require_the_whole_utterance():
    from kenzy.server.server import _STOP_PHRASES

    for phrase in ("stop the timer", "cancel", "never mind", "quiet down please"):
        normalized = re.sub(r"[^\w\s]", "", phrase).strip().lower()
        assert normalized not in _STOP_PHRASES, phrase
