"""
Kenzy STT service.

Accepts POST /transcribe with base64-encoded raw PCM audio (16 kHz / int16 /
mono) and returns transcribed text.

Supported providers (set via 'provider' in stt.yaml):
  whisper — local faster-whisper (default); nothing leaves the box
  openai  — OpenAI transcription API (gpt-4o-mini-transcribe et al.); requires
            OPENAI_API_KEY. No local model, so it runs on underpowered hosts —
            at the cost of sending each captured utterance to OpenAI.

The asyncio.Semaphore serialises concurrent whisper transcriptions because
CTranslate2 / faster-whisper is not documented as thread-safe for simultaneous
calls on the same model object.
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import wave
from typing import Any

import numpy as np
from fastapi import FastAPI
from pydantic import BaseModel

from kenzy import kenzy_version
from kenzy.fastapi_auth import (
    install_logs_endpoint,
    install_restart_endpoint,
    install_service_auth,
    install_upgrade_endpoint,
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

_provider: str = "whisper"

# Whisper (local)
_whisper: Any = None
_language: str | None = None
_sem: asyncio.Semaphore | None = None
_model_size: str = ""  # surfaced on /health for the dashboard

# OpenAI (cloud)
_openai_client: Any = None
_openai_model: str = "gpt-4o-mini-transcribe"
_openai_fallback: bool = True  # silent local-whisper retry on cloud failure
_wcfg: dict[str, Any] = {}  # whisper settings (also used by the fallback load)

_SAMPLE_RATE = 16000


@app.get("/health")
async def health() -> dict[str, object]:
    return {
        "status": "ok",
        "version": kenzy_version(),
        "provider": _provider,
        "model": _openai_model if _provider == "openai" else _model_size,
        "language": _language or "auto",
    }


@app.post("/transcribe", response_model=TranscribeResponse)
async def transcribe(req: TranscribeRequest) -> TranscribeResponse:
    loop = asyncio.get_running_loop()
    pcm = base64.b64decode(req.audio_b64)
    if _provider == "openai":
        try:
            text = await loop.run_in_executor(None, _run_openai, pcm)
        except Exception as exc:
            if not _openai_fallback:
                raise
            # Silent local fallback: load faster-whisper on first need. If this
            # fails too (package missing, model never cached and no internet),
            # the exception propagates and the user just gets the error cue.
            log.warning("OpenAI STT failed (%s) — falling back to local whisper", exc)
            text = await _whisper_fallback(pcm, loop)
    else:
        assert _sem is not None
        async with _sem:
            text = await loop.run_in_executor(None, _run_whisper, pcm)
    log.info("[%s] %s", req.room_id or "?", text or "(no speech detected)")
    return TranscribeResponse(text=text)


async def _whisper_fallback(pcm: bytes, loop: asyncio.AbstractEventLoop) -> str:
    """Transcribe locally, lazily loading the whisper model on first use."""
    global _whisper
    assert _sem is not None
    async with _sem:  # serialize the load AND the (non-thread-safe) transcription
        if _whisper is None:
            _whisper = await loop.run_in_executor(None, _load_whisper)
        return await loop.run_in_executor(None, _run_whisper, pcm)


def _load_whisper() -> Any:
    from faster_whisper import WhisperModel  # type: ignore[import-untyped]

    model_size = str(_wcfg.get("model", "base"))
    device = str(_wcfg.get("device", "cpu"))
    compute_type = str(_wcfg.get("compute_type", "int8"))
    log.warning("Loading whisper fallback model '%s' on %s (%s)…", model_size, device, compute_type)
    return WhisperModel(model_size, device=device, compute_type=compute_type)


# ---------------------------------------------------------------------------
# Whisper (local) path
# ---------------------------------------------------------------------------


def _run_whisper(pcm: bytes) -> str:
    audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    segments, _ = _whisper.transcribe(audio, language=_language, beam_size=5)
    return " ".join(s.text.strip() for s in segments).strip()


# ---------------------------------------------------------------------------
# OpenAI (cloud) path
# ---------------------------------------------------------------------------


def _pcm_to_wav(pcm: bytes, sample_rate: int = _SAMPLE_RATE) -> bytes:
    """Wrap raw int16 mono PCM in a WAV container (the upload needs a real format)."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)
    return buf.getvalue()


