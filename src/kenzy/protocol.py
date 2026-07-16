"""
Shared WebSocket protocol constants and message helpers.

Control messages are JSON text frames; audio data is binary frames.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

# ---------------------------------------------------------------------------
# Message type constants
# ---------------------------------------------------------------------------

MSG_HELLO = "hello"
MSG_CONFIG = "config"
MSG_AUDIO_START = "audio_start"
MSG_AUDIO_END = "audio_end"
MSG_WAKEWORD = "wakeword"
MSG_TRIGGER = "trigger"
MSG_STOP = "stop"
MSG_ACK = "ack"
MSG_TTS_START = "tts_start"
MSG_TTS_END = "tts_end"
MSG_RESTART = "restart"
MSG_UPGRADE = "upgrade"
MSG_SET_ROOM = "set_room"
MSG_REQUEST_LOGS = "request_logs"
MSG_LOGS = "logs"
MSG_STATUS = "status"  # node→server: report node health (e.g. audio init failed)
MSG_METRICS = "metrics"  # node→server: periodic system metrics (cpu/ram/disk/temp)
MSG_TUNE_START = "tune_start"  # server→node: begin a bounded calibration window
MSG_TUNE_STOP = "tune_stop"  # server→node: end calibration early
MSG_TUNE_SAMPLE = "tune_sample"  # node→server: one calibration sample (rms/wake/vad)
MSG_EXPECT_UTTERANCE = "expect_utterance"  # server→node: capture one utterance after the next TTS
MSG_FOLLOWUP_TIMEOUT = "followup_timeout"  # node→server: held-floor reply window expired silently
MSG_CALL_RINGING = "call_ringing"  # server→caller: play the ringback loop while the callee is rung
MSG_END_DIALOG = "end_dialog"  # server→node: a multi-turn dialog ended — play the end cue
# Intercom (live two-way call between two rooms; gated by the receiver's consent).
MSG_CALL_REQUEST = "call_request"  # server→node: ring the receiver (no audio yet)
MSG_CALL_CANCEL = "call_cancel"  # server→node: caller hung up before accept
MSG_INTERCOM_START = "intercom_start"  # server→node: consent accepted, begin the call
MSG_INTERCOM_END = "intercom_end"  # server↔node: end the call

# ---------------------------------------------------------------------------
# Audio format (shared by node and server)
# ---------------------------------------------------------------------------

SAMPLE_RATE: int = 16_000  # Hz
CHANNELS: int = 1  # mono
SAMPLE_WIDTH: int = 2  # bytes per sample (int16)
FRAME_MS: int = 80  # milliseconds per frame
FRAME_SAMPLES: int = SAMPLE_RATE * FRAME_MS // 1000  # 1 280 samples

# ---------------------------------------------------------------------------
# Message constructors
# ---------------------------------------------------------------------------


def hello(
    room_id: str,
    node_id: str | None = None,
    version: str = "1.0",
    capabilities: dict[str, Any] | None = None,
    token: str | None = None,
    kenzy_version: str | None = None,
    auth: dict[str, Any] | None = None,
) -> str:
    """Node→server registration.

    ``room_id`` is the human room *name* (sent to the backends as context).
    ``node_id`` is the node's stable primary identifier; when omitted the server
    falls back to using ``room_id`` as the key (legacy nodes). ``version`` is the
    wire-protocol version; ``kenzy_version`` is the installed package version (for
    the dashboard's per-host version view).
    """
    payload: dict[str, Any] = {"type": MSG_HELLO, "room_id": room_id, "version": version}
    if node_id is not None:
        payload["node_id"] = node_id
    if capabilities is not None:
        payload["capabilities"] = capabilities
    if token is not None:
        payload["token"] = token
    if auth is not None:
        payload["auth"] = auth  # 3.12+ token-proof join (raw token stays off the wire)
    if kenzy_version is not None:
        payload["kenzy_version"] = kenzy_version
    return json.dumps(payload)


def config(node_config: dict[str, Any]) -> str:
    """Server→node frame carrying the node's effective configuration."""
    return json.dumps({"type": MSG_CONFIG, "config": node_config})


def audio_start(session_id: str | None = None, room_id: str | None = None) -> tuple[str, str]:
    """Return (json_str, session_id)."""
    sid = session_id or str(uuid.uuid4())
    payload: dict[str, Any] = {"type": MSG_AUDIO_START, "session_id": sid}
    if room_id is not None:
        payload["room_id"] = room_id
    return json.dumps(payload), sid


def audio_end(session_id: str, reason: str = "silence") -> str:
    return json.dumps({"type": MSG_AUDIO_END, "session_id": session_id, "reason": reason})


def wakeword(session_id: str | None, model: str, score: float) -> str:
    return json.dumps(
        {
            "type": MSG_WAKEWORD,
            "session_id": session_id,
            "model": model,
            "score": round(float(score), 4),
        }
    )


def trigger(session_id: str | None = None) -> str:
    return json.dumps({"type": MSG_TRIGGER, "session_id": session_id or str(uuid.uuid4())})


def stop() -> str:
    return json.dumps({"type": MSG_STOP})


def restart() -> str:
    return json.dumps({"type": MSG_RESTART})


def upgrade(version: str | None = None) -> str:
    """Server→node: pip-upgrade ``kenzy[node]`` (to ``version`` or latest) and re-exec."""
    payload: dict[str, Any] = {"type": MSG_UPGRADE}
    if version is not None:
        payload["version"] = version
    return json.dumps(payload)


def set_room(room_id: str) -> str:
    """Server→node: set the node's room name (the node persists + applies it)."""
    return json.dumps({"type": MSG_SET_ROOM, "room_id": room_id})


def request_logs(request_id: str, level: str = "", limit: int = 200) -> str:
    return json.dumps(
        {"type": MSG_REQUEST_LOGS, "request_id": request_id, "level": level, "limit": limit}
    )


def node_logs(request_id: str, entries: list[dict[str, Any]]) -> str:
    return json.dumps({"type": MSG_LOGS, "request_id": request_id, "logs": entries})


def status(
    audio_ok: bool,
    audio_error: str | None = None,
    devices: list[dict[str, Any]] | None = None,
) -> str:
    """Node→server health update: sent when audio init fails (so the node can be
    fixed/restarted remotely while staying connected) and when the audio-device
    probe finishes (to deliver the device list for the dashboard picker)."""
    payload: dict[str, Any] = {"type": MSG_STATUS, "audio_ok": audio_ok, "audio_error": audio_error}
    if devices is not None:
        payload["devices"] = devices
    return json.dumps(payload)


def metrics(
    cpu: float | None = None,
    ram: float | None = None,
    disk: float | None = None,
    temp: float | None = None,
) -> str:
    """Node→server periodic system metrics (percentages / °C; None = unavailable
    on this platform). Shown on the dashboard's fleet card."""
    return json.dumps({"type": MSG_METRICS, "cpu": cpu, "ram": ram, "disk": disk, "temp": temp})


def tune_start(seconds: float = 20.0) -> str:
    return json.dumps({"type": MSG_TUNE_START, "seconds": seconds})


def tune_stop() -> str:
    return json.dumps({"type": MSG_TUNE_STOP})


def expect_utterance(cue: bool = True) -> str:
    """Arm one-shot capture after the next TTS prompt finishes playing.

    ``cue`` controls whether the node plays its ready chime when the capture
    window opens: True for record-after-the-tone flows (voice enrollment),
    False for conversational follow-ups (her question IS the cue — a beep on
    top reads as "wait, was I supposed to wait?"). Absent = True, so an older
    server keeps today's chime behavior on a newer node.
    """
    return json.dumps({"type": MSG_EXPECT_UTTERANCE, "cue": bool(cue)})


def followup_timeout() -> str:
    """Node→server: a held-floor reply window expired with no speech — the
    dialog is over (the server clears its turn counter). The node plays its own
    end-of-dialog cue locally ("I stopped waiting")."""
    return json.dumps({"type": MSG_FOLLOWUP_TIMEOUT})


def call_ringing() -> str:
    """Server→caller: start the intercom ringback loop while the target room is
    rung and asked to accept. The caller node loops its ``sound_ringback`` until
    the call connects (intercom_start), is declined/times out (a spoken reply),
    or is cancelled — so the caller isn't left in silence during the wait."""
    return json.dumps({"type": MSG_CALL_RINGING})


def end_dialog() -> str:
    """Tell the node a multi-turn dialog just ended — play the end-of-dialog cue
    (after any in-progress TTS finishes)."""
    return json.dumps({"type": MSG_END_DIALOG})


def tune_sample(
    rms: float = 0.0, wake: float = 0.0, vad: float = 0.0, seq: int = 0, stopped: bool = False
) -> str:
    """One calibration measurement frame (or a final ``stopped`` marker)."""
    return json.dumps(
        {
            "type": MSG_TUNE_SAMPLE,
            "rms": rms,
            "wake": wake,
            "vad": vad,
            "seq": seq,
            "stopped": stopped,
        }
    )


def ack(session_id: str) -> str:
    return json.dumps({"type": MSG_ACK, "session_id": session_id})


def tts_start(
    session_id: str, sample_rate: int = 22050, channels: int = 1, alert: bool = False
) -> str:
    """``alert=True`` marks alert audio (doorbell chimes): a muted node still
    plays it at the audible floor, like the wake-word ready chime. Older nodes
    ignore the extra key (the chime simply honors mute there)."""
    payload: dict[str, Any] = {
        "type": MSG_TTS_START,
        "session_id": session_id,
        "sample_rate": sample_rate,
        "channels": channels,
    }
    if alert:
        payload["alert"] = True
    return json.dumps(payload)


def tts_end(session_id: str) -> str:
    return json.dumps({"type": MSG_TTS_END, "session_id": session_id})


def call_request(from_room: str) -> str:
    """Server→node: ring the receiver for an intercom call. No audio is bridged yet."""
    return json.dumps({"type": MSG_CALL_REQUEST, "from_room": from_room})


def call_cancel() -> str:
    """Server→node: the caller cancelled before the receiver accepted."""
    return json.dumps({"type": MSG_CALL_CANCEL})


def intercom_start(peer_room: str, sample_rate: int = SAMPLE_RATE, channels: int = CHANNELS) -> str:
    """Server→node: consent accepted — begin live two-way audio with the peer room."""
    return json.dumps(
        {
            "type": MSG_INTERCOM_START,
            "peer_room": peer_room,
            "sample_rate": sample_rate,
            "channels": channels,
        }
    )


def intercom_end(reason: str = "ended") -> str:
    """End an intercom call (server→node to tear down, or node→server on wake word)."""
    return json.dumps({"type": MSG_INTERCOM_END, "reason": reason})


def parse(raw: str | bytes) -> dict[str, Any]:
    return json.loads(raw)  # type: ignore[no-any-return]
