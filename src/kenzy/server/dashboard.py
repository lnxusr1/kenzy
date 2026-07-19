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

from kenzy import kenzy_version, serviceauth, tlsutil
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
    ".svg": "image/svg+xml",
}

_BRAND_PETROL = b"#013249"
_BRAND_GOLD = b"#ffb500"

# Corner badge for the experimental favicon: the "something's different here" dot.
# The gold under-stroke (paint-order) is invisible against the tile but carves a
# separating gap where the dot overlaps the K's leg, so it reads as "K." not a blob.
_EXPERIMENTAL_BADGE = (
    b'<circle cx="25" cy="25" r="5" fill="#013249" stroke="#ffb500"'
    b' stroke-width="2.2" paint-order="stroke"/>'
)


def _experimental_favicon(svg: bytes) -> bytes:
    """Experimental-mode favicon: brand colors swapped (gold tile, petrol K) plus a
    corner badge dot, so the tab is tellable from production at a glance."""
    swapped = (
        svg.replace(_BRAND_PETROL, b"\x00")
        .replace(_BRAND_GOLD, _BRAND_PETROL)
        .replace(b"\x00", _BRAND_GOLD)
    )
    return swapped.replace(b"</svg>", _EXPERIMENTAL_BADGE + b"</svg>")


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


