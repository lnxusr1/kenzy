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
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlsplit

import websockets
from websockets.datastructures import Headers
from websockets.http11 import Request, Response

from kenzy import serviceauth
from kenzy.logutil import install_ring_handler

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


@dataclass
class DashboardConfig:
    enabled: bool = False
    bind: str = "127.0.0.1"
    port: int = 8770
    auth_token: str | None = None
    auth_username: str | None = None
    auth_password_hash: str | None = None
    logs: bool = False
    tuning: bool = False
    controls: bool = False

    @classmethod
    def from_cfg(cls, cfg: dict[str, Any]) -> DashboardConfig:
        d = cfg.get("dashboard", {}) or {}
        auth = d.get("auth", {}) or {}
        return cls(
            enabled=bool(d.get("enabled", False)),
            bind=str(d.get("bind", "127.0.0.1")),
            port=int(d.get("port", 8770)),
            auth_token=d.get("auth_token") or None,
            auth_username=auth.get("username") or None,
            auth_password_hash=auth.get("password_hash") or None,
            logs=bool(d.get("logs", False)),
            tuning=bool(d.get("tuning", False)),
            controls=bool(d.get("controls", False)),
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


class Dashboard:
    """Serves the read-only fleet/health dashboard over HTTP."""

    def __init__(self, server: AudioServer, cfg: dict[str, Any], dcfg: DashboardConfig) -> None:
        self._server = server
        self._dcfg = dcfg
        self._service_urls = _service_targets(cfg)
        # Cookie-signing key: the password hash is a stable server-side secret, so
        # sessions survive restarts and a password change invalidates them. Fall
        # back to a per-process random key when no password is configured.
        self._cookie_secret = dcfg.auth_password_hash or secrets.token_urlsafe(32)
        # Live-push: connected browser WS clients + a short health-check cache so a
        # burst of node state changes can't hammer the backends.
        self._clients: set[ServerConnection] = set()
        self._svc_cache: tuple[float, list[dict[str, Any]]] | None = None
        server.add_state_listener(self._on_state_change)
        # Pull-based logs (only when the `logs` sub-flag is on): tell nodes to keep a
        # buffer, and capture the server's own logs for the viewer.
        server._capture_node_logs = dcfg.logs
        self._server_logs = install_ring_handler("kenzy") if dcfg.logs else None

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
        cookie = (
            f"{serviceauth.COOKIE_NAME}={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=43200"
        )
        return self._json(200, {"ok": True, "username": user}, set_cookie=cookie)

    def _logout(self) -> Response:
        cookie = f"{serviceauth.COOKIE_NAME}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0"
        return self._json(200, {"ok": True}, set_cookie=cookie)

    # ------------------------------------------------------------------
    # State surfaces
    # ------------------------------------------------------------------

    def _nodes_state(self) -> list[dict[str, Any]]:
        names = self._server.read_node_names()
        nodes: list[dict[str, Any]] = []
        for room_id, session in sorted(self._server._nodes.items()):
            has_override = self._server.read_node_override(room_id) != {}
            addr = getattr(session.ws, "remote_address", None)
            nodes.append(
                {
                    "room_id": room_id,
                    "display_name": names.get(room_id),
                    "ip": addr[0] if addr else None,
                    "configured": room_id in names or has_override,
                    "connected": True,
                    "streaming": bool(session.streaming),
                    "session_id": session.session_id,
                }
            )
        return nodes

    async def _services_state(self) -> list[dict[str, Any]]:
        if not self._service_urls:
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

        result = list(await asyncio.gather(*(check(n, u) for n, u in self._service_urls.items())))
        self._svc_cache = (time.monotonic(), result)
        return result

    async def _state(self) -> dict[str, Any]:
        return {
            "nodes": self._nodes_state(),
            "services": await self._services_state(),
            "flags": {
                "logs": self._dcfg.logs,
                "tuning": self._dcfg.tuning,
                "controls": self._dcfg.controls,
            },
        }

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
            # Live update channel: require auth, then allow the WS upgrade.
            if not self._authorized_mutation(request):
                return self._json(401, {"error": "auth required"})
            return None

        if path == "/api/login":
            return self._login(request)

        if path == "/api/logout":
            return self._logout()

        if path == "/api/me":
            user = self._current_user(request)
            return self._json(200, {"username": user, "authenticated": user is not None})

        if path == "/api/state":
            return self._json(200, await self._state())

        if path.startswith("/api/rooms/") and path.endswith("/config"):
            room = path[len("/api/rooms/") : -len("/config")]
            try:
                cfg = self._server._effective_node_config(room)
                override = self._server.read_node_override(room)
            except Exception:
                cfg, override = {}, {}
            return self._json(
                200,
                {
                    "room_id": room,
                    "display_name": self._server.read_node_names().get(room),
                    "config": cfg,
                    "override": override,
                    "editable": self._server.allowed_override_keys(),
                    "controls": self._dcfg.controls,
                },
            )

        if path == "/api/logs":
            return self._json(200, {"logs": self._tail_server_logs(request)})

        if path.startswith("/api/services/") and path.endswith("/logs"):
            name = path[len("/api/services/") : -len("/logs")]
            return self._json(200, {"logs": await self._service_logs(name, request)})

        if path.startswith("/api/rooms/") and path.endswith("/logs"):
            room = path[len("/api/rooms/") : -len("/logs")]
            return self._json(200, await self._node_logs(room, request))

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

    async def _node_logs(self, room: str, request: Request) -> dict[str, Any]:
        if not self._dcfg.logs:
            return {"logs": [], "reachable": False}
        level, limit = self._log_query(request)
        entries = await self._server.request_node_logs(room, level, limit)
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
            room = str(msg.get("room", ""))
            try:
                self._server.write_node_override(room, msg.get("config") or {})
            except (ValueError, OSError) as exc:
                return await ack(False, str(exc))
            applied = await self._server.push_config(room)  # live re-push if connected
            await ack(True)
            await self._broadcast_state()
            await connection.send(
                json.dumps({"type": "override_saved", "room": room, "applied_live": applied})
            )
        elif mtype == "set_name":
            if not self._dcfg.controls:
                return await ack(False, "controls are disabled (set dashboard.controls: true)")
            try:
                self._server.set_display_name(str(msg.get("room", "")), str(msg.get("name", "")))
            except (ValueError, OSError) as exc:
                return await ack(False, str(exc))
            await ack(True)
            await self._broadcast_state()
        elif mtype in ("trigger", "stop", "restart"):
            if not self._dcfg.controls:
                return await ack(False, "controls are disabled (set dashboard.controls: true)")
            room = str(msg.get("room", ""))
            action = {
                "trigger": self._server.trigger_node,
                "stop": self._server.stop_node,
                "restart": self._server.restart_node,
            }[mtype]
            ok = await action(room)
            await ack(ok, None if ok else "node not connected")
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
        ):
            await asyncio.Future()
