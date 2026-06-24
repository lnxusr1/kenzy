"""
Kenzy STT service.

Accepts POST /transcribe with base64-encoded raw PCM audio (16 kHz / int16 /
mono) and returns transcribed text using faster-whisper.

The asyncio.Semaphore serialises concurrent transcription requests because
CTranslate2 / faster-whisper is not documented as thread-safe for simultaneous
calls on the same model object.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from typing import Any

import numpy as np
from fastapi import FastAPI
from pydantic import BaseModel

from kenzy import kenzy_version
from kenzy.fastapi_auth import (
    install_logs_endpoint,
    install_restart_endpoint,
    install_service_auth,
)
from kenzy.logutil import quiet_health_access_log

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class TranscribeRequest(BaseModel):
    audio_b64: str  # base64-encoded int16 PCM at 16 kHz mono
    room_id: str | None = None
    session_id: str | None = None


class TranscribeResponse(BaseModel):
    text: str


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="Kenzy STT Service", version="0.1.0")

_whisper: Any = None
_language: str | None = None
_sem: asyncio.Semaphore | None = None
_model_size: str = ""  # surfaced on /health for the dashboard


@app.get("/health")
async def health() -> dict[str, object]:
    return {
        "status": "ok",
        "version": kenzy_version(),
        "model": _model_size,
        "language": _language or "auto",
    }


@app.post("/transcribe", response_model=TranscribeResponse)
async def transcribe(req: TranscribeRequest) -> TranscribeResponse:
    loop = asyncio.get_running_loop()
    pcm = base64.b64decode(req.audio_b64)
    assert _sem is not None
    async with _sem:
        text = await loop.run_in_executor(None, _run_whisper, pcm)
    log.info("[%s] %s", req.room_id or "?", text or "(no speech detected)")
    return TranscribeResponse(text=text)


# ---------------------------------------------------------------------------
# Whisper
# ---------------------------------------------------------------------------


def _run_whisper(pcm: bytes) -> str:
    audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    segments, _ = _whisper.transcribe(audio, language=_language, beam_size=5)
    return " ".join(s.text.strip() for s in segments).strip()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> None:
    global _whisper, _language, _sem, _model_size

    import uvicorn  # type: ignore[import-untyped]

    from kenzy.logutil import configure_logging, level_value
    from kenzy.serviceboot import load_service_config

    configure_logging(logging.INFO)  # provisional, so the config pull's retries are visible

    # Central config: pull from the server (blocking until it answers); an explicit
    # config path loads locally instead (dev/offline escape hatch).
    cfg: dict[str, Any] = load_service_config("stt")

    configure_logging(level_value(cfg.get("log_level"), logging.INFO))
    quiet_health_access_log()
    install_service_auth(app)
    install_logs_endpoint(
        app, capture_level=level_value(cfg.get("log_capture_level"), logging.DEBUG)
    )
    install_restart_endpoint(app)

    try:
        from faster_whisper import WhisperModel  # type: ignore[import-untyped]
    except ImportError as exc:
        raise RuntimeError(
            "faster-whisper is not installed – run: pip install faster-whisper"
        ) from exc

    wcfg: dict[str, Any] = cfg.get("whisper", {})
    model_size = _model_size = str(wcfg.get("model", "base"))
    device = str(wcfg.get("device", "cpu"))
    compute_type = str(wcfg.get("compute_type", "int8"))
    _language = wcfg.get("language") or None

    log.info("Loading Whisper model '%s' on %s (%s)…", model_size, device, compute_type)
    _whisper = WhisperModel(model_size, device=device, compute_type=compute_type)
    log.info("Whisper model ready.")

    _sem = asyncio.Semaphore(1)

    uvicorn.run(
        app,
        host=cfg.get("host", "127.0.0.1"),
        port=int(cfg.get("port", 8767)),
        log_level=str(cfg.get("log_level", "info")).lower(),
    )


if __name__ == "__main__":
    main()
