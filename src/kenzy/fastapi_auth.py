"""FastAPI-side service-to-service auth (imported only by the HTTP services).

Kept separate from ``kenzy.serviceauth`` because that module is also imported by
``kenzy-server``, which does not depend on FastAPI.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.responses import Response

from kenzy.logutil import install_ring_handler
from kenzy.serviceauth import check_bearer


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


def install_logs_endpoint(app: FastAPI, logger_name: str = "kenzy") -> None:
    """Capture this service's logs in a ring buffer and expose ``GET /logs``.

    Protected by the service-token middleware like every route except /health.
    """
    buf = install_ring_handler(logger_name)
    levels = logging.getLevelNamesMapping()

    async def logs(level: str = "", limit: int = 200) -> dict[str, object]:
        lv = levels.get(level.upper(), 0) if level else 0
        return {"logs": buf.tail(lv, limit)}

    app.add_api_route("/logs", logs, methods=["GET"])
