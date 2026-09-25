"""Engine and session lifecycle.

The unit of work opens at a boundary — an HTTP request or a Celery task — and commits there. No
`AsyncSession` is ever passed into the application layer: a use case receives repository *ports*,
which is what lets it be tested without a database and re-pointed at a different store without an
edit (ADR-0002).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Final

from sqlalchemy import event
from sqlalchemy.engine import ExceptionContext
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from tailorcraft.infrastructure.settings import Settings

# The attributes a Postgres error carries that name SCHEMA OBJECTS rather than data: which
# constraint fired, on which table and column. Every one is an identifier this codebase chose in a
# migration, so every one is safe to keep — and they are exactly what a person diagnosing a failed
# write needs. asyncpg exposes them on its `PostgresError`; `getattr` keeps this driver-agnostic.
_IDENTIFIER_FIELDS: Final = (
    "schema_name",
    "table_name",
    "column_name",
    "data_type_name",
    "constraint_name",
)


def create_engine(settings: Settings) -> AsyncEngine:
    """Build the async engine.

    `pool_pre_ping` costs one cheap round trip per checkout and buys immunity to the single most
    annoying production symptom there is: a connection the pool believes is alive because nothing
    told it that PostgreSQL restarted, surfacing as one failed request after every deploy.

    **A failing statement must not carry its data out of the process** (Constitution §8, AC-21). The
    values this application writes are a CV being inserted (1.1's `extracted_text`) and a tailored CV
    and cover letter being saved (1.3). In the worker, a failed save of a succeeded run is *allowed*
    to escape `run_tailoring` (G-28). It is not redelivered: Celery acks a task that raises, and the
    stale-run sweep records the run. It escapes so that it is seen. Celery's "raised unexpected" line
    renders the exception with its whole traceback, and Sentry ships every exception in the chain.
    That is also exactly how its data would leave the process. Measured at slice 1.3's `/verify`,
    the data leaks through **three layers**, and each needs its own answer:

    1. **SQLAlchemy's own rendering** — a `[parameters: (...)]` line under the SQL in every
       `DBAPIError`'s `str()`. `hide_parameters=True` removes it. It is the layer SQLAlchemy owns,
       so it is kept, and it is **necessary and not sufficient**: it is the only one of the three a
       setting can reach.
    2. **The driver's own message**, which SQLAlchemy copies into its message verbatim. asyncpg
       quotes the offending value when it cannot encode an argument (`invalid input for query
       argument $1: '<value>'`), and PostgreSQL's `DETAIL: Failing row contains (...)` on a CHECK or
       NOT NULL violation is **the whole row** — tailored CV included.
    3. **The `raise … from` chain.** SQLAlchemy raises its wrapper *from* the driver's exception,
       which is raised from asyncpg's own. `traceback.format_exception` — what Celery logs — and
       Sentry's chained-exception walker both render every link, so a clean `str(exc)` on the
       outermost exception proves nothing. One flag covers layer 1; a test that checked only
       `str(exc)` would have certified that as the fix, and did, until it was made to check the
       rendered chain.

    Layers 2 and 3 are handled by `_withhold_driver_message`, registered on **this engine** rather
    than on SQLAlchemy's `Engine` class. A class-level listener would rewrite every database error in
    every process that imports SQLAlchemy — Alembic, the eval CLI, a REPL — as a side effect of an
    import, and take their debugging detail with it. Scoped here, it covers exactly the engines this
    application builds for the API, the worker and the test suite (whose session engine comes from
    this function for that reason). **There is no setting that turns it off**, for the reason
    ADR-0012's address policy has none: a privacy guard with an off switch is a guard somebody
    switches off while chasing a bug and forgets.

    Out of this function's reach, stated so nobody assumes otherwise: **PostgreSQL's server log**
    records the same `DETAIL` line with the failing row at the default `log_error_verbosity`. That
    is the database container's configuration, not the application's, and is set to `terse` there.
    """
    engine = create_async_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=5,
        echo=False,
        hide_parameters=True,
    )
    # `sync_engine` because events are dispatched by the synchronous core an `AsyncEngine` wraps.
    event.listen(engine.sync_engine, "handle_error", _withhold_driver_message)
    return engine


def _withhold_driver_message(context: ExceptionContext) -> DBAPIError | None:
    """Replace a wrapped driver error with one that says *what* failed and never *with what*.

    SQLAlchemy calls this for every exception raised through the engine, after it has built its
    `DBAPIError` and before it raises. What survives is what diagnosis needs and nothing that can be
    data: the driver exception's fully-qualified type, the SQLSTATE, the schema identifiers above,
    and the SQL text (with placeholders — every statement this codebase issues binds its values).
    What goes is the driver's message, whose primary line can quote a value
    (`invalid input syntax for type uuid: "…"`) and whose `DETAIL` can quote a row. **The bound
    parameters go too:** the replacement is built with `params=None`.

    That last point matters even though nothing renders `.params` today. `hide_parameters=True`
    only keeps them out of `str()`; the values would still sit on the exception as an attribute,
    which is data. Three ordinary things would carry it out:
      - `DBAPIError.__reduce__` pickles `params`, for a result backend or a process boundary;
      - anything walking `vars(exc)`, such as a Sentry `before_send` or an exception renderer;
      - a future `log.warning(..., params=exc.params)` written by someone debugging a failed save.
    With `None`, the claim above is true of the object, not only of its message. An allow-list of
    identifiers rather than a scrub of the message, because a scrub is a bet on every message format
    PostgreSQL and asyncpg will ever emit, and the extraction sweep already recorded losing that
    kind of bet.

    **The replacement keeps the family.** It is built as `type(wrapped)`, so an `IntegrityError`
    stays an `IntegrityError` and every `except SQLAlchemyError` — the routers' G-13 and G-14
    handlers among them — still catches it. `connection_invalidated` is carried over because
    `pool_pre_ping` reads it off the raised error to decide whether to recycle the connection.

    **The chain is cut by mutating the driver exception, and that is the only place it can be cut.**
    SQLAlchemy raises whatever this returns `from context.original_exception`, so that exception is
    in the rendered chain no matter what is returned; its message is replaced and its own `__cause__`
    and `__context__` (asyncpg's exception, whose `__str__` appends the `DETAIL` from attributes
    rather than `args`) are dropped, which makes the original unreachable from any rendering. The
    object being mutated was created by the dialect for this one raise and is referenced by nothing
    else.

    A non-`DBAPIError` (`None` here — an exception SQLAlchemy chose not to wrap, such as a
    cancellation) is left alone: it carries no driver message, and returning `None` tells SQLAlchemy
    to raise its original. Errors raised while *connecting* do not pass through here at all: measured
    against a wrong password, asyncpg's `InvalidPasswordError` reaches the caller unwrapped, prose
    intact. That is acceptable rather than a gap — no statement has run, so there are no bound values
    or rows to quote; the message names the database user and nothing a user uploaded.
    """
    wrapped = context.sqlalchemy_exception
    if not isinstance(wrapped, DBAPIError):
        return None

    driver_error = context.original_exception
    # asyncpg's own exception, when the dialect translated it; the identifiers live there.
    source = driver_error.__cause__ or driver_error

    facts = []
    sqlstate = getattr(driver_error, "sqlstate", None) or getattr(source, "sqlstate", None)
    if sqlstate:
        facts.append(f"sqlstate={sqlstate}")
    for field in _IDENTIFIER_FIELDS:
        value = getattr(source, field, None)
        if value:
            facts.append(f"{field}={value}")
        # Carried over as attributes too, not only into the message, because cutting the chain
        # below makes asyncpg's exception — the only object holding them — unreachable, and a
        # repository that must recognise *which* constraint refused a write (`violated_constraint`)
        # would otherwise be left parsing this function's own prose. Same allow-list as the
        # message: identifiers this codebase chose in a migration, never data. Set on every field,
        # `None` included, so the attribute's absence can never be mistaken for a missing listener.
        setattr(driver_error, field, value if isinstance(value, str) else None)

    source_type = f"{type(source).__module__}.{type(source).__qualname__}"
    driver_error.args = (
        f"{source_type} [{', '.join(facts)}] driver message withheld: it can quote bound values "
        "and whole rows",
    )
    driver_error.__cause__ = None
    driver_error.__context__ = None
    driver_error.__suppress_context__ = True

    return type(wrapped)(
        wrapped.statement,
        # Not `wrapped.params`. A tailored CV is a bound value here, and a hidden attribute is still
        # an attribute. The docstring lists the three ways it would otherwise leave the process.
        None,
        driver_error,
        hide_parameters=True,
        connection_invalidated=wrapped.connection_invalidated,
        code=wrapped.code,
        ismulti=wrapped.ismulti,
    )


def violated_constraint(exc: DBAPIError) -> str | None:
    """The name of the constraint that refused a statement, or `None` if none is known.

    **This is how a repository tells one refusal from another** — `uq_identity_user_email` from
    `ck_identity_user_email_normalized`, say — and it is the only sanctioned way. Never parse the
    message: the driver's message is withheld by `_withhold_driver_message` precisely because it can
    quote data, and the replacement is prose for a person, not a format for a program.

    Reads the attribute `_withhold_driver_message` copies onto `exc.orig` from asyncpg's
    `constraint_name` (the SQLAlchemy dialect's adapted error carries only `sqlstate`/`pgcode`, and
    the asyncpg exception behind it is cut from the chain). A constraint name is a schema identifier
    chosen in a migration, so returning it leaks nothing.

    `None` on an engine without the listener, which in this codebase means a bug in how the engine
    was built — a caller comparing the result with a name then falls through to re-raising, which is
    the safe direction: an unrecognised refusal propagates, it is never mistranslated.
    """
    name = getattr(exc.orig, "constraint_name", None)
    return name if isinstance(name, str) else None


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Build the session factory.

    `expire_on_commit=False` because objects are read after the commit that saved them — with the
    default, every attribute access after a commit triggers a lazy refresh, which in an async
    session raises rather than quietly issuing SQL. Loud, but only once you hit it.
    """
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


@asynccontextmanager
async def session_scope(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """One transaction, committed on success and rolled back on any exception."""
    async with factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        else:
            await session.commit()
