"""Wyoming protocol listener for kenzy-stt (F3.4).

Exposes the service's configured transcription backend as a Wyoming ASR
provider so a Home Assistant voice pipeline transcribes through Kenzy's STT —
one STT setup (provider, model, fallback chain) for the whole house instead
of a second engine configured inside HA.

Same shape as the TTS listener (``kenzy.tts.wyoming_server``): runs on the
uvicorn loop via a FastAPI startup hook, off by default
(``wyoming.enabled`` in stt.yaml), follows the service bind (plain
unauthenticated TCP — loopback unless ``KENZY_BIND``/--listen-all), and the
``wyoming`` package is imported lazily.

Protocol per request: ``transcribe?`` → ``audio-start`` → ``audio-chunk``* →
``audio-stop`` ⇒ we reply with one ``transcript``. Incoming audio is
converted to the pipeline's 16 kHz mono int16 (mean-mix + linear resample)
when the client sends something else.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

log = logging.getLogger(__name__)

#: The transcription pipeline's expected format (kenzy.protocol).
_RATE = 16000

#: Default port — the Wyoming/whisper convention, so HA operators guess right.
DEFAULT_PORT = 10300


def install_wyoming_stt(
    app: Any,
    cfg: dict[str, Any],
    transcribe: Callable[[bytes], Awaitable[str]],
    *,
    model_name: str,
    bind: str,
) -> None:
    """Wire the Wyoming ASR listener into the FastAPI app's lifecycle.

    ``transcribe`` is the service's async transcription entry point
    (``transcribe_pcm`` — 16 kHz mono int16 in, text out). No-op unless
    ``wyoming.enabled`` is true in the service config.
    """
    wcfg = cfg.get("wyoming") or {}
    if not wcfg.get("enabled", False):
        return
    port = int(wcfg.get("port", DEFAULT_PORT))

    try:
        import wyoming  # noqa: F401
    except ImportError:
        log.error(
            "wyoming.enabled is true but the 'wyoming' package is not installed — "
            "run: pip install wyoming  (Wyoming STT listener NOT started)"
        )
        return

    state: dict[str, Any] = {}

    async def _startup() -> None:
        from wyoming.server import AsyncServer

        server = AsyncServer.from_uri(f"tcp://{bind}:{port}")
        state["server"] = server
        state["task"] = asyncio.create_task(
            server.run(_handler_factory(transcribe, model_name)), name="wyoming-stt"
        )
        log.info("Wyoming STT listening on tcp://%s:%d (model %r)", bind, port, model_name)

    async def _shutdown() -> None:
        task = state.get("task")
        if task is not None:
            task.cancel()

    app.router.on_startup.append(_startup)
    app.router.on_shutdown.append(_shutdown)


def to_pipeline_pcm(audio: bytes, rate: int, width: int, channels: int) -> bytes:
    """Convert incoming Wyoming audio to the pipeline's 16 kHz mono int16.

    HA's pipeline already sends 16 kHz mono 16-bit, so this is normally the
    identity. Other widths are rejected (only 16-bit is supported); extra
    channels are mean-mixed; other rates are linearly resampled.
    """
    if width != 2:
        raise ValueError(f"unsupported sample width {width} (16-bit only)")
    if rate == _RATE and channels == 1:
        return audio

    import numpy as np  # the stt extra ships numpy (faster-whisper dependency)

    samples = np.frombuffer(audio, dtype=np.int16)
    if channels > 1:
        samples = samples[: len(samples) - (len(samples) % channels)]
        samples = samples.reshape(-1, channels).mean(axis=1).astype(np.int16)
    if rate != _RATE:
        n_out = int(round(len(samples) * _RATE / rate))
        if n_out <= 0:
            return b""
        x_out = np.linspace(0, len(samples) - 1, n_out)
        samples = np.interp(x_out, np.arange(len(samples)), samples).astype(np.int16)
    return samples.tobytes()


def _handler_factory(
    transcribe: Callable[[bytes], Awaitable[str]], model_name: str
) -> Callable[..., Any]:
    """Build the per-connection event handler class (wyoming imported lazily)."""
    from wyoming.asr import Transcribe, Transcript
    from wyoming.audio import AudioChunk, AudioStart, AudioStop
    from wyoming.error import Error
    from wyoming.event import Event
    from wyoming.info import AsrModel, AsrProgram, Attribution, Describe, Info
    from wyoming.server import AsyncEventHandler

    from kenzy import kenzy_version

    attribution = Attribution(name="Kenzy", url="https://kenzy.ai")
    info = Info(
        asr=[
            AsrProgram(
                name="kenzy",
                description="Kenzy's speech-to-text — the household voice assistant",
                attribution=attribution,
                installed=True,
                version=kenzy_version(),
                models=[
                    AsrModel(
                        name=model_name,
                        description="The STT backend this Kenzy install is configured with",
                        attribution=attribution,
                        installed=True,
                        version=None,
                        languages=["en"],
                    )
                ],
            )
        ]
    )

    class KenzySttHandler(AsyncEventHandler):  # type: ignore[misc]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self._audio = bytearray()
            self._rate, self._width, self._channels = _RATE, 2, 1

        async def handle_event(self, event: Event) -> bool:
            if Describe.is_type(event.type):
                await self.write_event(info.event())
                return True
            if Transcribe.is_type(event.type):
                return True  # language hint — we transcribe with the configured setup
            if AudioStart.is_type(event.type):
                meta = AudioStart.from_event(event)
                self._audio.clear()
                self._rate, self._width, self._channels = meta.rate, meta.width, meta.channels
                return True
            if AudioChunk.is_type(event.type):
                self._audio.extend(AudioChunk.from_event(event).audio)
                return True
            if not AudioStop.is_type(event.type):
                return True  # ignore anything else, keep the connection

            try:
                pcm = to_pipeline_pcm(
                    bytes(self._audio), self._rate, self._width, self._channels
                )
                text = await transcribe(pcm)
            except Exception as exc:
                log.warning("[wyoming] transcription failed: %s", exc)
                await self.write_event(Error(text=str(exc), code="transcribe-failed").event())
                self._audio.clear()
                return True
            log.info("[wyoming] %s", text or "(no speech detected)")
            await self.write_event(Transcript(text=text).event())
            self._audio.clear()
            return True

    return KenzySttHandler
