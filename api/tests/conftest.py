"""Shared test fixtures.

Three decisions here shape every test in this suite:

1. **A real PostgreSQL, never a mock and never SQLite.** A mocked repository tests the mock. SQLite
   would silently accept things PostgreSQL rejects (and vice versa) — `JSONB`, timezone-aware
   timestamps and the constraint behaviour we actually rely on.
2. **A transaction per test, rolled back.** Each test runs inside an outer transaction that is
   discarded afterwards, so tests neither see nor leave state. This is fast and total — no
   truncation, no ordering dependencies.
3. **The transaction does not roll back Redis.** Nothing in a database transaction touches rate
   limiter counters, the purge lock or its heartbeat. `clear_redis` exists for exactly that, and
   the cheap proof that a suite gets it right is to run it **twice in a row**: a second run that
   fails is the classic symptom (CLAUDE.md).

The test database is `tailorcraft_test`, dedicated and never the dev one.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from tailorcraft.infrastructure.api.deps import get_session
from tailorcraft.infrastructure.api.main import create_app
from tailorcraft.infrastructure.clock import FixedClock
from tailorcraft.infrastructure.persistence.registry import configure_mappings
from tailorcraft.infrastructure.redis_client import create_redis
from tailorcraft.infrastructure.settings import Settings, get_settings
from tailorcraft.infrastructure.tasks.app import app as celery_app


@pytest.fixture(scope="session")
def settings() -> Settings:
    """Settings pointed at the TEST database.

    `database_url` is overridden rather than read, so there is no path by which a test run reaches
    the dev database because someone's shell had the wrong variable exported.
    """
    base = get_settings()
    return base.model_copy(update={"app_env": "test", "database_url": base.test_database_url})


@pytest.fixture(scope="session", autouse=True)
def _mappings() -> None:
    """Wire the imperative mappings once for the whole session.

    Imperative mapping runs as an import side effect; without this a test that never imports a
    mapping module queries an unmapped class and fails in a way that looks like a missing table.
    """
    configure_mappings()


@pytest.fixture(scope="session")
def _migrated(settings: Settings) -> None:
    """Bring the test database to head, once per session.

    Migrations rather than `metadata.create_all()`: this way every run also proves the migrations
    themselves apply to an empty database, which is the thing the deploy will do and the thing
    `create_all` would never catch.
    """
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", settings.test_database_url)
    command.upgrade(config, "head")


@pytest_asyncio.fixture(scope="session")
async def engine(settings: Settings, _migrated: None) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(settings.test_database_url, poolclass=None)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def connection(engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    """An outer transaction that is always rolled back.

    Every session in the test binds to this connection, so anything the code under test commits is
    committed into a transaction that never reaches the database. This is the whole isolation
    mechanism, and it is why no test needs to clean up after itself.
    """
    async with engine.connect() as conn:
        transaction = await conn.begin()
        try:
            yield conn
        finally:
            await transaction.rollback()


@pytest_asyncio.fixture
async def session(connection: AsyncConnection) -> AsyncIterator[AsyncSession]:
    factory = async_sessionmaker(
        bind=connection,
        expire_on_commit=False,
        # The code under test calls `commit()`. Bound to a connection that already has a transaction
        # open, that commit releases a SAVEPOINT instead of writing through — so the code behaves
        # exactly as it does in production while the outer rollback still discards everything.
        join_transaction_mode="create_savepoint",
    )
    async with factory() as s:
        yield s


@pytest.fixture
def clock() -> FixedClock:
    """A stopped clock.

    Whole-second by contract (ADR-0007) — `FixedClock` refuses a microsecond value rather than
    letting a test double quietly violate the rule production code obeys.
    """
    return FixedClock(datetime(2026, 9, 4, 12, 0, 0, tzinfo=UTC))


@pytest_asyncio.fixture
async def clear_redis(settings: Settings) -> AsyncIterator[None]:
    """Flush the test Redis before and after a test.

    Request this in any test that touches a rate limiter, the purge lock, or the purge heartbeat.
    The database rollback above cannot help: none of that state is in PostgreSQL.

    A leftover **lock** is the dangerous one. The job then does nothing, logs that it skipped, and
    exits 0 — so a test asserting a successful run passes against a run that never happened.
    """
    client = create_redis(settings.redis_url)
    await client.flushdb()
    try:
        yield
    finally:
        await client.flushdb()
        await client.aclose()


@pytest.fixture
def app(settings: Settings, session: AsyncSession, engine: AsyncEngine) -> FastAPI:
    """The application under test, wired to this test's rolled-back transaction.

    Exposed as its own fixture so a test can reach `app.state` directly — swapping in a stub Celery,
    say — instead of digging a private attribute out of the HTTP client. A test that reads
    `client._transport` is a test that breaks when httpx renames something internal.

    The `get_session` override is what makes a request's writes visible to the test's assertions and
    still discarded at the end. Without it the endpoint would open its own connection, commit for
    real, and leave rows behind.
    """
    app = create_app(settings)
    app.dependency_overrides[get_session] = lambda: session

    # The lifespan builds the engine and session factory; ASGITransport does not run it, so the
    # pieces the routes read off app.state are wired here instead.
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = lambda: session
    app.state.celery = celery_app

    return app


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    """An HTTP client bound to the application above.

    `ASGITransport` calls the app in-process — no socket, no port, no server to start or wait for.
    It also does not run the lifespan, which is why the `app` fixture wires `app.state` by hand.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c
