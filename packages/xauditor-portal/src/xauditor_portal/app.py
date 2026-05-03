"""FastAPI application factory.

Wiring:
- Async engine + `async_sessionmaker` attached to `app.state.session_factory`.
- Portal settings attached to `app.state.portal_settings` (tests can
  override). JWT signing secret attached to `app.state.jwt_secret` for
  deterministic tests.
- Routers for health, auth, runs, findings, feedback, config.

The engine URL comes from `XAUDITOR_PORTAL_DATABASE_URL` when set, else
from the main xauditor package via `load_config().reportdb` (local fallback
to postgresql+asyncpg://xauditor:xauditor-password@127.0.0.1:5432/xauditor_reportdb).
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from xauditor_portal.api import (
    auth as auth_router,
    config as config_router,
    feedback as feedback_router,
    findings as findings_router,
    health as health_router,
    projects as projects_router,
    runs as runs_router,
    users as users_router,
)
from xauditor_portal.db.base import (
    build_engine_url,
    make_async_session_factory,
)
from xauditor_portal.settings import PortalSettings, get_settings


log = logging.getLogger("xauditor_portal.app")


def _resolve_database_url(settings: PortalSettings) -> str:
    if settings.database_url:
        return settings.database_url
    # Fall back to main xauditor config if importable.
    try:
        from pathlib import Path

        from xauditor.config import load_config

        config = load_config(repo_root=Path.cwd())
        return build_engine_url(config.reportdb)
    except Exception as exc:  # noqa: BLE001 - fall back to a sentinel URL
        log.warning("Falling back to default database URL: %s", exc)
        return "postgresql+asyncpg://xauditor:xauditor-password@127.0.0.1:5432/xauditor_reportdb"


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: PortalSettings = app.state.portal_settings
    from sqlalchemy.ext.asyncio import create_async_engine

    # If an engine has already been stashed on app.state (tests), respect it.
    engine = getattr(app.state, "engine", None)
    if engine is None:
        url = _resolve_database_url(settings)
        engine = create_async_engine(url, future=True, pool_pre_ping=True)
        app.state.engine = engine
    app.state.session_factory = make_async_session_factory(engine)
    try:
        yield
    finally:
        if getattr(app.state, "_owns_engine", True):
            await engine.dispose()


def create_app(*, settings: PortalSettings | None = None) -> FastAPI:
    app = FastAPI(
        title="xauditor Portal",
        version="0.1.0",
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
        lifespan=_lifespan,
    )
    app.state.portal_settings = settings or get_settings()
    app.state._owns_engine = True

    app.include_router(health_router.router, prefix="/api")
    app.include_router(auth_router.router, prefix="/api/auth")
    app.include_router(runs_router.router, prefix="/api/runs")
    app.include_router(projects_router.router, prefix="/api/projects")
    app.include_router(findings_router.router, prefix="/api")
    app.include_router(feedback_router.router, prefix="/api")
    app.include_router(config_router.router, prefix="/api/config")
    app.include_router(users_router.router, prefix="/api/users")

    return app


__all__ = ["create_app"]
