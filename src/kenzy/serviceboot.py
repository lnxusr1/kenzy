"""Boot-time config pull for the backend HTTP services (stt/tts/llm/speaker).

Mirrors how a node pulls its config: the service discovers the server (mDNS,
like the node, or an explicit ``KENZY_SERVER_URL``), fetches its effective
config from the server's always-on ``GET /config/<service>`` endpoint, writes a
local copy as a record, and returns it. Per the centralized-config design the
service **blocks** (retry/backoff) until the server answers — the server is the
single source of truth.

Stdlib-only (``urllib``) so it adds no dependency to a service; secrets stay in
the host environment and are never part of the pulled config.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlencode, urlparse

log = logging.getLogger(__name__)

#: ws(s):// → http(s):// for talking to the server's config endpoint.
_SCHEME_MAP = {"ws": "http", "wss": "https"}


def _ssl_for(base: str) -> Any:
    """SSL context for https bases (None for plain http). Encrypted-but-unverified
    by default — KENZY_TLS_VERIFY=1 / KENZY_TLS_CA=<path> opt into verification."""
    if not base.startswith("https://"):
        return None
    from kenzy import tlsutil

    return tlsutil.client_context_from_env()


#: Last server HTTP base resolved by :func:`bootstrap_config`, reused by the
#: registration heartbeat so it doesn't re-run mDNS every tick.
_server_base: str | None = None


def _http_base(url: str) -> str:
    """Normalize a server URL (``ws://``/``http://``/bare ``host:port``) to an HTTP base."""
    parsed = urlparse(url if "://" in url else f"http://{url}")
    scheme = _SCHEME_MAP.get(parsed.scheme, parsed.scheme or "http")
    if not parsed.netloc:
        raise ValueError(f"cannot parse server URL: {url!r}")
    return f"{scheme}://{parsed.netloc}"


def _resolve_server_http(timeout: float) -> str:
    """Return the server's HTTP base: ``KENZY_SERVER_URL`` if set, else mDNS.

    Raises ``OSError`` when neither yields an address, so the caller retries.
    """
    env = os.environ.get("KENZY_SERVER_URL")
    if env:
        return _http_base(env)

    from kenzy.discovery import discover_server

    log.info("Discovering Kenzy server over mDNS…")
    url = discover_server(timeout)
    if url is None:
        raise OSError("no Kenzy server found (set KENZY_SERVER_URL or enable mDNS)")
    return _http_base(url)


def _save_local(service: str, cfg: dict[str, Any]) -> None:
    """Write a record of the pulled config into the config home (not read on boot).

    Boot always re-pulls from the server; this copy is just an operator-visible
    record of what's currently in effect, so it always lands in the writable
    config home rather than overwriting an existing dev/source config in place.
    """
    import yaml  # type: ignore[import-untyped]

    from kenzy.config import kenzy_home

    try:
        path = kenzy_home() / "configs" / f"{service}.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(cfg, default_flow_style=False, sort_keys=False))
    except OSError as exc:
        log.warning("Could not save pulled %s config locally: %s", service, exc)


def bootstrap_config(service: str, *, timeout: float = 5.0) -> dict[str, Any]:
    """Pull ``service``'s effective config from the server, blocking until it succeeds.

    Discovers the server (mDNS or ``KENZY_SERVER_URL``), fetches
    ``GET /config/<service>`` with the ``KENZY_SERVICE_TOKEN`` bearer, retries
    with exponential backoff (1 s → 60 s) on any failure, writes a local copy,
    and returns the config dict.
    """
    token = os.environ.get("KENZY_SERVICE_TOKEN")
    delay = 1
    while True:
        try:
            base = _resolve_server_http(timeout)
            req = urllib.request.Request(f"{base}/config/{service}")  # noqa: S310 (http only)
            if token:
                req.add_header("Authorization", f"Bearer {token}")
            with urllib.request.urlopen(req, context=_ssl_for(base), timeout=timeout) as resp:  # noqa: S310
                cfg = json.loads(resp.read().decode())
            if not isinstance(cfg, dict):
                raise ValueError("server returned a non-object config")
            _save_local(service, cfg)
            global _server_base
            _server_base = base
            log.info("Pulled %s config from %s", service, base)
            return cfg
        except (OSError, urllib.error.URLError, ValueError, json.JSONDecodeError) as exc:
            log.warning("Config pull for %s failed (%s); retrying in %ds", service, exc, delay)
            time.sleep(delay)
            delay = min(delay * 2, 60)


def fetch_service_config(service: str, *, timeout: float = 3.0) -> dict[str, Any] | None:
    """Best-effort single fetch of ``service``'s effective config from the server.

    Unlike :func:`bootstrap_config` this does **not** retry, block, or write a local
    copy — it returns ``None`` on any failure. For tools (e.g. ``kenzy-enroll``) that
    want server-provided values (like an auto-wired peer URL) but must stay responsive
    when the server is unreachable.
    """
    token = os.environ.get("KENZY_SERVICE_TOKEN")
    try:
        base = _resolve_server_http(timeout)
        req = urllib.request.Request(f"{base}/config/{service}")  # noqa: S310 (http only)
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        with urllib.request.urlopen(req, context=_ssl_for(base), timeout=timeout) as resp:  # noqa: S310
            cfg = json.loads(resp.read().decode())
        return cfg if isinstance(cfg, dict) else None
    except Exception:
        return None


def effective_bind(cfg: dict[str, Any]) -> str:
    """The host a service should bind to: ``KENZY_BIND`` if set (e.g. ``0.0.0.0`` from
    ``kenzy-deploy --listen-all``), else the config's ``host`` (default 127.0.0.1)."""
    return os.environ.get("KENZY_BIND") or str(cfg.get("host", "127.0.0.1"))


def start_registration(service: str, cfg: dict[str, Any], *, interval: float = 30.0) -> None:
    """Announce this service to the server periodically so it auto-appears.

    Backend services pull their config but don't otherwise tell the server they
    exist; the server learns of them only from static ``<svc>.url`` config. This
    heartbeat closes that gap: every ``interval`` seconds (and once at startup) the
    service GETs ``/register?service=&host=&port=&version=`` so the server can add it
    to the live backend set (dashboard + pipeline) without hand-wired URLs.

    Best-effort daemon thread (stdlib only); it never blocks startup and silently
    retries. The server resolves the reachable host from the request's source IP when
    the service binds ``0.0.0.0``, so a bind host of ``0.0.0.0`` is reported as-is.
    """
    port = int(cfg.get("port", 0) or 0)
    if not port:
        return
    # Report the effective bind host; the server maps 0.0.0.0 to our source IP.
    host = effective_bind(cfg)
    token = os.environ.get("KENZY_SERVICE_TOKEN")

    def _version() -> str:
        try:
            from kenzy import kenzy_version

            return kenzy_version()
        except Exception:
            return ""

    def _loop() -> None:
        params = urlencode({"service": service, "host": host, "port": port, "version": _version()})
        while True:
            base = _server_base
            if base is None:
                try:
                    base = _resolve_server_http(3.0)
                except OSError:
                    base = None
            if base:
                try:
                    req = urllib.request.Request(f"{base}/register?{params}")  # noqa: S310
                    if token:
                        req.add_header("Authorization", f"Bearer {token}")
                    urllib.request.urlopen(req, context=_ssl_for(base), timeout=5).close()  # noqa: S310
                except Exception as exc:  # noqa: BLE001 - heartbeat is best-effort
                    log.debug("Service registration for %s failed: %s", service, exc)
            time.sleep(interval)

    threading.Thread(target=_loop, name=f"kenzy-register-{service}", daemon=True).start()
    log.info("Service registration heartbeat started for %s", service)


def load_service_config(service: str, argv: list[str] | None = None) -> dict[str, Any]:
    """Resolve a service's config: explicit path arg → local file; else pull from server.

    The explicit-path form (e.g. ``kenzy-stt configs/stt.yaml``) is a dev/offline
    escape hatch that loads locally; with no argument the service pulls its config
    from the server via :func:`bootstrap_config` (blocking until it answers).
    """
    args = sys.argv if argv is None else argv
    if len(args) > 1:
        import yaml  # type: ignore[import-untyped]

        from kenzy.config import resolve_config

        with open(resolve_config(service, args[1])) as fh:
            data = yaml.safe_load(fh) or {}
        return data if isinstance(data, dict) else {}
    return bootstrap_config(service)
