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
MSG_SET_ROOM = "set_room"
MSG_REQUEST_LOGS = "request_logs"
MSG_LOGS = "logs"
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
FRAME_BYTES: int = FRAME_SAMPLES * SAMPLE_WIDTH  # 2 560 bytes

# ---------------------------------------------------------------------------
# Message constructors
# ---------------------------------------------------------------------------


def hello(
    room_id: str,
    node_id: str | None = None,
    version: str = "1.0",
    capabilities: dict[str, Any] | None = None,
    token: str | None = None,
) -> str:
    """Node→server registration.

    ``room_id`` is the human room *name* (sent to the backends as context).
    ``node_id`` is the node's stable primary identifier; when omitted the server
    falls back to using ``room_id`` as the key (legacy nodes).
    """
    payload: dict[str, Any] = {"type": MSG_HELLO, "room_id": room_id, "version": version}
    if node_id is not None:
        payload["node_id"] = node_id
    if capabilities is not None:
        payload["capabilities"] = capabilities
    if token is not None:
        payload["token"] = token
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


def set_room(room_id: str) -> str:
    """Server→node: set the node's room name (the node persists + applies it)."""
    return json.dumps({"type": MSG_SET_ROOM, "room_id": room_id})


def request_logs(request_id: str, level: str = "", limit: int = 200) -> str:
    return json.dumps(
        {"type": MSG_REQUEST_LOGS, "request_id": request_id, "level": level, "limit": limit}
    )


def node_logs(request_id: str, entries: list[dict[str, Any]]) -> str:
    return json.dumps({"type": MSG_LOGS, "request_id": request_id, "logs": entries})


def ack(session_id: str) -> str:
    return json.dumps({"type": MSG_ACK, "session_id": session_id})


def tts_start(session_id: str, sample_rate: int = 22050, channels: int = 1) -> str:
    return json.dumps(
        {
            "type": MSG_TTS_START,
            "session_id": session_id,
            "sample_rate": sample_rate,
            "channels": channels,
        }
    )


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
