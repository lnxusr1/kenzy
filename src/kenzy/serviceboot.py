"""Boot-time config pull for the backend HTTP services (stt/tts/llm/speaker).

Mirrors how a node pulls its config: the service discovers the server (mDNS,
like the node, or an explicit ``KENZY_SERVER_URL``), fetches its effective
config from the server's always-on ``GET /config/<service>`` endpoint, writes a
local copy as a record, and returns it. Per the centralized-config design the
service **blocks** (retry/backoff) until the server answers — the server is the
single source of truth.

Stdlib-only (``http.client``) so it adds no dependency to a service.
"""

from __future__ import annotations

import http.client
import json
import logging
import os
import sys
import threading
import time
from typing import Any
from urllib.parse import urlencode, urlparse

log = logging.getLogger(__name__)

#: ws(s):// → http(s):// for talking to the server's config endpoint.
_SCHEME_MAP = {"ws": "http", "wss": "https"}


def _signed_get(base: str, path: str, token: str | None, timeout: float) -> tuple[int, bytes, bool]:
    """GET ``base + path`` with token-proof auth (stdlib http.client).

    Sends the ``X-Kenzy-Auth`` token-proof signature only (3.12 — the raw
    token never rides the wire). Reads the server's TLS cert off the connection to verify the
    ``X-Kenzy-Sig`` response signature — binding the reply to the channel we
    actually spoke over, so a relay presenting a different cert is caught.

    Returns ``(status, body, response_ok)``. ``response_ok`` is False only when
    the server signed a reply that failed verification (relay/tamper); an
    unsigned reply (legacy/plaintext server) leaves it True. Raises the
    OSError family on transport failure so callers retry.
    """
    parsed = urlparse(base)
    host, port = parsed.hostname or "127.0.0.1", parsed.port
    https = parsed.scheme == "https"
    if https:
        from kenzy import tlsutil

        conn: http.client.HTTPConnection = http.client.HTTPSConnection(
            host, port, context=tlsutil.client_context_from_env(), timeout=timeout
        )
    else:
        conn = http.client.HTTPConnection(host, port, timeout=timeout)
    ts: int | None = None
    try:
        conn.connect()
        binding = b""
        if https:
            from kenzy import tlsutil

            sock = conn.sock
            der = sock.getpeercert(binary_form=True) if sock is not None else None  # type: ignore[union-attr]
            binding = tlsutil.peer_cert_binding(der)
        headers: dict[str, str] = {}
        if token:
            from kenzy import serviceauth

            ts = int(time.time())
            sign_path = path.split("?", 1)[0]
            headers[serviceauth.SIG_HEADER] = serviceauth.sign_service_request(
                token, "GET", sign_path, ts=ts
            )
            # Token-proof only — the raw token never rides the wire.
        conn.request("GET", path, headers=headers)
        resp = conn.getresponse()
        body = resp.read()
        status = resp.status
        sig = resp.getheader("X-Kenzy-Sig")
    finally:
        conn.close()

    response_ok = True
    if token and ts is not None and sig:
        from kenzy import serviceauth

        response_ok = serviceauth.verify_service_response(sig, token, ts, body, binding=binding)
    return status, body, response_ok


#: Last server HTTP base resolved by :func:`bootstrap_config`, reused by the
#: registration heartbeat so it doesn't re-run mDNS every tick.
_server_base: str | None = None


def server_base() -> str | None:
    """The server's HTTP base as resolved by config-pull/registration (None
    until the first successful pull). Lets service code poke the server's
    always-on endpoints without re-running discovery."""
    return _server_base


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


