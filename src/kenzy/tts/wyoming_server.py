"""Wyoming protocol listener for kenzy-tts (F3.3).

Exposes the service's configured voice as a Wyoming TTS provider so a Home
Assistant voice pipeline can speak its Assist replies in Kenzy's actual voice
(HA: *Settings → Devices & Services → Add integration → Wyoming Protocol*,
pointed at this host/port).

Runs inside the kenzy-tts process on the uvicorn event loop (started from a
FastAPI startup hook, like the jobs runner) and reuses the exact synthesis
path ``POST /speak`` uses — same provider, same voice, same fallback chain.

Off by default (``wyoming.enabled`` in tts.yaml). Wyoming is plain TCP with
no authentication, so the listener follows the service bind: loopback unless
``KENZY_BIND``/``--listen-all`` opens the service to the LAN. The ``wyoming``
package is imported lazily — the service runs without it unless enabled.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)

#: Audio format of every synthesis path in this service (see /speak).
_RATE, _WIDTH, _CHANNELS = 24000, 2, 1
#: Bytes per audio-chunk event (~42 ms at 24 kHz mono int16).
_CHUNK_BYTES = 2048

#: Default port — the Wyoming/Piper convention, so HA operators guess right.
DEFAULT_PORT = 10200


def install_wyoming_tts(
    app: Any,
    cfg: dict[str, Any],
    synthesise: Callable[[str, str], bytes],
    *,
    voice_name: str,
    bind: str,
) -> None:
    """Wire the Wyoming TTS listener into the FastAPI app's lifecycle.

    ``synthesise`` is the service's blocking synthesis entry point
    (``_synthesise(text, voice_prompt)``); it runs on the default executor per
    request. No-op unless ``wyoming.enabled`` is true in the service config.
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
            "run: pip install wyoming  (Wyoming TTS listener NOT started)"
        )
        return

    state: dict[str, Any] = {}

    async def _startup() -> None:
        from wyoming.server import AsyncServer

        server = AsyncServer.from_uri(f"tcp://{bind}:{port}")
        state["server"] = server
        state["task"] = asyncio.create_task(
            server.run(_handler_factory(synthesise, voice_name)), name="wyoming-tts"
        )
        log.info("Wyoming TTS listening on tcp://%s:%d (voice %r)", bind, port, voice_name)

    async def _shutdown() -> None:
        task = state.get("task")
        if task is not None:
            task.cancel()

    app.router.on_startup.append(_startup)
    app.router.on_shutdown.append(_shutdown)


def _handler_factory(
    synthesise: Callable[[str, str], bytes], voice_name: str
) -> Callable[..., Any]:
    """Build the per-connection event handler class (wyoming imported lazily)."""
    from wyoming.audio import AudioChunk, AudioStart, AudioStop
    from wyoming.error import Error
    from wyoming.event import Event
    from wyoming.info import Attribution, Describe, Info, TtsProgram, TtsVoice
    from wyoming.server import AsyncEventHandler
    from wyoming.tts import Synthesize

    from kenzy import kenzy_version

    attribution = Attribution(name="Kenzy", url="https://kenzy.ai")
    info = Info(
        tts=[
            TtsProgram(
                name="kenzy",
                description="Kenzy's voice — the household voice assistant",
                attribution=attribution,
                installed=True,
                version=kenzy_version(),
                voices=[
                    TtsVoice(
                        name=voice_name,
                        description="The voice this Kenzy install is configured with",
                        attribution=attribution,
                        installed=True,
                        version=None,
                        languages=["en"],
                    )
                ],
            )
        ]
    )

    class KenzyTtsHandler(AsyncEventHandler):  # type: ignore[misc]
        async def handle_event(self, event: Event) -> bool:
            if Describe.is_type(event.type):
                await self.write_event(info.event())
                return True
            if not Synthesize.is_type(event.type):
                return True  # ignore anything else, keep the connection

            text = Synthesize.from_event(event).text
            log.info("[wyoming] speak: %s", text[:80])
            loop = asyncio.get_running_loop()
            try:
                pcm = await loop.run_in_executor(None, synthesise, text, "")
            except Exception as exc:
                log.warning("[wyoming] synthesis failed: %s", exc)
                await self.write_event(Error(text=str(exc), code="synthesis-failed").event())
                return True

            await self.write_event(
                AudioStart(rate=_RATE, width=_WIDTH, channels=_CHANNELS).event()
            )
            for i in range(0, len(pcm), _CHUNK_BYTES):
                await self.write_event(
                    AudioChunk(
                        audio=pcm[i : i + _CHUNK_BYTES],
                        rate=_RATE,
                        width=_WIDTH,
                        channels=_CHANNELS,
                    ).event()
                )
            await self.write_event(AudioStop().event())
            return True

    return KenzyTtsHandler
