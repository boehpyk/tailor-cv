"""The FastAPI application factory.

A factory rather than a module-level `app = FastAPI()` so that a test can build an application
pointed at a different database without importing a global and monkey-patching it. The module-level
`app` at the bottom exists only because uvicorn needs an import path.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import cast

import structlog
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from tailorcraft.infrastructure.api.middleware import MaxBodySizeMiddleware
from tailorcraft.infrastructure.api.routers import auth, export, health, intake, posting, tailoring
from tailorcraft.infrastructure.identity.password_hasher import Argon2PasswordHasher
from tailorcraft.infrastructure.observability import configure_logging, configure_sentry
from tailorcraft.infrastructure.persistence.database import create_engine, create_session_factory
from tailorcraft.infrastructure.persistence.registry import configure_mappings
from tailorcraft.infrastructure.settings import Settings, get_settings
from tailorcraft.infrastructure.tasks.app import app as celery_app

log = structlog.get_logger(__name__)

# ADR-0021 §2: the argon2 executor's size IS the memory cap — 2 in-flight hashes x 64 MiB per process,
# x 2 uvicorn processes = 256 MiB worst case. A constant, not a setting: if the box's memory is ever
# tight this drops to 1 before the argon2 parameters drop (ADR-0021's consequences), and that is a
# decision to make in review, not a variable to flip on a box.
ARGON2_EXECUTOR_WORKERS = 2


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
        # The password hasher and its dedicated executor (ADR-0021 §2): long-lived like the engine,
        # so the decoy hash is computed once per process, and the pool is bounded and private — never
        # the loop's default executor, which CV extraction and the posting parser share.
        argon2_executor = ThreadPoolExecutor(
            max_workers=ARGON2_EXECUTOR_WORKERS, thread_name_prefix="argon2"
        )
        app.state.argon2_executor = argon2_executor
        app.state.password_hasher = Argon2PasswordHasher(argon2_executor)

        log.info("app.started", env=settings.app_env)
        try:
            yield
        finally:
            await app.state.engine.dispose()
            # By lifespan shutdown uvicorn has drained every request, so nothing is waiting on a
            # hash; `wait=True` lets one already running finish rather than abandoning its thread.
            argon2_executor.shutdown(wait=True, cancel_futures=True)
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

    # Added first so it is the outermost user middleware — the earliest thing that sees a request
    # ORDERING, which is the opposite of what it looks like: `add_middleware` does
    # `user_middleware.insert(0, ...)` and `build_middleware_stack` wraps over `reversed(...)`, so the
    # LAST middleware registered ends up OUTERMOST. Registering this one first therefore puts it
    # *inside* CORS, and that is the arrangement we want — verified, not assumed:
    #
    #   ServerErrorMiddleware > CORSMiddleware > MaxBodySizeMiddleware > ExceptionMiddleware > router
    #
    # Inside CORS is correct because the 413 then carries `Access-Control-Allow-Origin`, so a browser
    # doing a cross-origin upload can actually READ the error envelope. Outermost, the same 413 would
    # reach the browser stripped of CORS headers and the fetch would reject with an opaque failure —
    # the user would be told nothing, for a file we know exactly what is wrong with.
    #
    # It is safe to sit inside CORS only because CORSMiddleware is *receive-transparent*: it wraps
    # `send`, never `receive`, so the undrained receive channel this depends on arrives intact.
    # THE CONSTRAINT THAT FOLLOWS: any middleware added LATER becomes outermost, and if it touches
    # `receive` or reads the body it silently defeats this check with no test to catch it. Register
    # anything body-reading BEFORE this line, and re-read middleware.py's docstring first.
    #
    # The point of all this is to answer 413 before the app calls `receive()` at all, so a client
    # sending `Expect: 100-continue` is still waiting for permission when the refusal arrives — see
    # `middleware.py` for why a cap enforced only inside the handler never achieved that.
    # Every route pays this check except `/health/*`, which polls far more often than anyone uploads.
    app.add_middleware(
        MaxBodySizeMiddleware,
        max_bytes=settings.max_upload_bytes,
        json_max_bytes=settings.json_request_max_bytes,
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

    # ------------------------------------------------------------------------------------------
    # Exception handlers. Every non-2xx response this API sends uses one envelope,
    # `{"error": {"code": ..., "message": ...}}` (technical-plan.md's API contract) — a client
    # branches on the stable `code`, never on prose. Three handlers, for three failures a router's
    # own `try/except` structurally cannot reach:
    # ------------------------------------------------------------------------------------------

    @app.exception_handler(RequestValidationError)
    async def handle_request_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """**The first of two known, deliberately-resolved conflicts this slice's spec flags**
        (T25/T26's brief, "F-1 `missing_file`").

        A required `UploadFile` parameter (`file: Annotated[UploadFile, File(...)]` on
        `routers/intake.py::upload_base_cv`) means a request with no `file` part never reaches that
        function at all: FastAPI validates declared parameters — path, query, header, cookie and
        body — *before* calling the endpoint, and raises `RequestValidationError` instead, rendering
        Starlette's own `{"detail": [...]}` shape. Left unhandled, that is a different envelope than
        every other error this API returns, and the client would have to special-case exactly one
        endpoint's exactly one failure mode to parse it. Catching it here, once, for the whole app,
        is what keeps "one error shape for the whole API" true rather than aspirational — the
        alternative (moving the file check into the handler body, past FastAPI's own validation) was
        rejected because a required parameter typed correctly is a better contract than a manual
        `if file is None` a future editor can forget to keep in sync with the OpenAPI schema.

        Only the missing-`file` case gets its own `code` (F-1's `missing_file` is the one row of the
        failure contract this exception can produce); anything else that fails FastAPI's own
        parameter validation (an unparsable path UUID, say) gets a generic 422 rather than a
        fabricated, more specific code this handler has no way to justify.
        """
        for error in exc.errors():
            if tuple(error.get("loc", ())) == ("body", "file"):
                return JSONResponse(
                    status_code=422,
                    content={
                        "error": {
                            "code": "missing_file",
                            "message": "A CV file is required.",
                        }
                    },
                )
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "validation_error",
                    "message": "The request could not be validated.",
                }
            },
        )

    @app.exception_handler(HTTPException)
    async def handle_http_exception(request: Request, exc: HTTPException) -> JSONResponse:
        """Renders every `HTTPException` this app raises (`routers/intake.py`,
        `infrastructure/api/deps.py`, `infrastructure/api/errors.py`) as the `{"error": {...}}`
        envelope, reading `code`/`message` off `exc.detail` when the raise site supplied that shape
        — which every raise site in this codebase does. `exc.headers` is forwarded unchanged so a
        429's `Retry-After` survives (F-24) exactly the way Starlette's own default handler already
        preserves it; this handler only changes the body shape, not the header behaviour.
        """
        # `cast`, not an unjustified `Any` (CLAUDE.md): Starlette's `HTTPException.__init__` types
        # its `detail` parameter as `str | None` and assigns it straight to `self.detail`, so mypy
        # sees `exc.detail: str | None` even though FastAPI's own subclass accepts `Any` and every
        # raise site in this codebase (`routers/intake.py`, `deps.py`, `errors.py`) actually passes
        # a `dict`. Without the cast, mypy treats the `isinstance(..., dict)` branch below as
        # unreachable and errors on it.
        detail = cast("object", exc.detail)
        if isinstance(detail, dict) and "code" in detail and "message" in detail:
            content = {"error": detail}
        else:
            content = {"error": {"code": "http_error", "message": str(detail)}}
        return JSONResponse(status_code=exc.status_code, content=content, headers=exc.headers)

    @app.exception_handler(SQLAlchemyError)
    async def handle_sqlalchemy_error(request: Request, exc: SQLAlchemyError) -> JSONResponse:
        """F-15: "Postgres down, or the commit fails after the file was written" -> 503
        `service_unavailable`.

        This is the second of the two conflicts, though the brief only names the first: the commit
        `deps.get_session` issues happens **after** a handler has already returned successfully — in
        that dependency's own `else: await session.commit()`, outside any `try/except` a router
        function's body could ever wrap around it. A router-local `try/except` structurally cannot
        catch this, the same architectural reason `RequestValidationError` needs a handler here
        rather than in `routers/intake.py`. Never logs the query or its parameters — `errno`-style
        identification only (Constitution §8): the exception's own type name, nothing that could
        carry a fragment of a CV that happened to be mid-flight in the same transaction.
        """
        log.error("db.request_failed", error=type(exc).__name__)
        return JSONResponse(
            status_code=503,
            content={
                "error": {
                    "code": "service_unavailable",
                    "message": "The service is temporarily unavailable. Please try again.",
                }
            },
        )

    app.include_router(health.router)
    # `/api/auth/*` shares no prefix with any other router, so its position is free.
    app.include_router(auth.router)
    app.include_router(intake.router)
    app.include_router(posting.router)
    app.include_router(tailoring.router)
    # `export` last, and the order is not arbitrary: its router declares `prefix="/api"` and
    # spells full paths for two resource shapes, so it overlaps `tailoring`'s prefix. Starlette
    # matches in registration order, and no route here shadows one above — every export path
    # under `/api/tailoring-runs/...` ends in a segment (`/exports`, `/download`) that the
    # tailoring router has no route for. Adding a route to either, check that again.
    app.include_router(export.router)

    return app


app = create_app()