def _apply_secrets(cfg: dict[str, Any]) -> None:
    """Move a served ``_secrets`` map into this process's environment (stage b).

    Pops the map out of ``cfg`` (so it never lands in the on-disk config record)
    and sets each key in ``os.environ``. **Server value wins** over any local
    ``.env`` — central rotation must not be shadowed by a stale local copy. Only
    ever present on an authenticated TLS pull (the server gates it); on
    plaintext/unauthenticated the service just keeps its own environment.
    """
    secrets = cfg.pop("_secrets", None)
    if not isinstance(secrets, dict):
        return
    for name, value in secrets.items():
        if isinstance(name, str) and isinstance(value, str) and value:
            os.environ[name] = value
    if secrets:
        log.info("Applied %d secret(s) from the server to the environment", len(secrets))


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
    ``GET /config/<service>`` with a token-proof signature, retries
    with exponential backoff (1 s → 60 s) on any failure, writes a local copy,
    and returns the config dict.
    """
    from kenzy.serviceauth import service_token_from_env

    token = service_token_from_env()
    delay = 1
    while True:
        try:
            base = _resolve_server_http(timeout)
            status, body, response_ok = _signed_get(base, f"/config/{service}", token, timeout)
            if status != 200:
                raise OSError(f"server returned HTTP {status} for /config/{service}")
            if not response_ok:
                raise ValueError("config response signature invalid — possible relay")
            cfg = json.loads(body.decode())
            if not isinstance(cfg, dict):
                raise ValueError("server returned a non-object config")
            _apply_secrets(cfg)  # pop _secrets into our env BEFORE we persist a record
            _save_local(service, cfg)
            global _server_base
            _server_base = base
            log.info("Pulled %s config from %s", service, base)
            return cfg
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            log.warning("Config pull for %s failed (%s); retrying in %ds", service, exc, delay)
            time.sleep(delay)
            delay = min(delay * 2, 60)


def populate_data(service: str, *, timeout: float = 10.0) -> None:
    """Fill a service's data slice from the server when its own copy is empty.

    The mirror of config-pull for DATA (speaker embeddings; the LLM's skills +
    curation): a freshly installed or reimaged host boots with an empty slice
    and fetches it from the server's ``GET /data/<service>`` — so disaster
    recovery is "restore the server, turn the hosts on." **Local data always
    wins**: a host that already has its slice never calls the server, so a
    stale server copy can't clobber a live fleet. Best-effort — config-pull has
    already confirmed the server is reachable; a fresh install with nothing to
    pull is normal, not an error.
    """
    from kenzy import backup
    from kenzy.config import kenzy_data_root

    if service not in backup.DATA_SLICES:
        return
    root = kenzy_data_root()
    if backup.slice_populated(root, service):
        return  # local data wins — never overwrite a live host

    from kenzy.serviceauth import service_token_from_env

    token = service_token_from_env()
    try:
        base = _server_base or _resolve_server_http(timeout)
        status, body, response_ok = _signed_get(base, f"/data/{service}", token, timeout)
        if status != 200 or not response_ok:
            log.warning(
                "Data self-populate for %s: server returned %s (verified=%s)",
                service,
                status,
                response_ok,
            )
            return
        entries = backup.unpack_archive_bytes(body)
        written = backup.write_slice(entries, root)
        if written:
            log.info("Self-populated %s data from the server: %d file(s)", service, len(written))
        else:
            log.info("Self-populate for %s: server has no data yet (fresh install)", service)
    except Exception as exc:  # noqa: BLE001 - best-effort; a fresh install has nothing to pull
        log.warning("Data self-populate for %s failed (%s); continuing", service, exc)


def fetch_service_config(service: str, *, timeout: float = 3.0) -> dict[str, Any] | None:
    """Best-effort single fetch of ``service``'s effective config from the server.

    Unlike :func:`bootstrap_config` this does **not** retry, block, or write a local
    copy — it returns ``None`` on any failure. For tools (e.g. ``kenzy-enroll``) that
    want server-provided values (like an auto-wired peer URL) but must stay responsive
    when the server is unreachable.
    """
    from kenzy.serviceauth import service_token_from_env

    token = service_token_from_env()
    try:
        base = _resolve_server_http(timeout)
        status, body, response_ok = _signed_get(base, f"/config/{service}", token, timeout)
        if status != 200 or not response_ok:
            return None
        cfg = json.loads(body.decode())
        if isinstance(cfg, dict):
            cfg.pop("_secrets", None)  # a peer-URL fetch has no business carrying secrets
            return cfg
        return None
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
    from kenzy.serviceauth import service_token_from_env

    token = service_token_from_env()

    def _version() -> str:
        try:
            from kenzy import kenzy_version

            return kenzy_version()
        except Exception:
            return ""

    from kenzy import tlsutil

    serves_tls = bool(tlsutil.uvicorn_tls_kwargs(cfg))

    def _loop() -> None:
        params = urlencode(
            {
                "service": service,
                "host": host,
                "port": port,
                "version": _version(),
                "tls": "1" if serves_tls else "0",
            }
        )
        while True:
            base = _server_base
            if base is None:
                try:
                    base = _resolve_server_http(3.0)
                except OSError:
                    base = None
            if base:
                try:
                    _signed_get(base, f"/register?{params}", token, 5.0)
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
