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
import time
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlparse

log = logging.getLogger(__name__)

#: ws(s):// → http(s):// for talking to the server's config endpoint.
_SCHEME_MAP = {"ws": "http", "wss": "https"}


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
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
                cfg = json.loads(resp.read().decode())
            if not isinstance(cfg, dict):
                raise ValueError("server returned a non-object config")
            _save_local(service, cfg)
            log.info("Pulled %s config from %s", service, base)
            return cfg
        except (OSError, urllib.error.URLError, ValueError, json.JSONDecodeError) as exc:
            log.warning("Config pull for %s failed (%s); retrying in %ds", service, exc, delay)
            time.sleep(delay)
            delay = min(delay * 2, 60)


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
