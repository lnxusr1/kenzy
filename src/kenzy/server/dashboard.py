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
import hmac
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import websockets
from websockets.datastructures import Headers
from websockets.http11 import Request, Response

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
    logs: bool = False
    tuning: bool = False
    controls: bool = False

    @classmethod
    def from_cfg(cls, cfg: dict[str, Any]) -> DashboardConfig:
        d = cfg.get("dashboard", {}) or {}
        return cls(
            enabled=bool(d.get("enabled", False)),
            bind=str(d.get("bind", "127.0.0.1")),
            port=int(d.get("port", 8770)),
            auth_token=d.get("auth_token") or None,
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

    # ------------------------------------------------------------------
    # Auth (reserved for mutating endpoints; read-only GETs are LAN-open)
    # ------------------------------------------------------------------

    def _authorized(self, request: Request) -> bool:
        if not self._dcfg.auth_token:
            return True
        header = request.headers.get("Authorization", "")
        token = header[7:] if header.startswith("Bearer ") else header
        return hmac.compare_digest(token, self._dcfg.auth_token)

    # ------------------------------------------------------------------
    # State surfaces
    # ------------------------------------------------------------------

    def _nodes_state(self) -> list[dict[str, Any]]:
        nodes: list[dict[str, Any]] = []
        for room_id, session in sorted(self._server._nodes.items()):
            nodes.append(
                {
                    "room_id": room_id,
                    "connected": True,
                    "streaming": bool(session.streaming),
                    "session_id": session.session_id,
                }
            )
        return nodes

    async def _services_state(self) -> list[dict[str, Any]]:
        if not self._service_urls:
            return []
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

        return list(await asyncio.gather(*(check(n, u) for n, u in self._service_urls.items())))

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
    def _json(status: int, payload: Any) -> Response:
        headers = Headers()
        headers["Content-Type"] = "application/json"
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

        if path == "/api/state":
            return self._json(200, await self._state())

        if path.startswith("/api/rooms/") and path.endswith("/config"):
            room = path[len("/api/rooms/") : -len("/config")]
            try:
                cfg = self._server._effective_node_config(room)
            except Exception:
                cfg = {}
            return self._json(200, {"room_id": room, "config": cfg})

        if path.startswith("/api/"):
            return self._json(404, {"error": "unknown endpoint"})

        return self._static(path)

    async def serve(self) -> None:
        log.info("Dashboard on http://%s:%d/dashboard", self._dcfg.bind, self._dcfg.port)
        if self._dcfg.auth_token is None:
            log.info("Dashboard auth: none (LAN-trust). Bind is %s.", self._dcfg.bind)
        async with websockets.serve(
            self._reject_ws,
            self._dcfg.bind,
            self._dcfg.port,
            process_request=self.process_request,
        ):
            await asyncio.Future()

    async def _reject_ws(self, connection: ServerConnection) -> None:
        # No live WS channel in the foundation; any upgrade just closes.
        await connection.close()
