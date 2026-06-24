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

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.responses import Response

from kenzy.logutil import install_ring_handler
from kenzy.serviceauth import check_bearer

log = logging.getLogger(__name__)


class UpgradeRequest(BaseModel):
    # Optional exact version to pin; None → latest (floored >=3.0.0). Module-level so
    # FastAPI recognizes it as the request body (a local class is read as a query param).
    version: str | None = None


def install_service_auth(app: FastAPI) -> None:
    """Require ``KENZY_SERVICE_TOKEN`` as a bearer on every route except /health.

    No-op when the env var is unset, so service-to-service auth is opt-in and
    backward compatible. ``/health`` stays open (the dashboard polls it).
    """
    token = os.environ.get("KENZY_SERVICE_TOKEN")
    if not token:
        return

    @app.middleware("http")
    async def _service_token_guard(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.url.path != "/health" and not check_bearer(
            request.headers.get("authorization"), token
        ):
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
