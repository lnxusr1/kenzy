"""Kenzy server dashboard (Phase 0 foundation).

A small, opt-in web dashboard served by ``kenzy-server`` on its own
bind/port via the ``websockets`` HTTP hook — no extra dependency. It exposes a
read-only fleet/health view of the connected nodes and the configured backend
services.

Zero overhead when off: this module is only imported and started when
``dashboard.enabled`` is true (see ``server.main``). A disabled server mounts no
route, allocates no buffers, and leaves the node side completely untouched.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hmac
import json
import logging
import secrets
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, quote, urlsplit

import websockets
from websockets.datastructures import Headers
from websockets.http11 import Request, Response

from kenzy import kenzy_version, serviceauth
from kenzy.logutil import install_ring_handler, level_value

if TYPE_CHECKING:
    from websockets.asyncio.server import ServerConnection

    from kenzy.server.server import AudioServer

log = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).parent / "dashboard_static"

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
}


def _default_dashboard_auth() -> tuple[str | None, str | None]:
    """The shipped default dashboard login, read from the packaged ``server.yaml``.

    The default lives in config (operator-editable, a single source) rather than
    hardcoded in source. Used only as a fallback when the active config enables the
    dashboard but omits an ``auth`` block (e.g. a bare/partial config), so login
    still works out of the box. The hash is stable, so the cookie-signing secret
    survives restarts.
    """
    try:
        import yaml  # type: ignore[import-untyped]

        from kenzy.config import packaged_config

        data = yaml.safe_load(packaged_config("server").read_text()) or {}
        auth = (data.get("dashboard") or {}).get("auth") or {}
        return (auth.get("username") or None), (auth.get("password_hash") or None)
    except Exception:  # packaged default unreadable — no fallback creds
        return None, None


@dataclass
class DashboardConfig:
    enabled: bool = False
    bind: str = "127.0.0.1"
    port: int = 8770
    auth_token: str | None = None
    auth_username: str | None = None
    auth_password_hash: str | None = None
    # logs/controls default ON (matching the shipped server.yaml) so a partial config
    # that just enables the dashboard gets the full experience; set false to opt out.
    logs: bool = True
    controls: bool = True
    # Optional Host allow-list (hostnames, no port) for DNS-rebinding defense on the
    # WS/mutation channel. Empty ⇒ no Host restriction (the Origin==Host check still
    # applies); set it when serving under a fixed hostname (F-6).
    allowed_hosts: tuple[str, ...] = ()

    @classmethod
    def from_cfg(cls, cfg: dict[str, Any]) -> DashboardConfig:
        d = cfg.get("dashboard", {}) or {}
        auth = d.get("auth", {}) or {}
        # Fall back to the shipped default login (from the packaged server.yaml) when
        # the active config enables the dashboard without its own auth block.
        def_user, def_hash = _default_dashboard_auth()
        return cls(
            enabled=bool(d.get("enabled", False)),
            bind=str(d.get("bind", "127.0.0.1")),
            port=int(d.get("port", 8770)),
            auth_token=d.get("auth_token") or None,
            auth_username=auth.get("username") or def_user,
            auth_password_hash=auth.get("password_hash") or def_hash,
            logs=bool(d.get("logs", True)),
            controls=bool(d.get("controls", True)),
            allowed_hosts=tuple(str(h) for h in (d.get("allowed_hosts") or [])),
        )


def _service_targets(cfg: dict[str, Any]) -> dict[str, str]:
    """Map service name → base health URL, derived from configured endpoint URLs."""
    targets: dict[str, str] = {}
    for name in ("stt", "tts", "llm", "speaker"):
        url = (cfg.get(name, {}) or {}).get("url")
        if not url:
            continue
        parts = urlsplit(str(url))
        if parts.scheme and parts.netloc:
            targets[name] = f"{parts.scheme}://{parts.netloc}/health"
    return targets


_MISSING = object()


def _dotted_get(d: dict[str, Any], key: str) -> Any:
    """Return the value at a dotted path, or _MISSING if any segment is absent."""
    cur: Any = d
    for part in key.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return _MISSING
        cur = cur[part]
    return cur


def _dotted_set(d: dict[str, Any], key: str, value: Any) -> None:
    cur = d
    parts = key.split(".")
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value


def _version_tuple(v: str) -> tuple[int, ...]:
    """Best-effort numeric version tuple (leading digits of each dotted segment),
    e.g. ``"3.1.10"`` → ``(3, 1, 10)``. Non-numeric segments contribute 0."""
    out: list[int] = []
    for seg in v.split(".")[:4]:
        digits = ""
        for ch in seg:
            if ch.isdigit():
                digits += ch
            else:
                break
        out.append(int(digits) if digits else 0)
    return tuple(out)


def _is_newer(latest: str, current: str) -> bool:
    """True if ``latest`` is a newer version than ``current`` (numeric compare)."""
    try:
        return _version_tuple(latest) > _version_tuple(current)
    except Exception:
        return False


def _coerce_server_value(raw: Any, typ: str) -> Any:
    if typ == "bool":
        return bool(raw)
    if typ == "num":
        return float(raw)
    return str(raw)


class Dashboard:
    """Serves the read-only fleet/health dashboard over HTTP."""

    def __init__(
        self,
        server: AudioServer,
        cfg: dict[str, Any],
        dcfg: DashboardConfig,
        config_path: Path | str | None = None,
    ) -> None:
        self._server = server
        self._dcfg = dcfg
        self._service_urls = _service_targets(cfg)
        self._discovery = cfg.get("discovery", {}) or {}
        self._cfg = cfg  # effective server config (server.yaml ← server.local.yaml)
        # Path to server.yaml, so the Settings page can persist a password change
        # (None when the server was started without a resolvable config file).
        self._config_path = Path(config_path) if config_path else None
        # Cookie-signing key: the password hash is a stable server-side secret, so
        # sessions survive restarts and a password change invalidates them. Fall
        # back to a per-process random key when no password is configured.
        self._cookie_secret = dcfg.auth_password_hash or secrets.token_urlsafe(32)
        # F-9: flag (and loudly warn about) the shipped default password.
        self._default_password = bool(
            dcfg.auth_password_hash
            and serviceauth.verify_password("password", dcfg.auth_password_hash)
        )
        if dcfg.enabled and self._default_password:
            if dcfg.bind in ("127.0.0.1", "localhost", "::1"):
                log.warning(
                    "Dashboard is using the DEFAULT password (admin/password) — change it in "
                    "Settings or with `kenzy-passwd`."
                )
            else:
                log.warning(
                    "SECURITY: dashboard is using the DEFAULT password (admin/password) AND is "
                    "bound to %s:%s — it is plaintext HTTP reachable by anyone on your network, "
                    "who could take control. Change it now in Settings or with `kenzy-passwd`.",
                    dcfg.bind,
                    dcfg.port,
                )
        # Live-push: connected browser WS clients + a short health-check cache so a
        # burst of node state changes can't hammer the backends.
        self._clients: set[ServerConnection] = set()
        self._svc_cache: tuple[float, list[dict[str, Any]]] | None = None
        # Cache of the latest kenzy version on PyPI (checked lazily; ~1 h TTL) so the
        # update check doesn't hit PyPI on every Settings load.
        self._pypi_cache: tuple[float, str | None] | None = None
        server.add_state_listener(self._on_state_change)
        # Calibration: which connected browser is tuning which node (connection → node_id).
        # Tune samples are relayed only to the subscribed client, not all clients, and
        # never through the heavy state snapshot.
        self._tune_subs: dict[ServerConnection, str] = {}
        server.add_tune_listener(self._on_tune_sample)
        # Pull-based logs (only when the `logs` sub-flag is on): tell nodes to keep a
        # buffer, and capture the server's own logs for the viewer down to the
        # configured capture depth (default debug).
        server._capture_node_logs = dcfg.logs
        capture = level_value(cfg.get("log_capture_level"), logging.DEBUG)
        self._server_logs = install_ring_handler("kenzy", level=capture) if dcfg.logs else None
        # Pipeline observability (Activity tab): a bounded ring of recent completed
        # pipelines. Recorded only when the `logs` flag is on (the records carry
        # transcripts), so it's off/zero-overhead otherwise.
        self._sessions: deque[dict[str, Any]] = deque(maxlen=200)
        if dcfg.logs:
            server.add_session_listener(self._on_session)

    # ------------------------------------------------------------------
    # Auth — read-only GETs are LAN-open; mutations need a login cookie
    # (browser) or the optional auth_token bearer (API/CLI).
    # ------------------------------------------------------------------

    def _cookie_value(self, request: Request, name: str) -> str | None:
        for part in request.headers.get("Cookie", "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == name:
                return v
        return None

    def _current_user(self, request: Request) -> str | None:
        """Return the authenticated username, from the session cookie or bearer."""
        cookie = self._cookie_value(request, serviceauth.COOKIE_NAME)
        if cookie:
            user = serviceauth.verify_cookie(cookie, self._cookie_secret)
            if user:
                return user
        token = self._dcfg.auth_token
        if token:
            header = request.headers.get("Authorization", "")
            presented = header[7:] if header.startswith("Bearer ") else ""
            if hmac.compare_digest(presented, token):
                return "api"
        return None

    def _authorized_mutation(self, request: Request) -> bool:
        return self._current_user(request) is not None

    def _origin_host_ok(self, request: Request) -> bool:
        """Cross-site / DNS-rebinding guard for the WS (mutation) channel (F-6).

        A browser sends ``Origin`` on a WS handshake — require it to match the ``Host``
        we were reached on (defeats cross-site WebSocket hijacking). Non-browser clients
        (CLI/bearer) send no Origin and are allowed (they still need the auth token).
        When ``allowed_hosts`` is set, also require the Host to be in it (defeats DNS
        rebinding, where Origin==Host both carry the attacker's name).
        """
        host = request.headers.get("Host", "")
        origin = request.headers.get("Origin")
        if origin:
            from urllib.parse import urlsplit

            if urlsplit(origin).netloc != host:
                return False
        if self._dcfg.allowed_hosts:
            hostname = host.rsplit(":", 1)[0].strip("[]")  # drop port; unbracket IPv6
            if hostname not in self._dcfg.allowed_hosts:
                return False
        return True

    def _login(self, request: Request) -> Response:
        """Verify HTTP Basic credentials and issue a signed session cookie."""
        header = request.headers.get("Authorization", "")
        user = pw = ""
        if header.startswith("Basic "):
            try:
                user, _, pw = base64.b64decode(header[6:]).decode("utf-8", "replace").partition(":")
            except (binascii.Error, ValueError):
                user = pw = ""
        ok = (
            self._dcfg.auth_username is not None
            and self._dcfg.auth_password_hash is not None
            and hmac.compare_digest(user, self._dcfg.auth_username)
            and serviceauth.verify_password(pw, self._dcfg.auth_password_hash)
        )
        if not ok:
            return self._json(401, {"error": "invalid credentials"})
        token = serviceauth.sign_cookie(user, self._cookie_secret)
        cookie = self._cookie_header(token, request, max_age=43200)
        return self._json(200, {"ok": True, "username": user}, set_cookie=cookie)

    def _logout(self, request: Request) -> Response:
        cookie = self._cookie_header("", request, max_age=0)
        return self._json(200, {"ok": True}, set_cookie=cookie)

    @staticmethod
    def _cookie_header(value: str, request: Request, *, max_age: int) -> str:
        """Build the session cookie. Adds ``Secure`` when the request arrived over TLS
        (directly or via a reverse proxy that forwards the scheme), so the cookie isn't
        sent in cleartext once HTTPS is in front (F-7). Plaintext stays the default."""
        secure = request.headers.get("X-Forwarded-Proto", "").lower() == "https"
        attrs = "HttpOnly; SameSite=Strict; Path=/"
        if secure:
            attrs += "; Secure"
        return f"{serviceauth.COOKIE_NAME}={value}; {attrs}; Max-Age={max_age}"

    # ------------------------------------------------------------------
    # State surfaces
    # ------------------------------------------------------------------

    def _nodes_state(self) -> list[dict[str, Any]]:
        nodes: list[dict[str, Any]] = []
        for node_id, session in sorted(self._server._nodes.items()):
            has_override = self._server.read_node_override(node_id) != {}
            addr = getattr(session.ws, "remote_address", None)
            nodes.append(
                {
                    "node_id": node_id,
                    "room": session.room_id,
                    "ip": addr[0] if addr else None,
                    "configured": has_override,
                    "connected": True,
                    "streaming": bool(session.streaming),
                    "session_id": session.session_id,
                    "audio_ok": bool(session.audio_ok),
                    "audio_error": session.audio_error,
                    "version": session.kenzy_version,
                }
            )
        return nodes

    async def _services_state(self) -> list[dict[str, Any]]:
        # Statically-configured backends plus any that auto-registered with the server
        # (GET /register); the latter win on name collision (live address).
        targets = {**self._service_urls, **self._server.announced_health_urls()}
        if not targets:
            return []
        import time

        if self._svc_cache and time.monotonic() - self._svc_cache[0] < 3.0:
            return self._svc_cache[1]
        import httpx

        async def check(name: str, url: str) -> dict[str, Any]:
            try:
                async with httpx.AsyncClient(timeout=2.0) as client:
                    r = await client.get(url)
                detail = (
                    r.json()
                    if r.headers.get("content-type", "").startswith("application/json")
                    else {}
                )
                return {"name": name, "up": r.status_code == 200, "detail": detail}
            except Exception:
                return {"name": name, "up": False, "detail": {}}

        result = list(await asyncio.gather(*(check(n, u) for n, u in targets.items())))
        self._svc_cache = (time.monotonic(), result)
        return result

    async def _state(self) -> dict[str, Any]:
        return {
            "nodes": self._nodes_state(),
            "services": await self._services_state(),
            "flags": {
                "logs": self._dcfg.logs,
                "controls": self._dcfg.controls,
            },
        }

    # ------------------------------------------------------------------
    # Server self-config editor (safe subset; written to server.local.yaml)
    # ------------------------------------------------------------------

    def _read_server_override(self) -> dict[str, Any]:
        import yaml  # type: ignore[import-untyped]

        from kenzy.server.server import _server_override_path

        if self._config_path is None:
            return {}
        path = _server_override_path(self._config_path)
        if not path.is_file():
            return {}
        try:
            data = yaml.safe_load(path.read_text()) or {}
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _server_config_state(self) -> dict[str, Any]:
        from kenzy.server.server import _SERVER_EDITABLE

        override = self._read_server_override()
        fields = []
        for key, typ in _SERVER_EDITABLE.items():
            value = _dotted_get(self._cfg, key)
            fields.append(
                {
                    "key": key,
                    "type": typ,
                    "value": None if value is _MISSING else value,
                    "overridden": _dotted_get(override, key) is not _MISSING,
                }
            )
        return {"fields": fields, "writable": self._config_path is not None}

    def _write_server_override(self, patch: dict[str, Any]) -> None:
        """Validate + persist dashboard-edited server settings to server.local.yaml."""
        import yaml

        from kenzy.server.server import _SERVER_EDITABLE, _server_override_path

        if self._config_path is None:
            raise ValueError("server config file location is unknown")
        if not isinstance(patch, dict):
            raise ValueError("config must be a mapping")
        unknown = sorted(k for k in patch if k not in _SERVER_EDITABLE)
        if unknown:
            raise ValueError("unsupported keys: " + ", ".join(unknown))
        override = self._read_server_override()
        for key, raw in patch.items():
            try:
                _dotted_set(override, key, _coerce_server_value(raw, _SERVER_EDITABLE[key]))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"invalid value for {key}: {exc}") from exc
        path = _server_override_path(self._config_path)
        path.write_text(yaml.safe_dump(override, default_flow_style=False, sort_keys=True))
        log.info("Wrote server override %s (%d key(s))", path, len(patch))

    def _settings_state(self) -> dict[str, Any]:
        """Read-only server/dashboard info shown on the Settings page."""
        version = kenzy_version()
        return {
            "version": version,
            "username": self._dcfg.auth_username,
            "server": {"host": self._server._host, "port": self._server._port},
            "dashboard": {"bind": self._dcfg.bind, "port": self._dcfg.port},
            "discovery": {
                "enabled": bool(self._discovery.get("enabled", True)),
                "instance": str(self._discovery.get("instance", "kenzy-server")),
                "auth_required": bool(self._discovery.get("token")),
            },
            # The join token is a *provisioning* secret (paste it into a node install:
            # `kenzy-init --profile node --token …`). Surfaced only here, behind auth,
            # so an operator never has to memorize it (F-2). Deliberate, narrow exception
            # to "secrets never served": this endpoint is auth-gated and it's not an
            # upstream API key.
            "join_token": self._discovery.get("token") or None,
            "api_token": self._dcfg.auth_token or None,
            "services": [
                {"name": n, "url": u[: -len("/health")]} for n, u in self._service_urls.items()
            ],
            "flags": {
                "controls": self._dcfg.controls,
                "logs": self._dcfg.logs,
            },
            # The Settings password form is only offered when we can persist it.
            "can_set_password": self._config_path is not None and self._config_path.is_file(),
            # F-9: surface "still on the default password" so the SPA can nag.
            "default_password": self._default_password,
        }

    def _set_password(self, new_password: str) -> None:
        """Persist a new dashboard password to server.yaml and apply it live.

        Rewrites ``dashboard.auth.password_hash`` (preserving comments via
        :func:`kenzy.passwd.set_auth`) and updates the in-memory hash + cookie
        secret so it takes effect immediately — no restart. Because the signing
        secret changes, existing sessions are invalidated and must sign in again.
        """
        if self._config_path is None or not self._config_path.is_file():
            raise OSError("server.yaml not found — cannot persist the password")
        from kenzy.passwd import set_auth
        from kenzy.serviceauth import hash_password

        username = self._dcfg.auth_username or "admin"
        new_hash = hash_password(new_password)
        text = self._config_path.read_text()
        self._config_path.write_text(set_auth(text, username, new_hash))
        self._dcfg.auth_username = username
        self._dcfg.auth_password_hash = new_hash
        self._cookie_secret = new_hash
        # Re-evaluate the default-password flag so the dashboard's "default password"
        # warning clears immediately (or re-appears if set back to "password") — no
        # restart needed.
        self._default_password = new_password == "password"

    # ------------------------------------------------------------------
    # HTTP handling (via the websockets process_request hook)
    # ------------------------------------------------------------------

    @staticmethod
    def _json(status: int, payload: Any, *, set_cookie: str | None = None) -> Response:
        headers = Headers()
        headers["Content-Type"] = "application/json"
        if set_cookie is not None:
            headers["Set-Cookie"] = set_cookie
        return Response(
            status, "OK" if status == 200 else "ERR", headers, json.dumps(payload).encode()
        )

    @staticmethod
    def _static(path: str) -> Response:
        name = "index.html" if path in ("/", "/dashboard", "/dashboard/") else path.lstrip("/")
        target = (_STATIC_DIR / name).resolve()
        # Contain within the static dir (no path traversal).
        if _STATIC_DIR.resolve() not in target.parents or not target.is_file():
            headers = Headers()
            headers["Content-Type"] = "text/plain"
            return Response(404, "Not Found", headers, b"not found")
        headers = Headers()
        headers["Content-Type"] = _CONTENT_TYPES.get(target.suffix, "application/octet-stream")
        return Response(200, "OK", headers, target.read_bytes())

    async def process_request(
        self, connection: ServerConnection, request: Request
    ) -> Response | None:
        path = request.path.split("?", 1)[0]

        if path == "/ws":
            # Live update + mutation channel. Reject cross-site / rebinding handshakes
            # (F-6), then require auth, then allow the WS upgrade.
            if not self._origin_host_ok(request):
                return self._json(403, {"error": "bad origin/host"})
            if not self._authorized_mutation(request):
                return self._json(401, {"error": "auth required"})
            return None

        if path == "/api/login":
            return self._login(request)

        if path == "/api/logout":
            return self._logout(request)

        if path == "/api/me":
            user = self._current_user(request)
            return self._json(200, {"username": user, "authenticated": user is not None})

        # Everything else under /api/* requires auth. The open endpoints are above
        # (login/logout/me); static assets fall through below. This protects the
        # read surfaces too — several carry transcripts/logs/topology (F-1).
        if path.startswith("/api/") and not self._authorized_mutation(request):
            return self._json(401, {"error": "auth required"})

        if path == "/api/state":
            return self._json(200, await self._state())

        if path == "/api/settings":
            return self._json(200, self._settings_state())

        if path.startswith("/api/nodes/") and path.endswith("/config"):
            node_id = path[len("/api/nodes/") : -len("/config")]
            try:
                cfg = self._server._effective_node_config(node_id)
                override = self._server.read_node_override(node_id)
            except Exception:
                cfg, override = {}, {}
            session = self._server._nodes.get(node_id)
            return self._json(
                200,
                {
                    "node_id": node_id,
                    # Room is server-owned: fall back to the stored override room
                    # when the node is offline so it's editable before it connects.
                    "room": session.room_id if session else override.get("room_id"),
                    "connected": session is not None,
                    "config": cfg,
                    "override": override,
                    "editable": self._server.allowed_override_keys(),
                    "controls": self._dcfg.controls,
                    # Audio devices the node reported (for the device picker); empty
                    # when offline or not yet probed.
                    "devices": (session.capabilities.get("devices") or []) if session else [],
                },
            )

        if path == "/api/logs":
            return self._json(200, {"logs": self._tail_server_logs(request)})

        if path == "/api/sessions":
            # Recent completed pipelines (most recent first); gated by the logs flag
            # since records carry transcripts.
            sessions = list(reversed(self._sessions)) if self._dcfg.logs else []
            return self._json(200, {"sessions": sessions})

        if path == "/api/server/config":
            return self._json(200, self._server_config_state())

        if path == "/api/skills":
            return self._json(200, await self._skills_state())

        if path == "/api/ha/curation":
            return self._json(200, await self._ha_curation_state())

        if path == "/api/speakers":
            return self._json(200, await self._speakers_state())

        if path == "/api/upgrade":
            return self._json(200, await self._upgrade_state())

        if path.startswith("/api/services/") and path.endswith("/config"):
            name = path[len("/api/services/") : -len("/config")]
            try:
                cfg = self._server._effective_service_config(name)
                override = self._server.read_service_override(name)
            except Exception:
                return self._json(404, {"error": "unknown service"})
            return self._json(
                200,
                {
                    "service": name,
                    "config": cfg,  # effective, secret-stripped
                    "override": override,
                    "reachable": name in self._service_urls,
                    "controls": self._dcfg.controls,
                },
            )

        if path.startswith("/api/services/") and path.endswith("/logs"):
            name = path[len("/api/services/") : -len("/logs")]
            return self._json(200, {"logs": await self._service_logs(name, request)})

        if path.startswith("/api/nodes/") and path.endswith("/logs"):
            node_id = path[len("/api/nodes/") : -len("/logs")]
            return self._json(200, await self._node_logs(node_id, request))

        if path.startswith("/api/"):
            return self._json(404, {"error": "unknown endpoint"})

        return self._static(path)

    # ------------------------------------------------------------------
    # Log viewer (pull-based; only wired when the `logs` sub-flag is on)
    # ------------------------------------------------------------------

    @staticmethod
    def _log_query(request: Request) -> tuple[str, int]:
        q = parse_qs(urlsplit(request.path).query)
        level = q.get("level", [""])[0]
        try:
            limit = int(q.get("limit", ["200"])[0])
        except ValueError:
            limit = 200
        return level, limit

    def _tail_server_logs(self, request: Request) -> list[dict[str, Any]]:
        if not self._dcfg.logs or self._server_logs is None:
            return []
        level, limit = self._log_query(request)
        lv = logging.getLevelNamesMapping().get(level.upper(), 0) if level else 0
        return self._server_logs.tail(lv, limit)

    async def _service_logs(self, name: str, request: Request) -> list[dict[str, Any]]:
        health_url = self._service_urls.get(name)
        if not self._dcfg.logs or not health_url:
            return []
        base = health_url[: -len("/health")]
        import httpx

        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                r = await client.get(
                    f"{base}/logs?{urlsplit(request.path).query}",
                    headers=self._server._service_headers(),
                )
                r.raise_for_status()
            logs = r.json().get("logs", [])
            return logs if isinstance(logs, list) else []
        except Exception:
            return []

    async def _restart_service(self, name: str) -> bool:
        """POST /restart to a backend service so it re-execs and re-pulls config."""
        health_url = self._service_urls.get(name)
        if not health_url:
            return False
        base = health_url[: -len("/health")]
        import httpx

        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.post(f"{base}/restart", headers=self._server._service_headers())
            return r.status_code == 200
        except Exception:
            return False

    async def _upgrade_service(self, name: str, version: str | None) -> tuple[bool, str]:
        """POST /upgrade to a backend service (it pip-upgrades its own extra + re-execs).
        Long timeout — a pip install on a slow host can take minutes."""
        health_url = self._service_urls.get(name)
        if not health_url:
            return False, "service not reachable"
        base = health_url[: -len("/health")]
        import httpx

        try:
            async with httpx.AsyncClient(timeout=900.0) as client:
                r = await client.post(
                    f"{base}/upgrade",
                    json={"version": version},
                    headers=self._server._service_headers(),
                )
                r.raise_for_status()
            data = r.json()
            return bool(data.get("ok")), str(data.get("output", ""))[-800:]
        except Exception as exc:
            return False, str(exc)

    async def _do_service_upgrade(
        self, connection: ServerConnection, name: str, version: str | None
    ) -> None:
        """Run a service upgrade and report progress/result over the WS (the service
        re-execs itself on success)."""

        async def send(payload: dict[str, Any]) -> None:
            try:
                await connection.send(json.dumps(payload))
            except Exception:
                pass

        await send({"type": "upgrade_progress", "stage": "installing", "target": name})
        ok, output = await self._upgrade_service(name, version)
        await send({"type": "upgrade_result", "ok": ok, "output": output, "target": name})

    # ------------------------------------------------------------------
    # Skill registry (kenzy-llm): read the loaded skills + live-toggle disables
    # ------------------------------------------------------------------

    async def _llm_skills_request(
        self, method: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        """GET/POST the LLM service's /skills endpoint; None if unreachable."""
        health_url = self._service_urls.get("llm")
        if not health_url:
            return None
        base = health_url[: -len("/health")]
        import httpx

        try:
            async with httpx.AsyncClient(timeout=4.0) as client:
                if method == "POST":
                    r = await client.post(
                        f"{base}/skills",
                        json=payload or {},
                        headers=self._server._service_headers(),
                    )
                else:
                    r = await client.get(f"{base}/skills", headers=self._server._service_headers())
                r.raise_for_status()
            data = r.json()
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    async def _llm_curation_request(
        self, method: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        """GET/POST the LLM service's /ha/curation endpoint; None if unreachable."""
        health_url = self._service_urls.get("llm")
        if not health_url:
            return None
        base = health_url[: -len("/health")]
        import httpx

        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                if method == "POST":
                    r = await client.post(
                        f"{base}/ha/curation",
                        json=payload or {},
                        headers=self._server._service_headers(),
                    )
                else:
                    r = await client.get(
                        f"{base}/ha/curation", headers=self._server._service_headers()
                    )
                r.raise_for_status()
            data = r.json()
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    async def _ha_curation_state(self) -> dict[str, Any]:
        info = await self._llm_curation_request("GET")
        return {
            "reachable": info is not None,
            "controls": self._dcfg.controls,
            "curation": (info or {}).get("curation", {}),
            "devices": (info or {}).get("devices", []),
            "ha_reachable": bool((info or {}).get("reachable", False)),
        }

    async def _set_ha_curation(self, curation: dict[str, Any]) -> tuple[bool, str | None]:
        """Persist the curation document via the LLM service."""
        res = await self._llm_curation_request("POST", {"curation": curation})
        if res is None:
            return False, "LLM service not reachable"
        if not res.get("ok"):
            return False, res.get("error") or "could not save curation"
        return True, None

    async def _skills_state(self) -> dict[str, Any]:
        info = await self._llm_skills_request("GET")
        return {
            "reachable": info is not None,
            "controls": self._dcfg.controls,
            "skills": (info or {}).get("skills", []),
            "fast_intents": (info or {}).get("fast_intents", []),
        }

    async def _set_skill_disabled(self, name: str, disabled: bool) -> tuple[bool, str | None]:
        """Toggle one skill: persist to the llm override and live-apply (no restart)."""
        info = await self._llm_skills_request("GET")
        if info is None:
            return False, "LLM service not reachable"
        current = {
            s["name"]
            for s in info.get("skills", []) + info.get("fast_intents", [])
            if s.get("disabled")
        }
        if disabled:
            current.add(name)
        else:
            current.discard(name)
        new_list = sorted(current)
        # Persist into configs/services/llm.yaml (survives restart) without clobbering
        # other override keys, then live-apply via the LLM's /skills endpoint.
        override = self._server.read_service_override("llm")
        override.setdefault("skills", {})["disabled"] = new_list
        try:
            self._server.write_service_override("llm", override)
        except (ValueError, OSError) as exc:
            return False, str(exc)
        applied = await self._llm_skills_request("POST", {"disabled": new_list})
        if applied is None:
            return False, "saved, but live-apply failed (will take effect on restart)"
        return True, None

    # ------------------------------------------------------------------
    # Update check (visibility layer for the upgrade feature)
    # ------------------------------------------------------------------

    async def _latest_pypi_version(self) -> str | None:
        """Latest kenzy version on PyPI, cached ~1 h. None if unreachable (offline /
        air-gapped degrade gracefully). Checked lazily, only when the UI asks."""
        import time

        if self._pypi_cache and time.monotonic() - self._pypi_cache[0] < 3600:
            return self._pypi_cache[1]
        import httpx

        latest: str | None = None
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.get("https://pypi.org/pypi/kenzy/json")
                r.raise_for_status()
            info = r.json().get("info") or {}
            latest = str(info["version"]) if info.get("version") else None
        except Exception:
            latest = None
        self._pypi_cache = (time.monotonic(), latest)
        return latest

    async def _do_server_upgrade(self, connection: ServerConnection, version: str | None) -> None:
        """Run the server self-upgrade, push the result, and re-exec on success."""
        async def send(payload: dict[str, Any]) -> None:
            try:
                await connection.send(json.dumps(payload))
            except Exception:
                pass

        await send({"type": "upgrade_progress", "stage": "installing"})
        ok, output = await self._server.run_self_upgrade("server", version)
        await send({"type": "upgrade_result", "ok": ok, "output": output})
        if ok:
            # Re-exec so the new code loads; the WS drops and the SPA reconnects.
            await send({"type": "server_restarting"})
            asyncio.get_running_loop().call_later(0.8, self._server.restart_server)

    async def _upgrade_state(self) -> dict[str, Any]:
        current = kenzy_version()
        latest = await self._latest_pypi_version()
        # A dev/editable checkout has no comparable version — never flag it.
        update = bool(latest and current != "dev" and _is_newer(latest, current))
        return {
            "current": current,
            "latest": latest,
            "update_available": update,
            "checkable": latest is not None,
            "controls": self._dcfg.controls,
        }

    # ------------------------------------------------------------------
    # Speaker profiles (kenzy-speaker): manage the enrolled list
    # ------------------------------------------------------------------

    async def _speaker_request(
        self, method: str, sub_path: str, payload: dict[str, Any] | None = None
    ) -> tuple[int, Any] | None:
        """Proxy a request to the speaker service; (status, json) or None if unreachable."""
        health_url = self._service_urls.get("speaker")
        if not health_url:
            return None
        base = health_url[: -len("/health")]
        import httpx

        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                r = await client.request(
                    method,
                    f"{base}{sub_path}",
                    json=payload,
                    headers=self._server._service_headers(),
                )
            try:
                body = r.json()
            except Exception:
                body = None
            return r.status_code, body
        except Exception:
            return None

    async def _speakers_state(self) -> dict[str, Any]:
        res = await self._speaker_request("GET", "/speakers")
        speakers = []
        if res and res[0] == 200 and isinstance(res[1], dict):
            speakers = res[1].get("speakers", [])
        try:
            threshold = float(
                self._server._effective_service_config("speaker").get("identify_threshold", 0.25)
            )
        except Exception:
            threshold = 0.25
        rooms = [
            {"node_id": nid, "room": sess.room_id} for nid, sess in self._server._nodes.items()
        ]
        return {
            "reachable": res is not None,
            "controls": self._dcfg.controls,
            "identify_threshold": threshold,
            "speakers": speakers,
            "rooms": rooms,
        }

    async def _delete_speaker(self, name: str) -> tuple[bool, str | None]:
        res = await self._speaker_request("DELETE", f"/speakers/{quote(name, safe='')}")
        if res is None:
            return False, "speaker service not reachable"
        if res[0] == 200:
            return True, None
        detail = res[1].get("detail") if isinstance(res[1], dict) else None
        return False, detail or f"delete failed ({res[0]})"

    async def _rename_speaker(self, name: str, new_name: str) -> tuple[bool, str | None]:
        res = await self._speaker_request(
            "POST", f"/speakers/{quote(name, safe='')}/rename", {"new_name": new_name}
        )
        if res is None:
            return False, "speaker service not reachable"
        if res[0] == 200:
            return True, None
        detail = res[1].get("detail") if isinstance(res[1], dict) else None
        return False, detail or f"rename failed ({res[0]})"

    async def _node_logs(self, node_id: str, request: Request) -> dict[str, Any]:
        if not self._dcfg.logs:
            return {"logs": [], "reachable": False}
        level, limit = self._log_query(request)
        entries = await self._server.request_node_logs(node_id, level, limit)
        return {"logs": entries or [], "reachable": entries is not None}

    # ------------------------------------------------------------------
    # Live update channel (authed WS): push fleet state on every change
    # ------------------------------------------------------------------

    def _on_state_change(self) -> None:
        """Server registry/state changed — fan a fresh snapshot to WS clients."""
        if self._clients:
            asyncio.create_task(self._broadcast_state())

    async def _broadcast_state(self) -> None:
        payload = json.dumps({"type": "state", "data": await self._state()})
        for ws in list(self._clients):
            try:
                await ws.send(payload)
            except Exception:
                self._clients.discard(ws)

    def _on_tune_sample(self, node_id: str, sample: dict[str, Any]) -> None:
        """Relay one calibration sample to the client(s) tuning this node only."""
        targets = [c for c, n in self._tune_subs.items() if n == node_id]
        if not targets:
            return
        payload = json.dumps({"type": "tune", "node": node_id, "sample": sample})
        asyncio.create_task(self._send_tune(targets, payload))

    async def _send_tune(self, targets: list[ServerConnection], payload: str) -> None:
        for ws in targets:
            try:
                await ws.send(payload)
            except Exception:
                self._tune_subs.pop(ws, None)

    def _on_session(self, record: dict[str, Any]) -> None:
        """Store a completed-pipeline record and push it to connected browsers."""
        self._sessions.append(record)
        if self._clients:
            payload = json.dumps({"type": "session", "data": record})
            asyncio.create_task(self._broadcast_raw(payload))

    async def _broadcast_raw(self, payload: str) -> None:
        for ws in list(self._clients):
            try:
                await ws.send(payload)
            except Exception:
                self._clients.discard(ws)

    async def _ws_handler(self, connection: ServerConnection) -> None:
        # Auth was checked in process_request before the upgrade was allowed.
        self._clients.add(connection)
        try:
            await connection.send(json.dumps({"type": "state", "data": await self._state()}))
            async for raw in connection:
                await self._handle_ws_message(connection, raw)
        except Exception:
            pass
        finally:
            self._clients.discard(connection)
            # If this client was calibrating a node, stop it (unless another client
            # is still tuning the same node).
            node = self._tune_subs.pop(connection, "")
            if node and node not in self._tune_subs.values():
                asyncio.create_task(self._server.stop_node_tuning(node))

    async def _handle_ws_message(self, connection: ServerConnection, raw: str | bytes) -> None:
        try:
            msg = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return
        mid = msg.get("id")
        mtype = msg.get("type")

        async def ack(ok: bool, error: str | None = None) -> None:
            payload: dict[str, Any] = {"type": "ack", "id": mid, "ok": ok}
            if error:
                payload["error"] = error
            await connection.send(json.dumps(payload))

        # All mutations are gated behind the `controls` sub-flag.
        if mtype == "set_override":
            if not self._dcfg.controls:
                return await ack(False, "controls are disabled (set dashboard.controls: true)")
            node = str(msg.get("node", ""))
            try:
                self._server.write_node_override(node, msg.get("config") or {})
            except (ValueError, OSError) as exc:
                return await ack(False, str(exc))
            applied = await self._server.push_config(node)  # live re-push if connected
            await ack(True)
            await self._broadcast_state()
            await connection.send(
                json.dumps({"type": "override_saved", "node": node, "applied_live": applied})
            )
        elif mtype == "set_room":
            if not self._dcfg.controls:
                return await ack(False, "controls are disabled (set dashboard.controls: true)")
            try:
                ok = await self._server.set_room(str(msg.get("node", "")), str(msg.get("name", "")))
            except ValueError as exc:
                return await ack(False, str(exc))
            await ack(ok, None if ok else "node not connected")
            if ok:
                await self._broadcast_state()
        elif mtype in ("trigger", "stop", "restart"):
            if not self._dcfg.controls:
                return await ack(False, "controls are disabled (set dashboard.controls: true)")
            node = str(msg.get("node", ""))
            action = {
                "trigger": self._server.trigger_node,
                "stop": self._server.stop_node,
                "restart": self._server.restart_node,
            }[mtype]
            ok = await action(node)
            await ack(ok, None if ok else "node not connected")
        elif mtype == "set_muted":
            if not self._dcfg.controls:
                return await ack(False, "controls are disabled (set dashboard.controls: true)")
            ok = await self._server.set_node_muted(str(msg.get("node", "")), bool(msg.get("muted")))
            await ack(ok, None if ok else "node not connected")
            if ok:
                await self._broadcast_state()
        elif mtype == "tune_start":
            if not self._dcfg.controls:
                return await ack(False, "controls are disabled (set dashboard.controls: true)")
            node = str(msg.get("node", ""))
            try:
                seconds = float(msg.get("seconds", 20.0))
            except (TypeError, ValueError):
                seconds = 20.0
            ok = await self._server.start_node_tuning(node, seconds)
            if ok:
                self._tune_subs[connection] = node
            await ack(ok, None if ok else "node not connected")
        elif mtype == "tune_stop":
            node = self._tune_subs.pop(connection, "") or str(msg.get("node", ""))
            if node and node not in self._tune_subs.values():
                await self._server.stop_node_tuning(node)
            await ack(True)
        elif mtype == "announce":
            if not self._dcfg.controls:
                return await ack(False, "controls are disabled (set dashboard.controls: true)")
            text = str(msg.get("text", "")).strip()
            if not text:
                return await ack(False, "announcement text is empty")
            count = await self._server.announce(text, msg.get("rooms"))
            if count:
                await ack(True)
            else:
                await ack(False, "no nodes reachable or TTS not configured")
            await connection.send(json.dumps({"type": "announce_result", "count": count}))
        elif mtype == "set_service_config":
            if not self._dcfg.controls:
                return await ack(False, "controls are disabled (set dashboard.controls: true)")
            service = str(msg.get("service", ""))
            try:
                self._server.write_service_override(service, msg.get("config") or {})
            except (ValueError, OSError) as exc:
                return await ack(False, str(exc))
            restarted = await self._restart_service(service)
            await ack(True)
            await connection.send(
                json.dumps({"type": "service_saved", "service": service, "restarted": restarted})
            )
        elif mtype == "restart_service":
            if not self._dcfg.controls:
                return await ack(False, "controls are disabled (set dashboard.controls: true)")
            ok = await self._restart_service(str(msg.get("service", "")))
            await ack(ok, None if ok else "service not reachable")
        elif mtype == "upgrade_server":
            if not self._dcfg.controls:
                return await ack(False, "controls are disabled (set dashboard.controls: true)")
            version = (str(msg.get("version") or "")).strip() or None
            # Accept now; the pip run can take minutes (longer than the request/ack
            # timeout), so progress + result ride separate events and, on success, the
            # server re-execs (the WS drops and the SPA reconnects to the new version).
            await ack(True)
            asyncio.create_task(self._do_server_upgrade(connection, version))
        elif mtype == "upgrade_service":
            if not self._dcfg.controls:
                return await ack(False, "controls are disabled (set dashboard.controls: true)")
            service = str(msg.get("service", "")).strip()
            if service not in ("stt", "tts", "llm", "speaker"):
                return await ack(False, "unknown service")
            version = (str(msg.get("version") or "")).strip() or None
            await ack(True)
            asyncio.create_task(self._do_service_upgrade(connection, service, version))
        elif mtype == "upgrade_node":
            if not self._dcfg.controls:
                return await ack(False, "controls are disabled (set dashboard.controls: true)")
            version = (str(msg.get("version") or "")).strip() or None
            # Fire-and-watch: the node installs + re-execs, reconnecting with its new
            # version (visible in the fleet view). No progress stream from the node.
            ok = await self._server.upgrade_node(str(msg.get("node", "")), version)
            await ack(ok, None if ok else "node not connected")
        elif mtype == "set_skill_disabled":
            if not self._dcfg.controls:
                return await ack(False, "controls are disabled (set dashboard.controls: true)")
            name = str(msg.get("name", "")).strip()
            if not name:
                return await ack(False, "skill name is required")
            ok, err = await self._set_skill_disabled(name, bool(msg.get("disabled")))
            await ack(ok, err)
        elif mtype == "set_ha_curation":
            if not self._dcfg.controls:
                return await ack(False, "controls are disabled (set dashboard.controls: true)")
            curation = msg.get("curation")
            if not isinstance(curation, dict):
                return await ack(False, "curation document is required")
            ok, err = await self._set_ha_curation(curation)
            await ack(ok, err)
        elif mtype == "delete_speaker":
            if not self._dcfg.controls:
                return await ack(False, "controls are disabled (set dashboard.controls: true)")
            name = str(msg.get("name", "")).strip()
            if not name:
                return await ack(False, "speaker name is required")
            ok, err = await self._delete_speaker(name)
            await ack(ok, err)
            if ok:
                await connection.send(json.dumps({"type": "speakers_changed"}))
        elif mtype == "rename_speaker":
            if not self._dcfg.controls:
                return await ack(False, "controls are disabled (set dashboard.controls: true)")
            name = str(msg.get("name", "")).strip()
            new_name = str(msg.get("new_name", "")).strip()
            if not name or not new_name:
                return await ack(False, "both name and new_name are required")
            ok, err = await self._rename_speaker(name, new_name)
            await ack(ok, err)
            if ok:
                await connection.send(json.dumps({"type": "speakers_changed"}))
        elif mtype == "enroll_speaker":
            if not self._dcfg.controls:
                return await ack(False, "controls are disabled (set dashboard.controls: true)")
            from kenzy.server.server import TranscribingServer

            name = str(msg.get("name", "")).strip()
            node_id = str(msg.get("node", "")).strip()
            server = self._server
            sess = server._nodes.get(node_id)
            if not name:
                return await ack(False, "speaker name is required")
            if sess is None:
                return await ack(False, "pick a connected room node to enroll from")
            if not isinstance(server, TranscribingServer):
                return await ack(False, "enrollment is not available on this server")
            await server.start_enrollment(node_id, sess.room_id, name, operator=True)
            await ack(True)
        elif mtype == "boost_trace":
            if not self._dcfg.controls:
                return await ack(False, "controls are disabled (set dashboard.controls: true)")
            try:
                seconds = int(msg.get("seconds", 30))
            except (TypeError, ValueError):
                seconds = 30
            ok = await self._server.boost_node_trace(str(msg.get("node", "")), seconds)
            await ack(ok, None if ok else "node not connected")
        elif mtype == "set_password":
            # Account self-service — allowed for any signed-in user (not gated by
            # `controls`), but the current password must be re-supplied.
            current = str(msg.get("current", ""))
            new = str(msg.get("new", ""))
            if len(new) < 4:
                return await ack(False, "new password must be at least 4 characters")
            if self._dcfg.auth_password_hash is None or not serviceauth.verify_password(
                current, self._dcfg.auth_password_hash
            ):
                return await ack(False, "current password is incorrect")
            try:
                self._set_password(new)
            except (ValueError, OSError) as exc:
                return await ack(False, str(exc))
            await ack(True)
        elif mtype == "set_server_config":
            # Admin-level (any signed-in user, like set_password) — not gated by
            # `controls`, so logs/controls themselves can be enabled from a fresh
            # dashboard. Writes the safe-key override, then re-execs the server.
            try:
                self._write_server_override(msg.get("config") or {})
            except (ValueError, OSError) as exc:
                return await ack(False, str(exc))
            await ack(True)
            await connection.send(json.dumps({"type": "server_restarting"}))
            # Let the ack flush, then re-exec the server to apply (it re-reads config).
            asyncio.get_running_loop().call_later(0.5, self._server.restart_server)
        else:
            await ack(False, f"unknown message type: {mtype!r}")

    async def serve(self) -> None:
        log.info("Dashboard on http://%s:%d/dashboard", self._dcfg.bind, self._dcfg.port)
        if self._dcfg.auth_username is None and self._dcfg.auth_token is None:
            log.warning("Dashboard has no credentials configured — mutations disabled.")
        async with websockets.serve(
            self._ws_handler,
            self._dcfg.bind,
            self._dcfg.port,
            process_request=self.process_request,
            max_size=262_144,  # mutations are small JSON; bound inbound frame size (F-10)
        ):
            await asyncio.Future()
