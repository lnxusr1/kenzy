"""
Kenzy TTS service.

Accepts POST /speak with text and an optional voice instruction prompt,
synthesises speech, and returns raw int16 PCM audio at 24 kHz mono as
application/octet-stream.

Supported providers (set via 'provider' in tts.yaml):
  openai  — OpenAI TTS API (default); requires OPENAI_API_KEY
  kokoro  — Local Kokoro TTS (PyTorch); requires pip install -e ".[kokoro]"
            and the espeak-ng system package

The sample rate and channel count are echoed in response headers
(X-Sample-Rate, X-Channels) so callers can configure playback without
hardcoding the format.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from fastapi import FastAPI
from fastapi.responses import Response
from pydantic import BaseModel

from kenzy import version_info
from kenzy.fastapi_auth import (
    install_logs_endpoint,
    install_restart_endpoint,
    install_service_auth,
    install_unit_endpoint,
    install_upgrade_endpoint,
)
from kenzy.logutil import quiet_health_access_log

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Request model
# ---------------------------------------------------------------------------


class SpeakRequest(BaseModel):
    text: str
    voice_prompt: str | None = None
    room_id: str | None = None
    # The caller (server) flags lockbox replies: the spoken text carries a
    # secret value, so this service's own log line must withhold it.
    sensitive: bool = False


# ---------------------------------------------------------------------------
# App + module-level state
# ---------------------------------------------------------------------------

app = FastAPI(title="Kenzy TTS Service", version="0.1.0")

_provider: str = "openai"

# OpenAI
_client: Any = None
_model: str = "gpt-4o-mini-tts"
_voice: str = "sage"
_speed: float = 1.0
_openai_fallback: bool = True  # silent local-Kokoro retry on cloud failure (if installed)

# Kokoro
_kokoro_pipeline: Any = None
_kokoro_voice: str = "af_heart"
_kokoro_speed: float = 1.0


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.get("/health")
async def health() -> dict[str, object]:
    # "local": speech never leaves the box (the lockbox's spoken-recall gate).
    if _provider == "kokoro":
        return {
            "status": "ok",
            **version_info(),
            "provider": "kokoro",
            "local": True,
            "voice": _kokoro_voice,
        }
    return {
        "status": "ok",
        **version_info(),
        "provider": "openai",
        "local": False,
        "model": _model,
        "voice": _voice,
    }


@app.post("/speak")
async def speak(req: SpeakRequest) -> Response:
    loop = asyncio.get_running_loop()
    log.info(
        "[%s] speak: %s",
        req.room_id or "?",
        "[lockbox reply — content withheld]" if req.sensitive else req.text[:80],
    )
    pcm = await loop.run_in_executor(None, _synthesise, req.text, req.voice_prompt or "")
    return Response(
        content=pcm,
        media_type="application/octet-stream",
        headers={"X-Sample-Rate": "24000", "X-Channels": "1"},
    )


# ---------------------------------------------------------------------------
# Synthesis — OpenAI path
# ---------------------------------------------------------------------------

# OpenAI TTS accepts at most 4096 characters per request.
_MAX_CHUNK_CHARS = 4096


def _split_text(text: str) -> list[str]:
    """Split text at sentence boundaries into chunks under _MAX_CHUNK_CHARS."""
    if len(text) <= _MAX_CHUNK_CHARS:
        return [text]

    sentences = re.split(r"(?<=[.!?])\s+", text)
    chunks: list[str] = []
    current = ""

    for sentence in sentences:
        if not sentence:
            continue
        if len(sentence) > _MAX_CHUNK_CHARS:
            for word in sentence.split():
                if len(current) + len(word) + 1 > _MAX_CHUNK_CHARS:
                    if current:
                        chunks.append(current)
                    current = word
                else:
                    current = (current + " " + word).strip()
        elif current and len(current) + 1 + len(sentence) > _MAX_CHUNK_CHARS:
            chunks.append(current)
            current = sentence
        else:
            current = (current + " " + sentence).strip()

    if current:
        chunks.append(current)

    return chunks


def _synthesise_openai(text: str, voice_prompt: str) -> bytes:
    kwargs: dict[str, Any] = {
        "model": _model,
        "voice": _voice,
        "input": text,
        "response_format": "pcm",
        "speed": _speed,
    }
    if voice_prompt:
        kwargs["instructions"] = voice_prompt
    return _client.audio.speech.create(**kwargs).content  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# Synthesis — Kokoro path
# ---------------------------------------------------------------------------


def _resolve_device(device: str) -> str:
    """Resolve 'auto' to the best available PyTorch device.

    Thin wrapper over :func:`kenzy.torchdevice.resolve_device`, which the
    ``kenzy-setup`` pre-warm shares — it used to have its own handling and
    passed the literal string "auto" straight to Kokoro.
    """
    from kenzy.torchdevice import resolve_device

    return resolve_device(device)


def _synthesise_kokoro(text: str) -> bytes:
    import numpy as np  # type: ignore[import-untyped]

    segments: list[Any] = []
    for _, _, audio in _kokoro_pipeline(text, voice=_kokoro_voice, speed=_kokoro_speed):
        if audio is not None and len(audio) > 0:
            segments.append(audio)

    if not segments:
        return b""

    combined = np.concatenate(segments)
    pcm_bytes: bytes = (np.clip(combined, -1.0, 1.0) * 32767).astype(np.int16).tobytes()
    return pcm_bytes


# ---------------------------------------------------------------------------
# Synthesis — dispatcher
# ---------------------------------------------------------------------------


def _synthesise(text: str, voice_prompt: str) -> bytes:
    if _provider == "kokoro":
        if voice_prompt:
            log.debug("voice_prompt ignored — not supported by the Kokoro provider")
        return _synthesise_kokoro(text)

    # OpenAI path: split at sentence boundaries to respect the 4096-char limit.
    try:
        chunks = _split_text(text)
        if len(chunks) == 1:
            return _synthesise_openai(chunks[0], voice_prompt)
        log.debug("TTS: splitting %d chars into %d chunks", len(text), len(chunks))
        return b"".join(_synthesise_openai(chunk, voice_prompt) for chunk in chunks)
    except Exception as exc:
        if not _openai_fallback:
            raise
        # Silent local fallback: Kokoro, if the extra is installed (lazy first
        # load). If it isn't — or the load fails — the exception propagates and
        # the server plays the pre-recorded error cue instead.
        log.warning("OpenAI TTS failed (%s) — falling back to local Kokoro", exc)
        _ensure_fallback_kokoro()
        return _synthesise_kokoro(text)


def _ensure_fallback_kokoro() -> None:
    """Lazily bring up a Kokoro pipeline for cloud-failure fallback (defaults:
    ``af_heart`` voice). Raises when the ``kokoro`` extra isn't installed."""
    global _kokoro_pipeline
    if _kokoro_pipeline is not None:
        return
    from kokoro import KPipeline  # type: ignore[import-untyped]

    log.warning("Loading Kokoro fallback pipeline (first cloud-TTS failure)…")
    _kokoro_pipeline = KPipeline(lang_code=_kokoro_voice[0], device=_resolve_device("auto"))


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> None:
    global _provider
    global _client, _model, _voice, _speed, _openai_fallback
    global _kokoro_pipeline, _kokoro_voice, _kokoro_speed

    import uvicorn  # type: ignore[import-untyped]
    from dotenv import load_dotenv  # type: ignore[import-untyped]

    load_dotenv()

    from kenzy.logutil import configure_logging, level_value
    from kenzy.serviceboot import effective_bind, load_service_config, start_registration

    configure_logging(logging.INFO)  # provisional, so the config pull's retries are visible

    # Central config: pull from the server (blocking until it answers); an explicit
    # config path loads locally instead (dev/offline escape hatch).
    cfg: dict[str, Any] = load_service_config("tts")
    start_registration("tts", cfg)  # auto-announce to the server (dashboard + pipeline)

    configure_logging(level_value(cfg.get("log_level"), logging.INFO))
    quiet_health_access_log()
    install_service_auth(app)
    install_logs_endpoint(
        app, capture_level=level_value(cfg.get("log_capture_level"), logging.DEBUG)
    )
    install_restart_endpoint(app)
    install_upgrade_endpoint(app, "tts")
    install_unit_endpoint(app, "kenzy-tts.service")

    from kenzy.fastapi_auth import install_features_endpoint, install_fill_endpoint
    from kenzy.features import feature, probe_binary, probe_import
    from kenzy.tts import wyoming_server as _wy

    def _features() -> list[dict[str, Any]]:
        wcfg = cfg.get("wyoming") or {}
        kokoro_wanted = _provider == "kokoro" or bool(
            (cfg.get("openai") or {}).get("fallback", True)
        )
        espeak = probe_binary("espeak-ng")
        return [
            feature(
                "wyoming",
                configured=bool(wcfg.get("enabled", False)),
                available=probe_import("wyoming"),
                active=_wy.is_active(),
                note="Home Assistant voice pipelines speak with Kenzy's voice.",
            ),
            feature(
                "kokoro",
                configured=kokoro_wanted,
                available=probe_import("kokoro") and espeak,
                active=_kokoro_pipeline is not None,
                install="pip" if espeak else "apt",
                note=(
                    "The local voice (provider or cloud-failure fallback)."
                    if espeak
                    else "Needs a system package first: sudo apt-get install espeak-ng"
                ),
            ),
        ]

    install_features_endpoint(app, _features)
    install_fill_endpoint(app, "tts,kokoro")

    _provider = str(cfg.get("provider", "openai")).lower()

    if _provider == "kokoro":
        try:
            from kokoro import KPipeline  # type: ignore[import-untyped]
        except ImportError as exc:
            raise RuntimeError(
                "kokoro is not installed — run: pip install -e '.[kokoro]'\n"
                "Also ensure espeak-ng is installed: sudo apt-get install espeak-ng"
            ) from exc

        kcfg: dict[str, Any] = cfg.get("kokoro", {})
        _kokoro_voice = str(kcfg.get("voice", "af_heart"))
        _kokoro_speed = float(kcfg.get("speed", 1.0))
        device = _resolve_device(str(kcfg.get("device", "auto")))
        lang_code = str(kcfg.get("lang_code") or _kokoro_voice[0])

        log.info(
            "TTS provider: kokoro  voice=%s speed=%.2f device=%s lang=%s",
            _kokoro_voice,
            _kokoro_speed,
            device,
            lang_code,
        )
        _kokoro_pipeline = KPipeline(lang_code=lang_code, device=device)

    else:
        try:
            from openai import OpenAI  # type: ignore[import-untyped]
        except ImportError as exc:
            raise RuntimeError("openai is not installed — run: pip install openai") from exc

        ocfg = cfg.get("openai", {})
        _model = str(ocfg.get("model", "gpt-4o-mini-tts"))
        _voice = str(ocfg.get("voice", "sage"))
        _speed = float(ocfg.get("speed", 1.0))
        _openai_fallback = bool(ocfg.get("fallback", True))
        _client = OpenAI()
        log.info(
            "TTS provider: openai  model=%s voice=%s speed=%.2f (local-Kokoro fallback %s)",
            _model,
            _voice,
            _speed,
            "on" if _openai_fallback else "off",
        )

    from kenzy import tlsutil
    from kenzy.tts.wyoming_server import install_wyoming_tts

    # Wyoming listener (F3.3): HA voice pipelines speak with Kenzy's voice.
    install_wyoming_tts(
        app,
        cfg,
        _synthesise,
        voice_name=_kokoro_voice if _provider == "kokoro" else _voice,
        bind=effective_bind(cfg),
    )

    uvicorn.run(
        app,
        host=effective_bind(cfg),
        port=int(cfg.get("port", 8769)),
        log_level=str(cfg.get("log_level", "info")).lower(),
        **tlsutil.uvicorn_tls_kwargs(cfg),
    )


if __name__ == "__main__":
    main()
