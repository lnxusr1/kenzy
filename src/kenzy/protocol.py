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
MSG_AUDIO_START = "audio_start"
MSG_AUDIO_END = "audio_end"
MSG_WAKEWORD = "wakeword"
MSG_TRIGGER = "trigger"
MSG_STOP = "stop"
MSG_ACK = "ack"
MSG_TTS_START = "tts_start"
MSG_TTS_END = "tts_end"

# ---------------------------------------------------------------------------
# Audio format (shared by node and server)
# ---------------------------------------------------------------------------

SAMPLE_RATE: int = 16_000       # Hz
CHANNELS: int = 1               # mono
SAMPLE_WIDTH: int = 2           # bytes per sample (int16)
FRAME_MS: int = 80              # milliseconds per frame
FRAME_SAMPLES: int = SAMPLE_RATE * FRAME_MS // 1000   # 1 280 samples
FRAME_BYTES: int = FRAME_SAMPLES * SAMPLE_WIDTH        # 2 560 bytes

# ---------------------------------------------------------------------------
# Message constructors
# ---------------------------------------------------------------------------


def hello(room_id: str, version: str = "1.0") -> str:
    return json.dumps({"type": MSG_HELLO, "room_id": room_id, "version": version})


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
        {"type": MSG_WAKEWORD, "session_id": session_id, "model": model, "score": round(float(score), 4)}
    )


def trigger(session_id: str | None = None) -> str:
    return json.dumps({"type": MSG_TRIGGER, "session_id": session_id or str(uuid.uuid4())})


def stop() -> str:
    return json.dumps({"type": MSG_STOP})


def ack(session_id: str) -> str:
    return json.dumps({"type": MSG_ACK, "session_id": session_id})


def tts_start(session_id: str, sample_rate: int = 22050, channels: int = 1) -> str:
    return json.dumps(
        {"type": MSG_TTS_START, "session_id": session_id,
         "sample_rate": sample_rate, "channels": channels}
    )


def tts_end(session_id: str) -> str:
    return json.dumps({"type": MSG_TTS_END, "session_id": session_id})


def parse(raw: str | bytes) -> dict[str, Any]:
    return json.loads(raw)  # type: ignore[no-any-return]
