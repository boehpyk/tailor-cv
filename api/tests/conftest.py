"""Shared test fixtures.

Four decisions here shape every test in this suite:

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
4. **The transaction does not roll back the filesystem either.** Every upload a test makes through
   `LocalFileStore` lands on disk for real, and `settings.upload_dir` defaults to the same shared,
   named volume `docker-compose.yml` mounts on both `api` and `worker` in production — the volume
   CLAUDE.md singles out as load-bearing precisely because it is real user data. Left unredirected,
   a test run writes real files into it and never cleans them up, growing without bound across every
   `make test` anyone runs. The `settings` fixture below points `upload_dir` at a session-scoped
   temp directory for exactly this reason, the same way `clear_redis` isolates Redis.

The test database is `tailorcraft_test`, dedicated and never the dev one. The test **Redis** is a
dedicated database on the same server (`test_redis_url`, derived from `redis_url` with the number
swapped) — dedicated since 2026-09-22 and not before, which is the third point's whole story.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from tailorcraft.infrastructure.api.deps import get_session
from tailorcraft.infrastructure.api.main import create_app
from tailorcraft.infrastructure.clock import FixedClock
from tailorcraft.infrastructure.persistence.database import create_engine
from tailorcraft.infrastructure.persistence.registry import configure_mappings
from tailorcraft.infrastructure.redis_client import create_redis
from tailorcraft.infrastructure.settings import Settings, get_settings
from tailorcraft.infrastructure.tasks.app import app as celery_app


@pytest.fixture(scope="session")
def settings(tmp_path_factory: pytest.TempPathFactory) -> Settings:
    """Settings pointed at the TEST database, and at a disposable upload directory.

    `database_url` is overridden rather than read, so there is no path by which a test run reaches
    the dev database because someone's shell had the wrong variable exported.

    `upload_dir` is overridden for the same reason `clear_redis` exists: a database transaction's
    rollback does not touch the filesystem, so every upload a test makes through `LocalFileStore`
    would otherwise land in the real, shared `uploads` volume and stay there forever — this is the
    module docstring's fourth point. `tmp_path_factory.mktemp` is session-scoped, matching this
    fixture's own scope (and pytest-asyncio's session-scoped loop, CLAUDE.md); pytest removes it
    afterwards on its own schedule, so nothing here needs to.

    **`redis_url` is overridden for exactly the same reason, and was not until 2026-09-22.** Until
    then this fixture isolated Postgres and the filesystem and left Redis pointing at the running
    dev system, so `clear_redis`'s `flushdb()` — below, which called it "the test Redis" — wiped the
    real one. Every `make test` did it, which by way of the pre-commit hook means every commit. It
    surfaced only when a job that *writes* to Redis (the guest purge's heartbeat) finally had to
    survive one: the heartbeat vanished between the rehearsal and the switch-on, and `/health/ready`
    reported `stale: true` on a system where nothing was wrong.
    The lesson is worth more than the line: **test isolation is a property you check per datastore.**
    Postgres was isolated, the filesystem was isolated, and Redis looked isolated because a docstring
    said so.
    `Settings` now derives `test_redis_url` from `redis_url` with the database swapped and **refuses
    a configuration where the two are the same store** — the check is the fix, the derivation is
    only a default.
    """
    base = get_settings()
    return base.model_copy(
        update={
            "app_env": "test",
            "database_url": base.test_database_url,
            "redis_url": base.test_redis_url,
            "upload_dir": tmp_path_factory.mktemp("uploads"),
        }
    )


@pytest.fixture(scope="session", autouse=True)
def _mappings() -> None:
    """Wire the imperative mappings once for the whole session.

    Imperative mapping runs as an import side effect; without this a test that never imports a
    mapping module queries an unmapped class and fails in a way that looks like a missing table.
    """
    configure_mappings()


async def _reset_schema(url: str) -> None:
    """Drop and recreate `public` in the test database, refusing any database not named `*_test`.

    The guard comes before the first statement: `get_settings()` under `APP_ENV=test` still returns
    the dev `database_url` (CLAUDE.md), and this function's only statement is a `DROP ... CASCADE`.
    """
    database = make_url(url).database
    if not database or not database.endswith("_test"):
        raise RuntimeError(f"refusing to reset schema of non-test database {database!r}")
    eng = create_async_engine(url, poolclass=NullPool)
    try:
        async with eng.begin() as conn:
            await conn.execute(text("DROP SCHEMA public CASCADE"))
            await conn.execute(text("CREATE SCHEMA public"))
    finally:
        await eng.dispose()


@pytest.fixture(scope="session")
def _migrated(settings: Settings) -> None:
    """Bring an EMPTY test database to head, once per session.

    Migrations rather than `metadata.create_all()`: this way every run also proves the migrations
    themselves apply to an empty database, which is the thing the deploy will do and the thing
    `create_all` would never catch.

    **Empty is load-bearing, not tidiness.** PostgreSQL never gives back a dropped column's slot —
    `DROP COLUMN` only marks it dead, and dead columns count toward the 1600-column table limit
    until the table is recreated. The migration up/down/up tests drop and re-add `tailoring_run`'s
    columns on every run, so a test database that persisted between runs accumulated them: on
    2026-09-23 it held 16 live columns and 1580 dead ones, `upgrade head` hit `TooManyColumnsError`,
    and 428 unrelated tests errored. CI never saw it — its database is new every time. Resetting
    here makes every local run start where CI does, and it also clears a schema a previous run left
    downgraded by being interrupted mid-migration-test.

    `asyncio.run` is safe here for the same reason Alembic's own `env.py` may call it: a sync
    session fixture runs while pytest-asyncio's loop is idle.
    """
    asyncio.run(_reset_schema(settings.test_database_url))
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", settings.test_database_url)
    command.upgrade(config, "head")


@pytest_asyncio.fixture(scope="session")
async def engine(settings: Settings, _migrated: None) -> AsyncIterator[AsyncEngine]:
    """Built through the production `create_engine` factory, not a bare `create_async_engine` call.

    That is a deliberate change (verify round 1, T41-style): a test asserting something about how
    this engine reports a database-side failure — `hide_parameters`, and eventually the
    error-sanitizing `handle_error` listener `create_engine` registers — can only guard the real
    config if the connection under test was actually built by it. A bare `create_async_engine` call
    here would let every such test pass or fail on a *different* engine than the one production
    (and the worker's own per-task `tasks/container.py::tailoring_use_case`) actually uses.

    Still session-scoped, still bound to pytest-asyncio's own session-scoped loop — unaffected by
    the change, since `create_engine` builds an ordinary `AsyncEngine` and this fixture's lifetime
    story doesn't change. The genuinely different case, where a *shared* engine would be wrong, is
    the worker's own per-task engine: a fresh loop per Celery task means a fresh engine per task,
    which is exactly what `tasks/container.py`'s own module docstring explains at length. Nothing
    about that changes here.
    """
    eng = create_engine(settings)
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

    That first sentence was **false from the day it was written until 2026-09-22**, and is worth
    leaving a scar over rather than a silent correction: there was no test Redis. The `settings`
    fixture swapped `database_url` and `upload_dir` and not `redis_url`, so this `flushdb()` landed
    on the running dev system's Redis every time the suite ran. It is true now — `settings` swaps in
    `test_redis_url`, which `Settings` refuses to let be the same store as the cache, the broker or
    the result backend.

    Request this in any test that touches a rate limiter, the purge lock, or the purge heartbeat.
    The database rollback above cannot help: none of that state is in PostgreSQL.

    A leftover **lock** is the dangerous one. The job then does nothing, logs that it skipped, and
    exits 0 — so a test asserting a successful run passes against a run that never happened.

    **`flushdb()` is global, so this suite cannot be run concurrently with another copy of itself.**
    A second pytest process against this Redis — another terminal, an agent running the gates,
    `pytest -n` — has its `clear_redis` delete *this* run's rate-limiter counters mid-test. The
    limiters fail open, so a third request that should be `429` comes back `201`/`202`, and the
    knock-on assertions fail with unrelated `409`s. The symptom is a **different set of unrelated
    tests failing on each run, all of which pass in isolation**; it reads as flaky application code
    and is not. Two sessions lost time to this during 1.6's `/verify`. If this ever needs to be
    parallel-safe, the fix is a per-run key prefix (or a per-worker Redis db) and deleting by that
    prefix here — not a narrower `flushdb`, which would reintroduce the leftover-lock hazard above.
    """
    client = create_redis(settings.redis_url)
    await client.flushdb()
    try:
        yield
    finally:
        await client.flushdb()
        await client.aclose()


def _committing_session_override(
    session: AsyncSession,
) -> Callable[[], AsyncIterator[AsyncSession]]:
    """Build the `get_session` override: an async-generator *function*, not a fixture.

    This has to be a plain function that FastAPI itself calls per request and drives as a yield
    dependency — not a `pytest_asyncio.fixture`. A fixture that is an async generator is consumed by
    pytest_asyncio itself, up to the first `yield`, and only the *yielded value* (here, `session`) is
    what a dependent fixture receives; the code after `yield` then runs once, at that fixture's own
    teardown — not once per request the way `deps.get_session`'s does. Wrapping this in
    `@pytest_asyncio.fixture` would silently reproduce a milder version of the exact bug this whole
    fix is for: the commit/rollback body would still run, just at the wrong time and the wrong
    number of times, for a reason just as invisible as the `lambda` it replaced.

    `get_session` is a **yield dependency**: it commits on success and rolls back on any exception
    raised while the request runs, and that commit/rollback pair happens in the dependency's own
    teardown, after the route handler has already returned — a router-local `try/except` structurally
    cannot reach it (see `main.py`'s `SQLAlchemyError` handler docstring for why F-15 needs an
    app-level handler for exactly that reason).

    A plain `lambda: session` used to stand in for it here. FastAPI resolves a yield-dependency
    override by calling it and, if the result is not a generator, using that return value directly —
    it never opens the generator, so it never runs the `try/yield/except: rollback / else: commit`
    body at all. A `lambda` *returns* a session; it does not *yield* one, so that body — the very
    thing that makes this a unit-of-work boundary rather than a bare handle — silently never executed
    during a test request. Nothing observable told you: requests still succeeded, responses still
    carried the rows the handler built in memory (`session`'s object identity map serves those back
    without a commit), and only a test that specifically depends on the dependency's own `commit()`
    failing — F-15 — could ever notice. That is the trap worth naming: a yield dependency replaced by
    a plain callable loses its teardown *silently*, and a suite can claim to exercise a commit
    boundary it has actually stopped touching, for as long as nobody writes the one test that would
    catch it.

    This restores `get_session`'s own shape — a per-request `try/yield/except: rollback / else:
    commit` — bound to the already-open, SAVEPOINT-per-test `session` fixture instead of a fresh one
    from `session_factory`. A successful request really does call `session.commit()` (releasing that
    SAVEPOINT; the outer transaction opened by the `connection` fixture is still what gets rolled back
    at teardown, so isolation is unchanged), a request that raises really does call
    `session.rollback()`, and a monkeypatched `commit` that raises `SQLAlchemyError` propagates out of
    this generator uncaught — exactly where `main.py`'s handler expects to catch it and render the
    503. The one deliberate difference from `deps.get_session`: production's version also rolls back a
    *failed commit* only indirectly (via `session.close()` inside the `async with factory() as
    session:` it opens, whose `__aexit__` runs on the way out even when `else:` itself is what
    raised). This override cannot call `session.close()` — the same `session` object is reused across
    every request a test makes and the test's later assertions still need it live — so the
    failed-commit case below rolls back explicitly instead, reaching the same "nothing half-written
    survives" outcome without closing the session out from under the rest of the test.
    """

    async def _override() -> AsyncIterator[AsyncSession]:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        else:
            try:
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    return _override


@pytest.fixture
def app(settings: Settings, session: AsyncSession, engine: AsyncEngine) -> FastAPI:
    """The application under test, wired to this test's rolled-back transaction.

    Exposed as its own fixture so a test can reach `app.state` directly — swapping in a stub Celery,
    say — instead of digging a private attribute out of the HTTP client. A test that reads
    `client._transport` is a test that breaks when httpx renames something internal.

    The `get_session` override is what makes a request's writes visible to the test's assertions and
    still discarded at the end, *and* what makes the request's commit/rollback semantics real rather
    than assumed — see `_committing_session_override`'s docstring for why this used to be a bare
    `lambda` and why that was silently wrong. Without an override at all the endpoint would open its
    own connection, commit for real, and leave rows behind.
    """
    app = create_app(settings)
    app.dependency_overrides[get_session] = _committing_session_override(session)

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
