"""Voice-guided calibration ("Hey Kenzy, calibrate"): the fast-intent trigger,
and the server's guided flow — prompts, phase collection over tune telemetry,
the shared voice-anchored math, apply-to-override, and the restart handoff."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import time
from pathlib import Path

import pytest
import yaml

from kenzy import calibration
from kenzy.llm import skills as reg
from kenzy.server import server as srv_mod
from kenzy.server.server import NodeSession, TranscribingServer

ROOT = Path(__file__).resolve().parents[1]


class _RecWS:
    def __init__(self):
        self.sent: list[str] = []

    async def send(self, data):
        self.sent.append(data)


# ---------------------------------------------------------------------------
# Fast intent
# ---------------------------------------------------------------------------


@pytest.fixture
def calib_skill():
    reg.set_config({})
    path = ROOT / "src" / "kenzy" / "llm" / "builtin_skills" / "calibrate.py"
    spec = importlib.util.spec_from_file_location("calibrate_skill", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["calibrate_skill"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize(
    "utterance",
    [
        "calibrate",
        "Calibrate yourself",
        "please calibrate the microphone",
        "recalibrate your hearing",
        "run calibration",
        "run audio calibration please",
        "calibrate this room",
    ],
)
async def test_fast_calibrate_matches(calib_skill, utterance):
    reg.begin_actions()
    res = await calib_skill.fast_calibrate(utterance, "den", None)
    assert res.status == "handled"
    assert {"type": "start_calibration"} in reg.take_actions()


@pytest.mark.parametrize(
    "utterance",
    [
        "calibrate the thermostat",
        "what does calibrate mean",
        "is the microphone calibrated",
        "calibrate the tv remote",
    ],
)
async def test_fast_calibrate_misses(calib_skill, utterance):
    reg.begin_actions()
    res = await calib_skill.fast_calibrate(utterance, "den", None)
    assert res.status == "miss"


async def test_skill_queues_action(calib_skill):
    reg.begin_actions()
    out = await calib_skill.calibrate_audio()
    assert "calibration" in out.lower()
    assert {"type": "start_calibration"} in reg.take_actions()


# ---------------------------------------------------------------------------
# Server guided flow (fast clock: margins/refractory/phase lengths shrunk)
# ---------------------------------------------------------------------------


async def _run_flow(
    tmp_path,
    monkeypatch,
    *,
    mode="spoken",
    quiet_rms=15.0,
    speech_rms=800.0,
    echo_rms=None,  # mic level while the node plays the probe (None ⇒ quiet ⇒ AEC ok)
    wake_peaks=True,
    verify=True,  # a real wake arrives during Verify
    synth_seconds=0.3,
    exchange_s=0.0,  # simulate the "never mind" pipeline running this long after the wake
):
    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    (tmp_path / "configs" / "nodes").mkdir(parents=True)
    monkeypatch.setattr(calibration, "QUIET_SECONDS", 0.5)
    monkeypatch.setattr(srv_mod, "_CALIB_SAY_MARGIN", 0.05)
    monkeypatch.setattr(srv_mod, "_CALIB_PEAK_REFRACTORY_S", 0.05)
    monkeypatch.setattr(srv_mod, "_CALIB_WAKE_WINDOW_S", 2.0)
    monkeypatch.setattr(srv_mod, "_CALIB_WAKE_EXTEND_S", 1.0)
    monkeypatch.setattr(srv_mod, "_CALIB_PROBE_LEAD_S", 0.02)
    monkeypatch.setattr(srv_mod, "_CALIB_PROBE_TAIL_S", 0.02)
    monkeypatch.setattr(srv_mod, "_CALIB_PROBE_MIN_S", 0.05)
    monkeypatch.setattr(srv_mod, "_CALIB_VERIFY_S", 0.6)
    monkeypatch.setattr(srv_mod, "_CALIB_RECONNECT_S", 5.0)
    monkeypatch.setattr(srv_mod, "_CALIB_CLOSE_MARGIN_S", 0.05)

    server = TranscribingServer({"host": "127.0.0.1", "port": 0})
    ws = _RecWS()
    server._nodes["n1"] = NodeSession(ws=ws, node_id="n1", room_id="den")

    said: list[str] = []
    said_at: dict[str, float] = {}
    events: list[dict] = []
    marks: dict = {}
    server.add_calib_listener(lambda node, e: events.append(e))

    async def fake_synth(text, vp):
        said.append(text)
        said_at[text] = time.monotonic()
        return b"\x00" * int(synth_seconds * 24000) * 2

    async def fake_stream(node_id, pcm):
        pass

    tuning = {"on": False}

    async def fake_tune_start(node_id, seconds=20.0):
        tuning["on"] = True
        return True

    async def fake_tune_stop(node_id):
        tuning["on"] = False
        return True

    async def fake_restart(node_id):
        # Simulate the node's re-exec: gone, then back on the same stub.
        async def cycle():
            sess_obj = server._nodes.pop(node_id, None)
            await asyncio.sleep(0.3)
            if sess_obj is not None:
                server._nodes[node_id] = sess_obj

        asyncio.get_running_loop().create_task(cycle())
        ws.sent.append('{"type": "restart"}')
        return True

    monkeypatch.setattr(server, "_synthesize", fake_synth)
    monkeypatch.setattr(server, "_stream_pcm", fake_stream)
    monkeypatch.setattr(server, "start_node_tuning", fake_tune_start)
    monkeypatch.setattr(server, "stop_node_tuning", fake_tune_stop)
    monkeypatch.setattr(server, "restart_node", fake_restart)
    if mode == "silent":
        monkeypatch.setattr(server, "_calib_beep", lambda: b"\x00" * int(0.3 * 24000) * 2)

    async def pump():
        i = 0
        while "n1" in server._calib_sessions:
            sess = server._calib_sessions.get("n1")
            if sess is None:
                break
            if tuning["on"]:
                ph = sess.get("phase")
                if ph == "wake":
                    server._notify_tune(
                        "n1",
                        {
                            "rms": speech_rms if i % 10 < 3 else quiet_rms,
                            "wake": 0.7 if wake_peaks and i % 10 == 0 else 0.02,
                            "vad": 0.9 if wake_peaks and i % 10 < 2 else 0.02,
                        },
                    )
                elif ph == "echo":
                    server._notify_tune(
                        "n1",
                        {"rms": echo_rms if echo_rms is not None else quiet_rms,
                         "wake": 0.02, "vad": 0.02},
                    )
                else:  # window-open handshake + the quiet phase
                    server._notify_tune("n1", {"rms": quiet_rms, "wake": 0.01, "vad": 0.01})
            ev = sess.get("verify")
            if verify and ev is not None and not ev.is_set():
                server._calib_saw_wake("n1")
                if exchange_s and "exchange" not in marks:
                    # The wake opened a real session; the user's "never mind"
                    # keeps the pipeline busy for a while — like real life.
                    async def _exchange():
                        server._stt_tasks["n1"] = asyncio.current_task()
                        await asyncio.sleep(exchange_s)
                        server._stt_tasks.pop("n1", None)
                        marks["exchange_done"] = time.monotonic()

                    marks["exchange"] = asyncio.create_task(_exchange())
            i += 1
            await asyncio.sleep(0.015)

    pump_task = asyncio.create_task(pump())
    err = await server.start_calibration("n1", "den", mode=mode)
    assert err is None, err
    task = server._calib_sessions["n1"]["task"]
    await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=30)
    pump_task.cancel()
    await asyncio.gather(pump_task, return_exceptions=True)
    import types as _t

    return _t.SimpleNamespace(
        server=server,
        ws=ws,
        said=said,
        said_at=said_at,
        events=events,
        marks=marks,
        ov_path=tmp_path / "configs" / "nodes" / "n1.yaml",
    )


async def test_full_flow_applies_and_verifies(tmp_path, monkeypatch):
    r = await _run_flow(tmp_path, monkeypatch)
    written = yaml.safe_load(r.ov_path.read_text())
    # Voice-anchored: 0.4 × speech_p25 (800) = 320 — NOT the quiet floor (~38).
    assert written["silence_rms_threshold"] == 320
    assert 0.2 <= written["wakeword_threshold"] <= 0.6
    assert 0.05 <= written["wakeword_vad_threshold"] <= 0.6
    assert "hardware_aec" not in written  # probe saw quiet residual = default true kept
    # Prompts spoken in order: intro → wake ask → summary → restart → verify → done.
    assert "stay quiet" in r.said[0].lower()
    assert any("hey kenzy" in s.lower() for s in r.said)
    assert any("tuned my hearing" in s.lower() for s in r.said)
    assert any("all set" in s.lower() for s in r.said)
    types = [json.loads(m).get("type") for m in r.ws.sent if isinstance(m, str)]
    assert "restart" in types and "config" in types
    stages = [e.get("stage") for e in r.events]
    assert stages[0] == "start" and "applied" in stages and "verify_result" in stages
    assert r.events[-1]["stage"] == "done" and r.events[-1]["ok"] is True
    vr = next(e for e in r.events if e.get("stage") == "verify_result")
    assert vr["ok"] is True and vr["nudges"] == 0
    assert "n1" not in r.server._calib_sessions


async def test_aec_probe_flips_flag_and_announces(tmp_path, monkeypatch):
    # Loud mic during the node's own playback ⇒ no hardware AEC ⇒ flag flips
    # (default is true) and the consequence is spoken, all BEFORE the wake phase.
    r = await _run_flow(tmp_path, monkeypatch, echo_rms=700.0)
    written = yaml.safe_load(r.ov_path.read_text())
    assert written["hardware_aec"] is False
    assert any("while i'm talking" in s.lower() for s in r.said)
    aec_events = [e for e in r.events if e.get("stage") == "aec"]
    assert aec_events == [{"mode": "spoken", "stage": "aec", "aec": False, "changed": True}]
    stages = [e.get("stage") for e in r.events]
    assert stages.index("aec") < stages.index("wake")  # applied before the wake phase


async def test_silent_mode_never_synthesizes(tmp_path, monkeypatch):
    # Dashboard mode: prompts are events (browser renders them); the beep is the
    # probe. No TTS anywhere — works on a fully-local / TTS-down install.
    r = await _run_flow(tmp_path, monkeypatch, mode="silent", echo_rms=700.0)
    assert r.said == []  # _synthesize never called
    written = yaml.safe_load(r.ov_path.read_text())
    assert written["hardware_aec"] is False
    assert written["silence_rms_threshold"] == 320
    prompts = [e["text"] for e in r.events if e.get("stage") == "prompt"]
    assert any("stay quiet" in p.lower() for p in prompts)  # browser gets the words
    assert r.events[-1]["stage"] == "done" and r.events[-1]["ok"] is True


async def test_verify_nudges_then_gives_up(tmp_path, monkeypatch):
    # No wake ever arrives in Verify: bounded nudges lower the threshold, then
    # the flow is honest about it (done ok, verify failed).
    r = await _run_flow(tmp_path, monkeypatch, verify=False)
    vr = next(e for e in r.events if e.get("stage") == "verify_result")
    assert vr["ok"] is False and vr["nudges"] == srv_mod._CALIB_MAX_NUDGES
    written = yaml.safe_load(r.ov_path.read_text())
    # Each nudge lowered the live wake threshold by 0.07.
    applied = next(e for e in r.events if e.get("stage") == "applied")
    assert written["wakeword_threshold"] < applied["patch"]["wakeword_threshold"]
    assert any("couldn't hear the wake word" in s.lower() for s in r.said)


async def test_poor_separation_withholds_silence(tmp_path, monkeypatch):
    # Speech only just clears the gate (inside the poor band): the silence
    # threshold is withheld but the score-based wake/VAD still calibrate.
    r = await _run_flow(tmp_path, monkeypatch, quiet_rms=100.0, speech_rms=250.0)
    written = yaml.safe_load(r.ov_path.read_text())
    assert "silence_rms_threshold" not in written
    assert "wakeword_threshold" in written
    assert any("fair warning" in s.lower() for s in r.said)


async def test_no_signal_changes_nothing(tmp_path, monkeypatch):
    # Nobody spoke: after one extension the flow gives up — no override, no
    # restart, honest reply.
    r = await _run_flow(tmp_path, monkeypatch, speech_rms=15.0, wake_peaks=False)
    assert not r.ov_path.exists()
    assert any("unchanged" in s.lower() for s in r.said)
    assert any("didn't quite hear" in s.lower() for s in r.said)
    types = [json.loads(m).get("type") for m in r.ws.sent if isinstance(m, str)]
    assert "restart" not in types
    assert r.events[-1]["stage"] == "done" and r.events[-1]["ok"] is False


async def test_closing_line_waits_for_never_mind_exchange(tmp_path, monkeypatch):
    """The verify wake opens a REAL session and the user's "never mind" runs the
    pipeline — Kenzy answers it. The calibration closing line must wait for that
    whole exchange, not race it for the speaker (field bug, 2026-07-11)."""
    r = await _run_flow(tmp_path, monkeypatch, exchange_s=0.6)
    close = next(text for text in r.said if "all set" in text.lower())
    assert "exchange_done" in r.marks, "simulated exchange never finished"
    assert r.said_at[close] > r.marks["exchange_done"], (
        "closing line spoke while the never-mind exchange was still running"
    )
