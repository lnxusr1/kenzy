"""Optional TLS for Kenzy's own connections (F-13, first slice).

Philosophy: **encryption is opt-in, verification is opt-in on top of that.**
A home-LAN install uses a self-signed certificate, which no client trusts
without installing a CA chain — so Kenzy's own clients (nodes, backend
services) default to *encrypted but unverified*: that stops passive sniffing
on the LAN without pretending a trust chain exists. Operators who deploy a
real CA can turn verification on (``tls_verify: true`` / ``KENZY_TLS_VERIFY=1``)
and optionally pin a CA file (``tls_ca`` / ``KENZY_TLS_CA``).

Generate a self-signed cert with:

    openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \\
        -keyout kenzy.key -out kenzy.crt -subj "/CN=kenzy"

Scope of this slice: the server terminates TLS on the node WebSocket port and
the dashboard; nodes and service config-pull/registration speak wss/https to
it. Backend-service HTTP (server↔stt/tts/llm/speaker) is not covered yet —
it is loopback in the default install (see design/security-hardening.md).
"""

from __future__ import annotations

import os
import ssl
from typing import Any


def server_context(cert: str, key: str) -> ssl.SSLContext:
    """A server-side context from a cert/key pair (paths on the server host)."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    return ctx


def client_context(verify: bool = False, ca: str | None = None) -> ssl.SSLContext:
    """A client-side context. ``ca`` pins a CA bundle (implies verification);
    ``verify`` uses the system trust store; the default trusts nothing and
    checks nothing — encrypted, unverified (the self-signed LAN posture)."""
    if ca:
        return ssl.create_default_context(cafile=ca)
    ctx = ssl.create_default_context()
    if not verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def client_context_from_env() -> ssl.SSLContext:
    """Client context for stdlib callers (serviceboot): ``KENZY_TLS_VERIFY=1``
    turns verification on; ``KENZY_TLS_CA=/path`` pins a CA bundle."""
    verify = os.environ.get("KENZY_TLS_VERIFY", "").strip().lower() in ("1", "true", "yes")
    ca = os.environ.get("KENZY_TLS_CA", "").strip() or None
    return client_context(verify=verify, ca=ca)


_SHARED_CLIENT: ssl.SSLContext | None = None


def httpx_verify() -> ssl.SSLContext:
    """Shared ``verify=`` context for Kenzy's internal httpx calls (server↔services,
    dashboard proxies). Env-configured like :func:`client_context_from_env`, cached.
    Harmless on plain-http URLs (the context is simply unused)."""
    global _SHARED_CLIENT
    if _SHARED_CLIENT is None:
        _SHARED_CLIENT = client_context_from_env()
    return _SHARED_CLIENT


def uvicorn_tls_kwargs(cfg: dict[str, Any]) -> dict[str, Any]:
    """ssl_certfile/ssl_keyfile kwargs for a backend service's uvicorn listener.

    The pair comes from the service's config ``tls:`` block — auto-injected by
    the server for co-located services when the server itself has TLS — or from
    ``KENZY_TLS_CERT``/``KENZY_TLS_KEY`` env (the multi-host path: the served
    config strips secret-like keys, so ``tls.key`` can't ride an override).
    Missing files degrade to plaintext with a warning, mirroring the server.
    """
    import logging

    log = logging.getLogger(__name__)
    tls = cfg.get("tls") or {}
    cert = os.environ.get("KENZY_TLS_CERT", "").strip() or tls.get("cert")
    key = os.environ.get("KENZY_TLS_KEY", "").strip() or tls.get("key")
    if not (cert and key):
        return {}
    if not (os.path.isfile(str(cert)) and os.path.isfile(str(key))):
        log.warning(
            "TLS configured but cert/key not found (%s, %s) — continuing WITHOUT TLS", cert, key
        )
        return {}
    log.info("TLS enabled on this service (https)")
    return {"ssl_certfile": str(cert), "ssl_keyfile": str(key)}