def _run_openai(pcm: bytes) -> str:
    kwargs: dict[str, Any] = {
        "model": _openai_model,
        "file": ("audio.wav", _pcm_to_wav(pcm)),
    }
    if _language:
        kwargs["language"] = _language
    result = _openai_client.audio.transcriptions.create(**kwargs)
    return str(result.text or "").strip()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> None:
    global _provider, _whisper, _language, _sem, _model_size
    global _openai_client, _openai_model, _openai_fallback, _wcfg

    import uvicorn  # type: ignore[import-untyped]
    from dotenv import load_dotenv  # type: ignore[import-untyped]

    load_dotenv()  # OPENAI_API_KEY for the openai provider; harmless otherwise

    from kenzy.logutil import configure_logging, level_value
    from kenzy.serviceboot import effective_bind, load_service_config, start_registration

    configure_logging(logging.INFO)  # provisional, so the config pull's retries are visible

    # Central config: pull from the server (blocking until it answers); an explicit
    # config path loads locally instead (dev/offline escape hatch).
    cfg: dict[str, Any] = load_service_config("stt")
    start_registration("stt", cfg)  # auto-announce to the server (dashboard + pipeline)

    configure_logging(level_value(cfg.get("log_level"), logging.INFO))
    quiet_health_access_log()
    install_service_auth(app)
    install_logs_endpoint(
        app, capture_level=level_value(cfg.get("log_capture_level"), logging.DEBUG)
    )
    install_restart_endpoint(app)
    install_upgrade_endpoint(app, "stt")

    _provider = str(cfg.get("provider", "whisper")).lower()
    _wcfg = dict(cfg.get("whisper", {}) or {})
    _sem = asyncio.Semaphore(1)

    if _provider == "openai":
        try:
            from openai import OpenAI  # type: ignore[import-untyped]
        except ImportError as exc:
            raise RuntimeError("openai is not installed — run: pip install openai") from exc

        ocfg: dict[str, Any] = cfg.get("openai", {})
        _openai_model = str(ocfg.get("model", "gpt-4o-mini-transcribe"))
        _openai_fallback = bool(ocfg.get("fallback", True))
        _language = ocfg.get("language") or None
        timeout = float(ocfg.get("timeout", 30.0))
        _openai_client = OpenAI(timeout=timeout)
        log.info(
            "STT provider: openai  model=%s language=%s (no local model loaded; "
            "local-whisper fallback %s)",
            _openai_model,
            _language or "auto",
            "on" if _openai_fallback else "off",
        )

    else:
        if _provider != "whisper":
            log.warning("Unknown STT provider %r; using whisper", _provider)
            _provider = "whisper"

        # Lazy import so the openai provider never needs (or loads) the local stack.
        try:
            from faster_whisper import WhisperModel  # type: ignore[import-untyped]
        except ImportError as exc:
            raise RuntimeError(
                "faster-whisper is not installed – run: pip install faster-whisper"
            ) from exc

        model_size = _model_size = str(_wcfg.get("model", "base"))
        device = str(_wcfg.get("device", "cpu"))
        compute_type = str(_wcfg.get("compute_type", "int8"))
        _language = _wcfg.get("language") or None

        log.info("Loading Whisper model '%s' on %s (%s)…", model_size, device, compute_type)
        _whisper = WhisperModel(model_size, device=device, compute_type=compute_type)
        log.info("Whisper model ready.")

    uvicorn.run(
        app,
        host=effective_bind(cfg),
        port=int(cfg.get("port", 8767)),
        log_level=str(cfg.get("log_level", "info")).lower(),
    )


if __name__ == "__main__":
    main()
