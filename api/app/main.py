"""FastAPI application factory.

``create_app`` wires the ``/healthz`` probe + JSON logging (T0.4), the Bearer
auth middleware (T1.2), and the MCP server mounted on ``/mcp`` (T1.5).
"""

from __future__ import annotations

import contextlib
import secrets
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import RedirectResponse
from starlette.staticfiles import StaticFiles
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.admin.auth import NotAuthenticated
from app.admin.router import admin_router
from app.auth import AuthMiddleware
from app.config import get_settings
from app.db import get_session
from app.log import configure_logging, get_logger
from app.mcp_server import build_mcp

logger = get_logger()


@contextlib.asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Run the mounted MCP server's session manager for the app's lifetime.

    FastAPI does not run a mounted sub-app's own lifespan, so the streamable-HTTP
    session manager (created lazily by ``streamable_http_app()`` at mount time)
    must be started here. ``app.state.mcp`` is the instance mounted below.
    """
    async with app.state.mcp.session_manager.run():
        yield


async def healthz(session: AsyncSession = Depends(get_session)) -> JSONResponse:
    """Liveness/readiness probe: verify the DB answers a trivial query.

    No auth (stays outside the T1.2 middleware). 200 when the DB responds,
    503 with a detail when it does not.
    """
    try:
        await session.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 — any DB failure means "not ok" for a health probe
        logger.error("healthz db check failed: %s", exc)
        return JSONResponse(
            status_code=503,
            content={"status": "error", "db": "error", "detail": str(exc)},
        )
    return JSONResponse(content={"status": "ok", "db": "ok"})


def create_app() -> FastAPI:
    """Build and return the FastAPI application."""
    configure_logging()
    settings = get_settings()
    # A fresh FastMCP per app: its session manager's run() is one-shot per
    # instance, so reusing a global would break a second create_app() (tests).
    mcp = build_mcp()
    app = FastAPI(title="Archivist API", version="0.1.0", lifespan=_lifespan)
    app.state.settings = settings
    app.state.mcp = mcp
    app.add_api_route("/healthz", healthz, methods=["GET"])
    # Mount the MCP server at /mcp (streamable_http_path="/" → canonical /mcp URL);
    # the session manager it created here is started by _lifespan above.
    app.mount("/mcp", mcp.streamable_http_app())
    # Bearer auth guards /mcp and its sub-paths; /healthz stays open.
    app.add_middleware(AuthMiddleware)

    # --- admin console (server-rendered /admin) ---
    app.include_router(admin_router)
    app.mount(
        "/admin/static",
        StaticFiles(directory=str(Path(__file__).resolve().parent / "admin" / "static")),
        name="admin-static",
    )

    async def _redirect_login(_request: object, _exc: Exception) -> RedirectResponse:
        return RedirectResponse("/admin/login", status_code=303)

    app.add_exception_handler(NotAuthenticated, _redirect_login)

    secret = settings.admin_session_secret or secrets.token_urlsafe(32)
    if settings.admin_session_secret is None:
        logger.warning(
            "ADMIN_SESSION_SECRET is unset; using an ephemeral secret "
            "(admin sessions reset on restart). Set it in production."
        )
    app.add_middleware(
        SessionMiddleware,
        secret_key=secret,
        session_cookie="archivist_admin",
        https_only=True,
        same_site="strict",
    )
    # Outermost: rewrite scope client/scheme from X-Forwarded-For/-Proto before
    # any route reads request.client, so the /admin login throttle keys on the
    # real client behind Caddy instead of the shared proxy address. Trust is
    # scoped to the proxy network (never "*") so a spoofed forwarded header from
    # a client cannot masquerade as another IP — see Settings.forwarded_allow_ips.
    app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=settings.forwarded_allow_ips)
    return app
