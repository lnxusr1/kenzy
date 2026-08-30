"""kenzy-s2s — the conversation engine's service entry point.

Boots exactly like every other backend: pull the effective config from the
server (``load_service_config`` — an explicit config path loads locally, the
dev/offline escape hatch), announce via the registration heartbeat, and serve.
The stt/tts URLs are auto-wired from the server's registry (an explicit value
in the service override wins — the multi-host escape hatch); the model
provider is this service's own config, deliberately independent of the classic
pipeline's model.

TLS follows the house rule: both ``tls.cert`` and ``tls.key`` present and
readable ⇒ the WebSocket speaks ``wss``; missing files warn and stay
plaintext — never auto-self-signed.
"""

from __future__ import annotations

import asyncio
import logging
import ssl
from typing import Any

from kenzy.s2s.server import Generate, Synthesize, Transcribe, serve
from kenzy.s2s.stages import ProviderConfig, http_synthesize, http_transcribe, provider_generate

log = logging.getLogger(__name__)


def _tls_context(cfg: dict[str, Any]) -> ssl.SSLContext | None:
    tls = cfg.get("tls") or {}
    cert, key = str(tls.get("cert", "") or ""), str(tls.get("key", "") or "")
    if not (cert and key):
        return None
    try:
        from kenzy import tlsutil

        return tlsutil.server_context(cert, key)
    except (OSError, ssl.SSLError) as exc:
        log.warning("kenzy-s2s: TLS pair unusable (%s) — serving plaintext", exc)
        return None


def _stages(cfg: dict[str, Any]) -> tuple[Transcribe, Generate, Synthesize]:
    stt = cfg.get("stt") or {}
    tts = cfg.get("tts") or {}
    stt_url = str(stt.get("url", "") or "")
    tts_url = str(tts.get("url", "") or "")
    if not stt_url:
        log.warning("kenzy-s2s: no stt url (config or registry) — transcription will fail")
    if not tts_url:
        log.warning("kenzy-s2s: no tts url (config or registry) — synthesis will fail")
    provider = ProviderConfig.from_config(cfg.get("provider") or {})
    log.info(
        "kenzy-s2s: stages — stt=%s tts=%s provider=%s (%s)",
        stt_url or "?",
        tts_url or "?",
        provider.model,
        provider.base_url or "api.openai.com",
    )
    return (
        http_transcribe(stt_url, timeout=float(stt.get("timeout", 30.0))),
        provider_generate(provider),
        http_synthesize(tts_url, timeout=float(tts.get("timeout", 30.0))),
    )


async def _run(cfg: dict[str, Any]) -> None:
    from kenzy.serviceboot import effective_bind

    host = effective_bind(cfg)
    port = int(cfg.get("port", 8771))
    transcribe, generate, synthesize = _stages(cfg)
    tls = _tls_context(cfg)
    server = await serve(
        host, port, transcribe=transcribe, generate=generate, synthesize=synthesize, ssl=tls
    )
    log.info(
        "kenzy-s2s listening on %s://%s:%d/v1/realtime",
        "wss" if tls else "ws",
        host,
        port,
    )
    try:
        await asyncio.get_running_loop().create_future()  # serve until signalled
    finally:
        server.close()
        await server.wait_closed()


def main() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    from kenzy.logutil import configure_logging, level_value
    from kenzy.serviceboot import load_service_config, start_registration

    configure_logging(logging.INFO)  # provisional, so the config pull's retries are visible
    cfg: dict[str, Any] = load_service_config("s2s")
    start_registration("s2s", cfg)
    configure_logging(level_value(cfg.get("log_level"), logging.INFO))

    try:
        asyncio.run(_run(cfg))
    except KeyboardInterrupt:
        log.info("kenzy-s2s: shutting down")


if __name__ == "__main__":
    main()
