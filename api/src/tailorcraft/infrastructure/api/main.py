"""The FastAPI application factory.

A factory rather than a module-level `app = FastAPI()` so that a test can build an application
pointed at a different database without importing a global and monkey-patching it. The module-level
`app` at the bottom exists only because uvicorn needs an import path.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from tailorcraft.infrastructure.api.routers import health
from tailorcraft.infrastructure.observability import configure_logging, configure_sentry
from tailorcraft.infrastructure.persistence.database import create_engine, create_session_factory
from tailorcraft.infrastructure.persistence.registry import configure_mappings
from tailorcraft.infrastructure.settings import Settings, get_settings
from tailorcraft.infrastructure.tasks.app import app as celery_app

log = structlog.get_logger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the application. Everything long-lived is created once, here."""
    settings = settings or get_settings()

    configure_logging(settings)
    configure_sentry(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Imperative mappings must be wired before the first query. A mapping module nobody imports
        # never runs its `map_imperatively()` call, and the aggregate stays silently unmapped until
        # something fails confusingly at query time (ADR-0007).
        configure_mappings()

        app.state.settings = settings
        app.state.engine = create_engine(settings)
        app.state.session_factory = create_session_factory(app.state.engine)
        app.state.celery = celery_app

        log.info("app.started", env=settings.app_env)
        try:
            yield
        finally:
            await app.state.engine.dispose()
            log.info("app.stopped")

    app = FastAPI(
        title="TailorCraft API",
        version="0.1.0",
        lifespan=lifespan,
        # The OpenAPI schema is the frontend's contract (ADR-0001), so it stays available in dev.
        # Off in production: it is a free map of the API for anyone who asks.
        docs_url=None if settings.is_production else "/docs",
        openapi_url=None if settings.is_production else "/openapi.json",
    )

    if settings.cors_origin_list:
        # An explicit origin list, never a wildcard. Cookies carry the refresh token (ADR-0008) and
        # `allow_credentials` with `allow_origins=["*"]` is the combination browsers refuse and
        # developers then "fix" by echoing the request's own Origin back — which is not a policy,
        # it is the absence of one.
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origin_list,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    app.include_router(health.router)

    return app


app = create_app()
