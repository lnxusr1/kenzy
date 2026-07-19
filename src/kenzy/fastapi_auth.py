"""FastAPI-side service-to-service auth (imported only by the HTTP services).

Kept separate from ``kenzy.serviceauth`` because that module is also imported by
``kenzy-server``, which does not depend on FastAPI.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.responses import Response

from kenzy.logutil import install_ring_handler
from kenzy.serviceauth import (
    SIG_HEADER,
    check_bearer,
    service_token_from_env,
    verify_service_request,
)

log = logging.getLogger(__name__)


def _service_authorized(request: Request, token: str) -> bool:
    """True when a request carries valid token-proof auth (``X-Kenzy-Auth``) or,
    during the deprecation window, the legacy bearer. Requests never carry the
    token in a replayable form once the bearer is dropped (a later minor)."""
    if (
        verify_service_request(
            request.headers.get(SIG_HEADER), token, request.method, request.url.path
        )
        is not None
    ):
        return True
    return check_bearer(request.headers.get("authorization"), token)


class UpgradeRequest(BaseModel):
    # Optional exact version to pin; None → latest (floored >=3.0.0). Module-level so
    # FastAPI recognizes it as the request body (a local class is read as a query param).
    version: str | None = None


def install_service_auth(app: FastAPI) -> None:
    """Require ``KENZY_SERVICE_TOKEN`` as a bearer on every route except /health.

    No-op when the env var is unset, so service-to-service auth is opt-in and
    backward compatible. ``/health`` stays open (the dashboard polls it).
    """
    token = service_token_from_env()
    if not token:
        return

    @app.middleware("http")
    async def _service_token_guard(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.url.path != "/health" and not _service_authorized(request, token):
            return JSONResponse({"detail": "invalid service token"}, status_code=401)
        return await call_next(request)


def install_logs_endpoint(
    app: FastAPI, logger_name: str = "kenzy", capture_level: int = logging.DEBUG
) -> None:
    """Capture this service's logs in a ring buffer and expose ``GET /logs``.

    The buffer captures down to ``capture_level`` (default debug) so the dashboard
    viewer can show more than the console prints. Protected by the service-token
    middleware like every route except /health.
    """
    buf = install_ring_handler(logger_name, level=capture_level)
    levels = logging.getLevelNamesMapping()

    async def logs(level: str = "", limit: int = 200) -> dict[str, object]:
        lv = levels.get(level.upper(), 0) if level else 0
        return {"logs": buf.tail(lv, limit)}

    app.add_api_route("/logs", logs, methods=["GET"])


def install_restart_endpoint(app: FastAPI) -> None:
    """Expose ``POST /restart`` that re-execs the service (re-pulls fresh config).

    Mirrors the node's self-restart: the process replaces itself with the same
    argv, so it works with or without a service manager. Protected by the
    service-token middleware like every route except /health. The dashboard POSTs
    here after editing a service's central config so the new config takes effect.
    """

    async def restart() -> dict[str, str]:
        async def _exec() -> None:
            # Give the HTTP response a moment to flush before re-execing.
            await asyncio.sleep(0.1)
            log.warning("Restart requested — re-executing service")
            os.execv(sys.executable, [sys.executable, *sys.argv])

        asyncio.create_task(_exec())
        return {"status": "restarting"}

    app.add_api_route("/restart", restart, methods=["POST"])


def install_backup_endpoint(app: FastAPI, items_fn: Callable[[], list[tuple[Path, str]]]) -> None:
    """Expose ``GET /backup`` — this service's state slice as a tar.gz.

    ``items_fn`` returns ``(path, archive-prefix)`` pairs, evaluated per request
    (the dirs are configured at startup and may be created later). The server
    merges these slices into the one downloadable backup archive so a multi-host
    deployment is still complete (speaker embeddings, the LLM host's
    skills/curation). Protected by the service-token middleware like every route
    except /health.
    """

    async def backup() -> Response:
        from kenzy.backup import archive_entries, collect_paths

        return Response(
            content=archive_entries(collect_paths(items_fn()), None),
            media_type="application/gzip",
        )

    app.add_api_route("/backup", backup, methods=["GET"])


def install_features_endpoint(app: FastAPI, report_fn: Callable[[], list[dict[str, Any]]]) -> None:
    """``GET /features`` — the service's optional-feature states for the
    dashboard's chips: [{name, configured, available, active, install, note}].
    Token-protected by the service-auth middleware like every route."""

    async def features() -> dict[str, object]:
        return {"features": report_fn()}

    app.add_api_route("/features", features, methods=["GET"])


def install_fill_endpoint(app: FastAPI, extra: str) -> None:
    """``POST /install_deps`` — the feature chips' Install action: fill missing
    dependencies for this service's extra (never moves versions), then re-exec
    so newly-available features start."""
    from kenzy.upgrade import run_pip_fill

    async def install_deps() -> dict[str, object]:
        ok, output = await run_pip_fill(extra)
        if ok:

            async def _exec() -> None:
                await asyncio.sleep(0.3)
                log.warning("Dependencies filled — re-executing service")
                os.execv(sys.executable, [sys.executable, *sys.argv])

            asyncio.create_task(_exec())
        return {"ok": ok, "output": output}

    app.add_api_route("/install_deps", install_deps, methods=["POST"])


def install_unit_endpoint(app: FastAPI, unit: str) -> None:
    """``GET /unit`` (systemd --user state) and ``POST /unit`` with
    ``{"action": "disable"}`` — a service may STOP ITSELF via systemd so
    Restart= policies don't resurrect it. Enabling a stopped service can't
    arrive here (nothing is listening) — the server handles co-located
    enables; remote stopped units are the operator's (documented)."""
    from kenzy.unitctl import disable_unit, unit_state

    async def get_unit() -> dict[str, object]:
        return {"unit": unit, **unit_state(unit)}

    class UnitAction(BaseModel):
        action: str

    async def post_unit(body: UnitAction) -> dict[str, object]:
        if body.action != "disable":
            return {"ok": False, "error": "only 'disable' is supported here"}

        async def _later() -> None:
            await asyncio.sleep(0.5)  # let the response flush first
            ok, out = await asyncio.to_thread(disable_unit, unit)
            if not ok:
                log.error("Self-disable failed: %s", out)

        asyncio.create_task(_later())
        return {"ok": True}

    app.add_api_route("/unit", get_unit, methods=["GET"])
    app.add_api_route("/unit", post_unit, methods=["POST"])


def install_upgrade_endpoint(app: FastAPI, extra: str) -> None:
    """Expose ``POST /upgrade`` that pip-upgrades ``kenzy[extra]`` then re-execs.

    The pip run is awaited (it can take minutes — the server's fan-out call uses a long
    timeout), and on success the service re-execs *after* the response flushes so the
    caller sees the result. Protected by the service-token middleware like /restart.
    """
    from kenzy.upgrade import run_pip_upgrade

    async def upgrade(body: UpgradeRequest) -> dict[str, object]:
        ok, output = await run_pip_upgrade(extra, body.version)
        if ok:

            async def _exec() -> None:
                await asyncio.sleep(0.3)  # let the response flush first
                log.warning("Upgrade applied — re-executing service")
                os.execv(sys.executable, [sys.executable, *sys.argv])

            asyncio.create_task(_exec())
        return {"ok": ok, "output": output}

    app.add_api_route("/upgrade", upgrade, methods=["POST"])