def _dotted_del(d: dict[str, Any], key: str) -> None:
    """Delete the value at a dotted path (no-op if absent), pruning parent dicts
    that become empty so the override file stays minimal."""
    parts = key.split(".")
    chain: list[dict[str, Any]] = [d]
    for part in parts[:-1]:
        nxt = chain[-1].get(part)
        if not isinstance(nxt, dict):
            return
        chain.append(nxt)
    chain[-1].pop(parts[-1], None)
    for i in range(len(chain) - 1, 0, -1):
        if not chain[i]:
            chain[i - 1].pop(parts[i - 1], None)


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
        # `experimental` opts this server into features not yet ready to ship
        # officially. Today it drives only the favicon color swap below, so an
        # experimental instance's tab is tellable from production at a glance.
        self._experimental = bool(cfg.get("experimental", False))
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
        # TLS: the dashboard terminates the same cert pair as the node WS port
        # (`tls: {cert, key}` in server.yaml). Browsers see https/wss; with a
        # self-signed cert they show a one-time interstitial (or install the CA).
        self._ssl: Any = None
        tls_cfg = cfg.get("tls") or {}
        if isinstance(tls_cfg, dict) and tls_cfg.get("cert") and tls_cfg.get("key"):
            from kenzy import tlsutil

            try:
                self._ssl = tlsutil.server_context(str(tls_cfg["cert"]), str(tls_cfg["key"]))
                log.info("TLS enabled on the dashboard (https)")
            except Exception as exc:
                log.error("Dashboard TLS config invalid (%s) — continuing WITHOUT TLS", exc)
        server.add_state_listener(self._on_state_change)
        # Calibration: which connected browser is tuning which node (connection → node_id).
        # Tune samples are relayed only to the subscribed client, not all clients, and
        # never through the heavy state snapshot.
        self._tune_subs: dict[ServerConnection, str] = {}
        server.add_tune_listener(self._on_tune_sample)
        # Guided-calibration progress events (low-rate) — pushed to every authed
        # client, so the dashboard can watch voice-initiated runs too.
        server.add_calib_listener(self._on_calib_event)
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
        # Live-push for the Scheduled view: any schedule change (set by voice,
        # fired, cancelled) pokes connected browsers to re-fetch /api/schedules.
        server.add_schedule_listener(self._on_schedules_change)
        server.add_metrics_listener(self._on_state_change)  # refresh cards on metrics ticks

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

    def _cookie_header(self, value: str, request: Request, *, max_age: int) -> str:
        """Build the session cookie. Adds ``Secure`` when the request arrived over TLS
        (directly or via a reverse proxy that forwards the scheme), so the cookie isn't
        sent in cleartext once HTTPS is in front (F-7). Plaintext stays the default."""
        secure = (
            self._ssl is not None or request.headers.get("X-Forwarded-Proto", "").lower() == "https"
        )
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
                    # Declared hardware capability: false ⇒ half-duplex room
                    # (no voice-interrupt during playback; intercom/alarms off).
                    "aec": self._server._node_aec(node_id),
                    # Latest node system metrics (cpu/ram/disk %, temp °C).
                    "metrics": session.metrics,
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
                async with httpx.AsyncClient(timeout=2.0, verify=tlsutil.httpx_verify()) as client:
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
                # Gate the HA nav tab: no-HA households see no HA surfaces.
                "ha_active": (await self._ha_flags())["active"],
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
        """Fields for the Settings editor, node-editor style: ``value`` is ONLY what
        the override layer (server.local.yaml) holds; ``inherited`` is what applies
        when it's unset (the hand-edited server.yaml value). The UI fills the input
        from ``value`` and shows ``inherited`` as the placeholder."""
        import yaml

        from kenzy.server.server import _SERVER_EDITABLE

        override = self._read_server_override()
        base: dict[str, Any] = {}
        if self._config_path is not None:
            try:
                loaded = yaml.safe_load(self._config_path.read_text()) or {}
                if isinstance(loaded, dict):
                    base = loaded
            except Exception:
                pass
        fields = []
        for key, typ in _SERVER_EDITABLE.items():
            ov = _dotted_get(override, key)
            inherited = _dotted_get(base, key)
            fields.append(
                {
                    "key": key,
                    "type": typ,
                    "value": None if ov is _MISSING else ov,
                    "inherited": None if inherited is _MISSING else inherited,
                    "overridden": ov is not _MISSING,
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
            if raw is None:  # null ⇒ unset: remove from the override, revert to inherited
                _dotted_del(override, key)
                continue
            try:
                _dotted_set(override, key, _coerce_server_value(raw, _SERVER_EDITABLE[key]))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"invalid value for {key}: {exc}") from exc
        path = _server_override_path(self._config_path)
        if override:
            path.write_text(yaml.safe_dump(override, default_flow_style=False, sort_keys=True))
        elif path.is_file():  # last key unset — drop the file, not an empty stub
            path.unlink()
        log.info("Wrote server override %s (%d key(s))", path, len(patch))

    def _settings_state(self) -> dict[str, Any]:
        """Read-only server/dashboard info shown on the Settings page."""
        from kenzy import installed_version

        version = kenzy_version()
        return {
            "version": version,
            # On-disk version; differs from `version` (the running code) only when
            # the package was upgraded under the live server — restart to apply.
            "installed": installed_version(),
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
            # Names (never values) of the env secrets currently set in the config
            # home's .env, for the write-only API-keys editor's set/not-set badges.
            "env_keys": self._env_key_names(),
        }

    @staticmethod
    def _env_key_names() -> list[str]:
        """UPPER_SNAKE names with non-empty values in the config home's .env."""
        from kenzy.config import kenzy_data_root

        try:
            text = (kenzy_data_root() / ".env").read_text()
        except OSError:
            return []
        names: set[str] = set()
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, _, value = line.partition("=")
            name = name.removeprefix("export ").strip()
            if (
                name
                and name == name.upper()
                and name.replace("_", "").isalnum()
                and value.strip().strip("\"'")
            ):
                names.add(name)
        return sorted(names)

    def _set_password(self, new_password: str) -> None:
        """Persist a new dashboard password and apply it live.

        Writes ``dashboard.auth`` to the ``server.local.yaml`` override layer
        (via :func:`kenzy.passwd.set_auth`) — NOT server.yaml, which a
        kenzy-deploy upgrade sync overwrites (silently reverting the login) and
        which may be the read-only packaged default. The in-memory hash + cookie
        secret update immediately — no restart. Because the signing secret
        changes, existing sessions are invalidated and must sign in again.
        """
        if self._config_path is None or not self._config_path.is_file():
            raise OSError("server.yaml not found — cannot persist the password")
        from kenzy.passwd import set_auth
        from kenzy.serviceauth import hash_password

        username = self._dcfg.auth_username or "admin"
        new_hash = hash_password(new_password)
        set_auth(self._config_path, username, new_hash)
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

    def _static(self, path: str) -> Response:
        name = "index.html" if path in ("/", "/dashboard", "/dashboard/") else path.lstrip("/")
        target = (_STATIC_DIR / name).resolve()
        # Contain within the static dir (no path traversal).
        if _STATIC_DIR.resolve() not in target.parents or not target.is_file():
            headers = Headers()
            headers["Content-Type"] = "text/plain"
            return Response(404, "Not Found", headers, b"not found")
        headers = Headers()
        headers["Content-Type"] = _CONTENT_TYPES.get(target.suffix, "application/octet-stream")
        body = target.read_bytes()
        if name == "favicon.svg" and self._experimental:
            body = _experimental_favicon(body)
        return Response(200, "OK", headers, body)

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

        if path == "/api/people":
            return self._json(200, await self._people_state())

        if path == "/api/memory":
            return self._json(200, await self._memory_state())

        if path.startswith("/api/people/") and path.endswith("/export"):
            pid = path[len("/api/people/") : -len("/export")]
            eqs = parse_qs(urlsplit(request.path).query or "")
            # Plaintext secret values ride only when the dashboard has controls
            # (same trust level as the Reveal button) — a read-only dashboard
            # exports the count line instead.
            inc_secrets = (eqs.get("secrets") or ["1"])[0] not in ("0", "false")
            inc_secrets = inc_secrets and self._dcfg.controls
            body = await self._person_export(pid, include_secrets=inc_secrets)
            if body is None:
                return self._json(404, {"error": "no such person"})
            import json as _json_mod

            return Response(
                200,
                "OK",
                Headers(
                    {
                        "Content-Type": "application/json",
                        "Content-Disposition": f'attachment; filename="kenzy-{pid}-export.json"',
                        "Cache-Control": "no-store",
                    }
                ),
                _json_mod.dumps(body, indent=2, ensure_ascii=False).encode(),
            )

        if path == "/api/backup":
            # Downloadable backup: the local config home merged with the stateful
            # services' slices (complete even multi-host). By default .env/API keys
            # and models/ stay out; ?secrets=1 and ?full=1 opt them in (the archive
            # then carries live credentials / model bulk — the UI says so).
            # Auth-gated like every /api read; restore is the kenzy-init CLI
            # (this HTTP hook exposes no request body, so upload isn't a thing).
            import time as _time

            qs = parse_qs(urlsplit(request.path).query)

            def _flag(key: str) -> bool:
                return (qs.get(key) or ["0"])[0].lower() in ("1", "true", "yes")

            data = await self._server.create_backup_archive(
                include_secrets=_flag("secrets") and self._dcfg.controls,
                include_models=_flag("full"),
                # The lockbox KEY rides by default (a backup restores everything);
                # ?lockbox_key=0 builds a shareable archive (ciphertext only). A
                # read-only dashboard (controls: false) gets ciphertext-only too —
                # the key is the same trust level as Reveal.
                include_lockbox_key=(
                    (qs.get("lockbox_key") or ["1"])[0] not in ("0", "false")
                    and self._dcfg.controls
                ),
            )
            headers = Headers()
            headers["Content-Type"] = "application/gzip"
            stamp = _time.strftime("%Y%m%d-%H%M%S")
            headers["Content-Disposition"] = f'attachment; filename="kenzy-backup-{stamp}.tar.gz"'
            return Response(200, "OK", headers, data)

        if path == "/api/schedules":
            # Active timers/alarms/reminders (deliberately auth-only, not gated by
            # `logs`: these are future announcements the operator manages, and you
            # can't cancel what you can't see).
            return self._json(
                200,
                {"schedules": self._server.list_schedules(), "controls": self._dcfg.controls},
            )

        if path == "/api/upgrade":
            return self._json(200, await self._upgrade_state())

        if path.startswith("/api/services/") and path.endswith("/config"):
            name = path[len("/api/services/") : -len("/config")]
            try:
                cfg = self._server._effective_service_config(name)
                defaults = self._server._effective_service_config(name, include_override=False)
                override = self._server.read_service_override(name)
            except Exception:
                return self._json(404, {"error": "unknown service"})
            return self._json(
                200,
                {
                    "service": name,
                    "config": cfg,  # effective, secret-stripped
                    "defaults": defaults,  # inherited layer only (field placeholders)
                    "override": override,
                    "reachable": self._service_base(name) is not None,
                    "controls": self._dcfg.controls,
                },
            )

        if path.startswith("/api/secrets/") and path.endswith("/reveal"):
            if not self._dcfg.controls:
                return self._json(403, {"error": "controls are disabled"})
            sid = path[len("/api/secrets/") : -len("/reveal")]
            res = await self._llm_memory_request("GET", f"/lockbox/reveal?id={quote(sid)}")
            if res is None:
                return self._json(404, {"error": "no such secret (or lockbox unreachable)"})
            return self._json(200, res)

        if path.startswith("/api/services/") and path.endswith("/unit"):
            name = path[len("/api/services/") : -len("/unit")]
            return self._json(200, await self._service_unit(name))

        if path.startswith("/api/services/") and path.endswith("/features"):
            name = path[len("/api/services/") : -len("/features")]
            return self._json(200, await self._service_features(name))

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
        # _service_base, never _service_urls: the static URL stays http:// when
        # mesh TLS made the service https-only (the 3.11 proxy regression class),
        # and a purely auto-registered service isn't in the static map at all.
        base = self._service_base(name)
        if not self._dcfg.logs or not base:
            return []
        import httpx

        try:
            async with httpx.AsyncClient(timeout=3.0, verify=tlsutil.httpx_verify()) as client:
                url = f"{base}/logs?{urlsplit(request.path).query}"
                r = await client.get(url, headers=self._server._service_headers("GET", url))
                r.raise_for_status()
            logs = r.json().get("logs", [])
            return logs if isinstance(logs, list) else []
        except Exception:
            return []

    def _service_base(self, name: str) -> str | None:
        """Base URL for a backend service — statically configured or auto-registered."""
        targets = {**self._service_urls, **self._server.announced_health_urls()}
        url = targets.get(name)
        return url[: -len("/health")] if url else None

    async def _service_features(self, name: str) -> dict[str, Any]:
        """Proxy a service's GET /features (the 4.1 chips). Unreachable ⇒
        reachable:false so the editor renders an honest empty state."""
        base = self._service_base(name)
        if not base:
            return {"reachable": False, "features": []}
        import httpx

        try:
            async with httpx.AsyncClient(timeout=5.0, verify=tlsutil.httpx_verify()) as client:
                url = f"{base}/features"
                r = await client.get(url, headers=self._server._service_headers("GET", url))
                r.raise_for_status()
            data = r.json()
            feats = data.get("features", []) if isinstance(data, dict) else []
            return {"reachable": True, "features": feats if isinstance(feats, list) else []}
        except Exception:
            return {"reachable": False, "features": []}

    async def _service_unit(self, name: str) -> dict[str, Any]:
        """A service's systemd --user unit state: ask the service itself when
        reachable; fall back to querying LOCALLY (covers co-located STOPPED
        services — the case the service can't answer for itself)."""
        base = self._service_base(name)
        if base:
            import httpx

            try:
                async with httpx.AsyncClient(timeout=4.0, verify=tlsutil.httpx_verify()) as client:
                    url = f"{base}/unit"
                    r = await client.get(url, headers=self._server._service_headers("GET", url))
                    r.raise_for_status()
                    data = r.json()
                    if isinstance(data, dict):
                        return {**data, "via": "service"}
            except Exception:
                pass
        from kenzy.unitctl import unit_state

        state = await asyncio.to_thread(unit_state, f"kenzy-{name}.service")
        return {"unit": f"kenzy-{name}.service", **state, "via": "local"}

    async def _set_service_enabled(self, name: str, enabled: bool) -> tuple[bool, str | None]:
        """Disable: the service self-disables via its /unit endpoint (works on
        any host), local fallback. Enable: LOCAL only — a stopped remote
        service has no endpoint; the operator runs
        `systemctl --user enable --now kenzy-<svc>` on that host (documented)."""
        from kenzy.unitctl import disable_unit, enable_unit, unit_state

        unit = f"kenzy-{name}.service"
        if enabled:
            state = await asyncio.to_thread(unit_state, unit)
            if not state.get("systemd") or not state.get("exists"):
                return False, (
                    "not manageable from here — if the service runs on another host, "
                    f"run there: systemctl --user enable --now {unit}"
                )
            ok, out = await asyncio.to_thread(enable_unit, unit)
            return ok, None if ok else out
        base = self._service_base(name)
        if base:
            import httpx

            try:
                async with httpx.AsyncClient(timeout=6.0, verify=tlsutil.httpx_verify()) as client:
                    url = f"{base}/unit"
                    r = await client.post(
                        url,
                        json={"action": "disable"},
                        headers=self._server._service_headers("POST", url),
                    )
                    r.raise_for_status()
                    if r.json().get("ok"):
                        return True, None
            except Exception:
                pass
        state = await asyncio.to_thread(unit_state, unit)
        if state.get("systemd") and state.get("exists"):
            ok, out = await asyncio.to_thread(disable_unit, unit)
            return ok, None if ok else out
        return False, "service unreachable and no local unit — nothing to disable"

    async def _install_feature_deps(self, name: str) -> tuple[bool, str | None]:
        """POST /install_deps — the chips' Install action. The pip run can take
        a couple of minutes; the service re-execs itself on success."""
        base = self._service_base(name)
        if not base:
            return False, "service not reachable"
        import httpx

        try:
            async with httpx.AsyncClient(timeout=600.0, verify=tlsutil.httpx_verify()) as client:
                url = f"{base}/install_deps"
                r = await client.post(url, headers=self._server._service_headers("POST", url))
                r.raise_for_status()
                body = r.json()
            if body.get("ok"):
                return True, None
            return False, str(body.get("output", "install failed"))[-300:]
        except Exception as exc:
            return False, f"install failed: {exc}"

    async def _restart_service(self, name: str) -> bool:
        """POST /restart to a backend service so it re-execs and re-pulls config."""
        base = self._service_base(name)
        if not base:
            return False
        import httpx

        try:
            async with httpx.AsyncClient(timeout=5.0, verify=tlsutil.httpx_verify()) as client:
                r = await client.post(
                    f"{base}/restart",
                    headers=self._server._service_headers("POST", f"{base}/restart"),
                )
            return r.status_code == 200
        except Exception:
            return False

    async def _service_health(self, base: str) -> dict[str, Any]:
        import httpx

        try:
            async with httpx.AsyncClient(timeout=3.0, verify=tlsutil.httpx_verify()) as client:
                r = await client.get(f"{base}/health")
            data = r.json()
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    async def _upgrade_service(self, name: str, version: str | None) -> tuple[bool, str]:
        """POST /upgrade to a backend service (it pip-upgrades its own extra + re-execs).
        Long timeout — a pip install on a slow host can take minutes.

        Restart-only short-circuit: when the service's venv already holds the target
        version (typical for services co-located with an already-upgraded server —
        one shared venv), pip has nothing to do, so a restart applies it; and if it's
        already RUNNING the target, nothing happens at all. Skipped when the target
        is unknown (no version given + PyPI unreachable) — then pip decides.
        """
        base = self._service_base(name)
        if not base:
            return False, "service not reachable"
        target = version or await self._latest_pypi_version()
        if target:
            health = await self._service_health(base)
            installed, running = health.get("installed"), health.get("version")
            if installed == target:
                if running == target:
                    return True, f"already running v{target} — nothing to do"
                if await self._restart_service(name):
                    return True, f"v{target} already installed — restarted to apply it"
                return False, f"v{target} already installed but the restart failed"
        import httpx

        try:
            async with httpx.AsyncClient(timeout=900.0, verify=tlsutil.httpx_verify()) as client:
                r = await client.post(
                    f"{base}/upgrade",
                    json={"version": version},
                    headers=self._server._service_headers("POST", f"{base}/upgrade"),
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

    async def _do_upgrade_all(self, connection: ServerConnection, version: str | None) -> None:
        """Step 2 of the two-step upgrade: everything EXCEPT the server — each backend
        service sequentially (co-located services share one venv, so parallel pip
        runs could collide; after a server upgrade the short-circuit turns most of
        these into plain restarts anyway), then every connected node (fire-and-watch:
        each installs + re-execs and reconnects on its new version). Per-item
        progress/result events feed the Settings page's running log."""

        async def send(payload: dict[str, Any]) -> None:
            try:
                await connection.send(json.dumps(payload))
            except Exception:
                pass

        services = sorted({**self._service_urls, **self._server.announced_health_urls()})
        nodes = [(nid, sess.room_id) for nid, sess in self._server._nodes.items()]
        total = len(services) + len(nodes)
        step = 0
        failed: list[str] = []
        for name in services:
            step += 1
            await send(
                {
                    "type": "upgrade_progress",
                    "stage": "installing",
                    "target": name,
                    "step": step,
                    "total": total,
                }
            )
            ok, output = await self._upgrade_service(name, version)
            if not ok:
                failed.append(name)
            await send({"type": "upgrade_result", "ok": ok, "output": output, "target": name})
        for nid, room in nodes:
            step += 1
            label = f"node {room or nid}"
            await send(
                {
                    "type": "upgrade_progress",
                    "stage": "installing",
                    "target": label,
                    "step": step,
                    "total": total,
                }
            )
            ok = await self._server.upgrade_node(nid, version)
            if not ok:
                failed.append(label)
            await send(
                {
                    "type": "upgrade_result",
                    "ok": ok,
                    "target": label,
                    "output": "upgrade sent — the node re-execs and reconnects"
                    if ok
                    else "node not connected",
                }
            )
        summary = f"{total - len(failed)}/{total} upgraded" + (
            f" — failed: {', '.join(failed)}" if failed else ""
        )
        await send({"type": "upgrade_all_done", "ok": not failed, "summary": summary})

    # ------------------------------------------------------------------
    # Skill registry (kenzy-llm): read the loaded skills + live-toggle disables
    # ------------------------------------------------------------------

    async def _llm_skills_request(
        self, method: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        """GET/POST the LLM service's /skills endpoint; None if unreachable."""
        base = self._service_base("llm")  # static config ← auto-registered (right scheme under TLS)
        if not base:
            return None
        import httpx

        try:
            async with httpx.AsyncClient(timeout=4.0, verify=tlsutil.httpx_verify()) as client:
                if method == "POST":
                    r = await client.post(
                        f"{base}/skills",
                        json=payload or {},
                        headers=self._server._service_headers("POST", f"{base}/skills"),
                    )
                else:
                    r = await client.get(
                        f"{base}/skills",
                        headers=self._server._service_headers("GET", f"{base}/skills"),
                    )
                r.raise_for_status()
            data = r.json()
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    async def _ha_flags(self, *, with_persons: bool = False) -> dict[str, Any]:
        """HA-availability flags for surface gating (cached ~30 s), from the
        LLM service's ``GET /ha/persons`` plus the server's own signals.

        ``active`` = HA is in this household's picture: the control side is
        configured (HA_API_KEY) OR the app front door has been used
        (``assist_seen``) — and the home_assistant module isn't disabled.
        No-HA households therefore see no HA surfaces at all."""
        import os
        import time as _t

        now = _t.monotonic()
        cached: tuple[float, dict[str, Any]] | None = getattr(self, "_ha_flags_cache", None)
        # NB: the fetch is identical with or without persons (the endpoint
        # always returns them when reachable) — with_persons must NOT force a
        # refetch on empty results, or an unreachable HA turns every People
        # page load into a fresh 10s-timeout roundtrip.
        stale = cached is None or now - cached[0] > 30.0
        if cached is None or stale:
            base = self._service_base("llm")
            info: dict[str, Any] = {}
            if base:
                import httpx

                try:
                    async with httpx.AsyncClient(
                        timeout=10.0, verify=tlsutil.httpx_verify()
                    ) as client:
                        r = await client.get(
                            f"{base}/ha/persons",
                            headers=self._server._service_headers("GET", f"{base}/ha/persons"),
                        )
                        r.raise_for_status()
                        info = r.json()
                except Exception:
                    info = {}
            configured = bool(info.get("configured")) or bool(os.environ.get("HA_API_KEY"))
            skill_disabled = bool(info.get("skill_disabled", False))
            flags = {
                "configured": configured,
                "skill_disabled": skill_disabled,
                "reachable": bool(info.get("reachable")),
                "assist_seen": self._server.assist_seen(),
                "persons": info.get("persons") or [],
            }
            flags["active"] = (configured or flags["assist_seen"]) and not skill_disabled
            cached = (now, flags)
            self._ha_flags_cache = cached
        return dict(cached[1])

    async def _llm_curation_request(
        self, method: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        """GET/POST the LLM service's /ha/curation endpoint; None if unreachable."""
        base = self._service_base("llm")  # static config ← auto-registered (right scheme under TLS)
        if not base:
            return None
        import httpx

        try:
            async with httpx.AsyncClient(timeout=20.0, verify=tlsutil.httpx_verify()) as client:
                if method == "POST":
                    r = await client.post(
                        f"{base}/ha/curation",
                        json=payload or {},
                        headers=self._server._service_headers("POST", f"{base}/ha/curation"),
                    )
                else:
                    r = await client.get(
                        f"{base}/ha/curation",
                        headers=self._server._service_headers("GET", f"{base}/ha/curation"),
                    )
                r.raise_for_status()
            data = r.json()
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    async def _llm_memory_request(
        self, method: str, sub_path: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        """GET/POST a kenzy-llm /memory endpoint; None if unreachable/disabled."""
        base = self._service_base("llm")  # static config ← auto-registered (right scheme under TLS)
        if not base:
            return None
        import httpx

        url = f"{base}/memory{sub_path}"
        try:
            async with httpx.AsyncClient(timeout=8.0, verify=tlsutil.httpx_verify()) as client:
                if method == "POST":
                    r = await client.post(
                        url, json=payload or {}, headers=self._server._service_headers("POST", url)
                    )
                else:
                    r = await client.get(url, headers=self._server._service_headers("GET", url))
                r.raise_for_status()
            data = r.json()
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    async def _memory_state(self) -> dict[str, Any]:
        """The ledger + owner display names (dashboard Memory manager, F7.2 thin).

        The dashboard is a credentialed admin surface: it sees every fact at
        every tier — tiers gate *voices*, the login cookie gates this."""
        info = await self._llm_memory_request("GET", "")
        people = {p["id"]: p["name"] for p in self._server.list_people()}
        facts = []
        for f in (info or {}).get("facts", []):
            f = dict(f)
            f["owner_name"] = people.get(f.get("owner"), f.get("owner"))
            facts.append(f)
        lb = await self._llm_memory_request("GET", "/lockbox")
        secrets = []
        for sec in (lb or {}).get("secrets", []):
            sec = dict(sec)
            sec["owner_name"] = people.get(sec.get("owner"), sec.get("owner"))
            secrets.append(sec)
        return {
            "reachable": info is not None,
            "local_model": bool((info or {}).get("local_model", True)),  # True ⇒ no banner
            "controls": self._dcfg.controls,
            "facts": facts,
            # 4.1 lockbox: masked metadata only (label/owner/age) — the
            # dashboard never receives secret text in a list payload.
            "lockbox": {"available": bool((lb or {}).get("available")), "secrets": secrets},
        }

    async def _forget_memory(self, fact_id: str) -> tuple[bool, str | None]:
        res = await self._llm_memory_request("POST", "/forget", {"id": fact_id})
        if res is None:
            return False, "memory service not reachable (or memory is disabled)"
        return True, None

    async def _ha_curation_state(self) -> dict[str, Any]:
        info = await self._llm_curation_request("GET")
        return {
            "reachable": info is not None,
            "controls": self._dcfg.controls,
            "curation": (info or {}).get("curation", {}),
            "devices": (info or {}).get("devices", []),
            "lists": (info or {}).get("lists", []),
            "ha_reachable": bool((info or {}).get("reachable", False)),
            "skill_disabled": bool((info or {}).get("skill_disabled", False)),
            "configured": bool((info or {}).get("configured", True)),
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
            # Module-level view (group toggles): dropping this leaves the UI unable
            # to show module state — "Enable all" never appears (found live).
            "modules": (info or {}).get("modules", []),
        }

    async def _set_skill_disabled(self, name: str, disabled: bool) -> tuple[bool, str | None]:
        """Toggle one skill OR a whole module: persist + live-apply (no restart).

        Module toggles operate on the module's members too — critically for
        enable: a module can read as disabled because every member was switched
        off individually, in which case discarding just the module name would be
        a silent no-op (the "Enable all does nothing" bug)."""
        info = await self._llm_skills_request("GET")
        if info is None:
            return False, "LLM service not reachable"
        entries = info.get("skills", []) + info.get("fast_intents", [])
        current = {s["name"] for s in entries if s.get("disabled")}
        module_names = {m.get("name") for m in info.get("modules", [])}
        if name in module_names:
            members = {s["name"] for s in entries if s.get("module") == name}
            if disabled:
                current -= members  # member entries are redundant under the module's
                current.add(name)
            else:
                current -= members  # clear however the module got disabled
                current.discard(name)
        elif disabled:
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
            async with httpx.AsyncClient(timeout=5.0, verify=tlsutil.httpx_verify()) as client:
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

        # Restart-only short-circuit: if the target version is already on disk
        # (pip ran but the process never recycled), applying it is a restart, not
        # another install; and if it's already running, there's nothing to do.
        target = version or await self._latest_pypi_version()
        if target:
            from kenzy import installed_version

            installed = installed_version()
            if installed == target:
                if kenzy_version() == target:
                    await send(
                        {
                            "type": "upgrade_result",
                            "ok": True,
                            "target": "server",
                            "output": f"already running v{target} — nothing to do",
                        }
                    )
                    return
                await send(
                    {
                        "type": "upgrade_result",
                        "ok": True,
                        "target": "server",
                        "output": f"v{target} already installed — restarting to apply it",
                    }
                )
                await send({"type": "server_restarting"})
                asyncio.get_running_loop().call_later(0.8, self._server.restart_server)
                return

        await send({"type": "upgrade_progress", "stage": "installing", "target": "server"})
        ok, output = await self._server.run_self_upgrade("server", version)
        await send({"type": "upgrade_result", "ok": ok, "output": output, "target": "server"})
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
        # Use _service_base (static config ← auto-registered) rather than the
        # static-only _service_urls: with mesh TLS the service registers itself
        # as https, and the static server.yaml url may still say http — the
        # announced base carries the right scheme.
        base = self._service_base("speaker")
        if not base:
            return None
        import httpx

        try:
            async with httpx.AsyncClient(timeout=8.0, verify=tlsutil.httpx_verify()) as client:
                r = await client.request(
                    method,
                    f"{base}{sub_path}",
                    json=payload,
                    headers=self._server._service_headers(method, f"{base}{sub_path}"),
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
            # Keep the person records honest: drop the deleted voice from its owner.
            self._server.remove_person_voiceprint(name)
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
            # Follow the rename in the person records so the link doesn't break.
            self._server.rename_person_voiceprint(name, new_name)
            return True, None
        detail = res[1].get("detail") if isinstance(res[1], dict) else None
        return False, detail or f"rename failed ({res[0]})"

    # ------------------------------------------------------------------
    # People (identity records): group enrolled voices into household members
    # ------------------------------------------------------------------

    async def _person_export(
        self, person_id: str, *, include_secrets: bool = True
    ) -> dict[str, Any] | None:
        """The F7.4 "what does Kenzy know about me" download: person record +
        voice profiles + every memory fact they own (all tiers)."""
        person = next(
            (p for p in self._server.list_people() if p["id"] == person_id), None
        )
        if person is None:
            return None
        voices: list[dict[str, Any]] = []
        res = await self._speaker_request("GET", "/speakers")
        if res and res[0] == 200 and isinstance(res[1], dict):
            wanted = {str(v).lower() for v in person["voiceprints"]}
            voices = [
                {"name": v.get("name"), "samples": v.get("samples", 0)}
                for v in res[1].get("speakers", [])
                if str(v.get("name", "")).lower() in wanted
            ]
        mem = await self._llm_memory_request(
            "GET", f"/export?person={quote(person_id)}&secrets={1 if include_secrets else 0}"
        )
        import time as _t

        return {
            "exported_at": _t.strftime("%Y-%m-%d %H:%M:%S"),
            "person": person,
            "voice_profiles": voices,
            "memory": {
                "available": mem is not None,
                "facts": (mem or {}).get("facts", []),
                # Lockbox entries ride the export by default (?secrets=0 excludes;
                # the count is reported instead so the export stays a complete answer).
                **(
                    {"lockbox": (mem or {}).get("secrets", [])}
                    if include_secrets
                    else {"lockbox_excluded": (mem or {}).get("secrets_excluded", 0)}
                ),
            },
        }

    async def _revoke_person(self, person_id: str) -> tuple[bool, str | None]:
        """F7.4 revoke-all (guest departure): erase every owned non-shared fact,
        delete the enrolled voiceprints, then remove the person record.

        Ordered for retryability: memory first (unreachable ⇒ abort, nothing
        half-forgotten — but a deliberate 503 from ``memory.enabled: false``
        counts as "no ledger to erase" and proceeds), and the person record is
        kept when any voiceprint delete fails, so the operation can simply be
        re-run (an unowned voiceprint would otherwise stay RECOGNIZED with no
        person left to revoke)."""
        person = next(
            (p for p in self._server.list_people() if p["id"] == person_id), None
        )
        if person is None:
            return False, "no such person"
        status, mem = await self._llm_memory_status(
            "POST", "/erase_person", {"person": person_id}
        )
        if status == 503:
            mem = {"erased": 0}  # memory disabled — nothing to erase, proceed
        elif status != 200 or not isinstance(mem, dict):
            return False, "memory service unreachable — nothing was removed"
        failures: list[str] = []
        for vp in person["voiceprints"]:
            ok, err = await self._delete_speaker(str(vp))
            if not ok:
                failures.append(f"voice {vp!r}: {err or 'delete failed'}")
        if failures:
            # Keep the record so revoke can be re-run once the speaker service
            # is back — their remaining voiceprints must not outlive the person.
            return False, (
                "their memories were erased, but voice deletion failed — fix and "
                "run Remove again: " + "; ".join(failures)
            )
        self._server.delete_person(person_id)
        log.info(
            "Revoked person %r: %s fact(s) erased, %d voiceprint(s) deleted",
            person_id,
            mem.get("erased", "?"),
            len(person["voiceprints"]),
        )
        return True, None

    async def _llm_memory_status(
        self, method: str, sub_path: str, payload: dict[str, Any] | None = None
    ) -> tuple[int, dict[str, Any] | None]:
        """Like _llm_memory_request, but surfaces the HTTP status so callers can
        tell a deliberate 503 (memory disabled) from an outage. 0 = no service."""
        base = self._service_base("llm")
        if not base:
            return 0, None
        import httpx

        url = f"{base}/memory{sub_path}"
        try:
            async with httpx.AsyncClient(timeout=8.0, verify=tlsutil.httpx_verify()) as client:
                if method == "POST":
                    r = await client.post(
                        url, json=payload or {}, headers=self._server._service_headers("POST", url)
                    )
                else:
                    r = await client.get(url, headers=self._server._service_headers("GET", url))
            ctype = r.headers.get("content-type", "")
            data = r.json() if ctype.startswith("application/json") else None
            return r.status_code, data if isinstance(data, dict) else None
        except Exception:
            return 0, None

    async def _people_state(self) -> dict[str, Any]:
        """People records merged with the speaker service's enrolled voiceprints,
        each tagged with the person (if any) that claims it — so the panel can
        surface unassigned voices and show who owns what. Also carries the
        connected rooms for the enroll-from-a-room flow (the People tab absorbed
        the old Speakers tab)."""
        people = self._server.list_people()
        owner: dict[str, str] = {}
        for p in people:
            for vp in p["voiceprints"]:
                owner[str(vp).lower()] = p["id"]

        res = await self._speaker_request("GET", "/speakers")
        voiceprints = []
        if res and res[0] == 200 and isinstance(res[1], dict):
            for v in res[1].get("speakers", []):
                name = str(v.get("name", ""))
                voiceprints.append(
                    {
                        "name": name,
                        "samples": int(v.get("samples", 0)),
                        "person_id": owner.get(name.lower()),
                    }
                )
        rooms = [
            {"node_id": nid, "room": sess.room_id} for nid, sess in self._server._nodes.items()
        ]
        return {
            "controls": self._dcfg.controls,
            "speaker_reachable": res is not None,
            "ha": await self._ha_flags(with_persons=True),
            "people": people,
            "voiceprints": voiceprints,
            "rooms": rooms,
        }

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

    def _on_calib_event(self, node_id: str, event: dict[str, Any]) -> None:
        """Push one guided-calibration progress event to connected browsers."""
        if not self._clients:
            return
        payload = json.dumps({"type": "calibration", "node": node_id, "event": event})
        asyncio.create_task(self._send_tune(list(self._clients), payload))

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

    def _on_schedules_change(self) -> None:
        """Poke connected browsers that the schedule set changed (they re-fetch
        the auth-gated /api/schedules — no entry data rides the push itself)."""
        if self._clients:
            asyncio.create_task(self._broadcast_raw(json.dumps({"type": "schedules"})))

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
        elif mtype == "tune_watch":
            # Watch-only tune subscription: the SERVER owns the window (guided
            # calibration) — the browser just wants the live meter samples.
            if not self._dcfg.controls:
                return await ack(False, "controls are disabled (set dashboard.controls: true)")
            self._tune_subs[connection] = str(msg.get("node", ""))
            await ack(True)
        elif mtype == "tune_unwatch":
            self._tune_subs.pop(connection, None)
            await ack(True)
        elif mtype == "calibrate_start":
            # Launch the guided calibration session in silent mode: the node beeps
            # for the AEC probe and this browser renders the prompts from events.
            if not self._dcfg.controls:
                return await ack(False, "controls are disabled (set dashboard.controls: true)")
            node = str(msg.get("node", ""))
            room = ""
            sess_obj = self._server._nodes.get(node)
            if sess_obj is not None:
                room = sess_obj.room_id
            err = await self._server.start_calibration(node, room, mode="silent")
            if err is None:
                self._tune_subs[connection] = node  # live meter rides along
            await ack(err is None, err)
        elif mtype == "calibrate_cancel":
            if not self._dcfg.controls:
                return await ack(False, "controls are disabled (set dashboard.controls: true)")
            node = str(msg.get("node", ""))
            self._server._end_calib_session(node, force=True)
            await self._server.stop_node_tuning(node)
            self._tune_subs.pop(connection, None)
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
        elif mtype == "set_service_enabled":
            if not self._dcfg.controls:
                return await ack(False, "controls are disabled (set dashboard.controls: true)")
            svc = str(msg.get("service", "")).strip()
            if svc not in ("stt", "tts", "llm", "speaker"):
                return await ack(False, "unknown service")
            ok, err = await self._set_service_enabled(svc, bool(msg.get("enabled")))
            await ack(ok, err)
        elif mtype == "install_feature_deps":
            if not self._dcfg.controls:
                return await ack(False, "controls are disabled (set dashboard.controls: true)")
            svc = str(msg.get("service", "")).strip()
            if svc not in ("stt", "tts", "llm", "speaker"):
                return await ack(False, "unknown service")
            ok, err = await self._install_feature_deps(svc)
            await ack(ok, err)
        elif mtype == "restart_service":
            if not self._dcfg.controls:
                return await ack(False, "controls are disabled (set dashboard.controls: true)")
            ok = await self._restart_service(str(msg.get("service", "")))
            await ack(ok, None if ok else "service not reachable")
        elif mtype == "restore":
            if not self._dcfg.controls:
                return await ack(False, "controls are disabled (set dashboard.controls: true)")
            import base64
            import binascii

            try:
                data = base64.b64decode(str(msg.get("data", "")), validate=True)
            except (binascii.Error, ValueError):
                return await ack(False, "invalid backup payload")
            if not data:
                return await ack(False, "empty backup payload")
            try:
                # File I/O + the cert-regen openssl call — off the event loop.
                restored = await asyncio.to_thread(self._server.restore_from_archive, data)
            except Exception as exc:  # RestoreError or bad archive
                return await ack(False, f"restore failed: {exc}")
            await ack(True, None)
            await self._broadcast_state()
            # Restart so services re-pull the restored config and self-populate;
            # the WS drops and the SPA reconnects to the restored server.
            log.warning("Restore applied (%d files) — restarting server", len(restored))
            asyncio.get_running_loop().call_later(0.8, self._server.restart_server)
        elif mtype == "restart_server":
            # Standalone restart — previously only reachable as a side effect of
            # saving a config change (founder-reported UX gap).
            if not self._dcfg.controls:
                return await ack(False, "controls are disabled (set dashboard.controls: true)")
            await ack(True)
            log.info("Dashboard requested a server restart")
            asyncio.get_running_loop().call_later(0.8, self._server.restart_server)
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
        elif mtype == "upgrade_all":
            # Step 2 of the two-step upgrade: every service + node in one go (the
            # server upgrades itself via upgrade_server, typically run first).
            # Sequential; per-item events feed the Settings page's running log.
            if not self._dcfg.controls:
                return await ack(False, "controls are disabled (set dashboard.controls: true)")
            version = (str(msg.get("version") or "")).strip() or None
            await ack(True)
            asyncio.create_task(self._do_upgrade_all(connection, version))
        elif mtype == "upgrade_node":
            if not self._dcfg.controls:
                return await ack(False, "controls are disabled (set dashboard.controls: true)")
            version = (str(msg.get("version") or "")).strip() or None
            # Fire-and-watch: the node installs + re-execs, reconnecting with its new
            # version (visible in the fleet view). No progress stream from the node.
            ok = await self._server.upgrade_node(str(msg.get("node", "")), version)
            await ack(ok, None if ok else "node not connected")
        elif mtype == "set_secret":
            # Write-only API-key entry: upserts the server host's .env; the value is
            # never echoed back, served, or logged. Controls-gated (it's server
            # config, not account self-service like set_password).
            if not self._dcfg.controls:
                return await ack(False, "controls are disabled (set dashboard.controls: true)")
            try:
                self._server.set_env_secret(str(msg.get("name", "")), str(msg.get("value", "")))
            except (ValueError, OSError) as exc:
                return await ack(False, str(exc))
            await ack(True)
        elif mtype == "cancel_schedule":
            if not self._dcfg.controls:
                return await ack(False, "controls are disabled (set dashboard.controls: true)")
            sid = str(msg.get("sid", "")).strip()
            if not sid:
                return await ack(False, "schedule id is required")
            count = self._server.cancel_schedule_ids([sid])
            await ack(count > 0, None if count else "schedule not found (already fired?)")
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
        elif mtype == "forget_memory":
            if not self._dcfg.controls:
                return await ack(False, "controls are disabled (set dashboard.controls: true)")
            fact_id = str(msg.get("fact_id", "")).strip()
            if not fact_id:
                return await ack(False, "a fact id is required")
            ok, err = await self._forget_memory(fact_id)
            await ack(ok, err)
        elif mtype == "review_memory":
            if not self._dcfg.controls:
                return await ack(False, "controls are disabled (set dashboard.controls: true)")
            fid = str(msg.get("fact_id", "")).strip()
            verdict = str(msg.get("action", "")).strip()
            if not fid or verdict not in ("release", "vault"):
                return await ack(False, "fact_id and action (release|vault) required")
            res = await self._llm_memory_request("POST", "/review", {"id": fid, "action": verdict})
            await ack(res is not None, None if res is not None else "memory not reachable")
        elif mtype == "forget_secret":
            if not self._dcfg.controls:
                return await ack(False, "controls are disabled (set dashboard.controls: true)")
            sid = str(msg.get("secret_id", "")).strip()
            if not sid:
                return await ack(False, "a secret id is required")
            res = await self._llm_memory_request("POST", "/lockbox/erase", {"id": sid})
            await ack(res is not None, None if res is not None else "lockbox not reachable")
        elif mtype == "save_person":
            if not self._dcfg.controls:
                return await ack(False, "controls are disabled (set dashboard.controls: true)")
            name = str(msg.get("name", "")).strip()
            if not name:
                return await ack(False, "a name is required")
            vps = msg.get("voiceprints") or []
            if not isinstance(vps, list):
                return await ack(False, "voiceprints must be a list")
            # NB: the person id rides as `person_id`, not `id` — the WS envelope
            # reserves `id` for request/ack correlation (a payload `id` would
            # clobber it and the ack would never match its caller).
            kwargs: dict[str, Any] = {}
            if "memory_opt_out" in msg:  # absent ⇒ preserve
                kwargs["memory_opt_out"] = bool(msg.get("memory_opt_out"))
            if "memory_capture" in msg:  # absent/invalid ⇒ preserve (store validates)
                kwargs["memory_capture"] = str(msg.get("memory_capture") or "")
            if "ha_user" in msg:  # absent ⇒ preserve (three-state, see PeopleStore)
                ha = str(msg.get("ha_user") or "").strip().lower()
                if ha and " " in ha:
                    return await ack(False, "HA person can't contain spaces")
                if ha and "." not in ha:
                    ha = f"person.{ha}"  # accept the bare object_id
                kwargs["ha_user"] = ha
            try:
                self._server.save_person(
                    str(msg.get("person_id", "")).strip(), name, [str(v) for v in vps], **kwargs
                )
            except (ValueError, OSError) as exc:
                return await ack(False, str(exc))
            await ack(True)
        elif mtype == "erase_person_memory":
            # The opt-out's companion action: erase a person's existing owned
            # facts (shared stays with the house) without touching voice/record.
            if not self._dcfg.controls:
                return await ack(False, "controls are disabled (set dashboard.controls: true)")
            pid = str(msg.get("person_id", "")).strip()
            if not pid:
                return await ack(False, "a person id is required")
            status, mem = await self._llm_memory_status(
                "POST", "/erase_person", {"person": pid}
            )
            if status == 503:
                return await ack(True)  # memory disabled — nothing to erase
            if status != 200:
                return await ack(False, "memory service unreachable — nothing was erased")
            log.info("Erased %s fact(s) for %r (opt-out cleanup)", (mem or {}).get("erased"), pid)
            await ack(True)
        elif mtype == "revoke_person":
            if not self._dcfg.controls:
                return await ack(False, "controls are disabled (set dashboard.controls: true)")
            pid = str(msg.get("person_id", "")).strip()
            if not pid:
                return await ack(False, "a person id is required")
            ok, err = await self._revoke_person(pid)
            await ack(ok, err)
        elif mtype == "delete_person":
            if not self._dcfg.controls:
                return await ack(False, "controls are disabled (set dashboard.controls: true)")
            pid = str(msg.get("person_id", "")).strip()
            if not pid:
                return await ack(False, "a person id is required")
            ok = self._server.delete_person(pid)
            await ack(ok, None if ok else "person not found")
        elif mtype == "enroll_speaker":
            if not self._dcfg.controls:
                return await ack(False, "controls are disabled (set dashboard.controls: true)")
            from kenzy.server.server import TranscribingServer

            # Person-first: the People tab sends `person_id` (enrollment must
            # belong to a person). A bare `name` is still accepted for API
            # clients — the server resolves/creates the person for it.
            person_id = str(msg.get("person_id", "")).strip()
            name = str(msg.get("name", "")).strip()
            node_id = str(msg.get("node", "")).strip()
            server = self._server
            sess = server._nodes.get(node_id)
            if person_id:
                person = next((p for p in server.list_people() if p["id"] == person_id), None)
                if person is None:
                    return await ack(False, "person not found")
                name = str(person["name"])
            elif not name:
                return await ack(False, "a person (or speaker name) is required")
            if sess is None:
                return await ack(False, "pick a connected room node to enroll from")
            if not isinstance(server, TranscribingServer):
                return await ack(False, "enrollment is not available on this server")
            await server.start_enrollment(
                node_id, sess.room_id, name, operator=True, person_id=person_id or None
            )
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
        scheme = "https" if self._ssl is not None else "http"
        log.info("Dashboard on %s://%s:%d/dashboard", scheme, self._dcfg.bind, self._dcfg.port)
        if self._dcfg.auth_username is None and self._dcfg.auth_token is None:
            log.warning("Dashboard has no credentials configured — mutations disabled.")
        async with websockets.serve(
            self._ws_handler,
            self._dcfg.bind,
            self._dcfg.port,
            process_request=self.process_request,
            # Mutations are small JSON, but a `restore` carries a base64 backup
            # (a realistic archive — configs/curation/embeddings/skills, no models
            # — is tens of KB; this bound holds even a large one while staying
            # finite). Bigger archives (`?full=1` models) use `kenzy-init --restore`.
            max_size=8_388_608,  # 8 MB (F-10: still a bounded inbound frame)
            ssl=self._ssl,
        ):
            await asyncio.Future()
