"""Engine-level persistence tests for `infrastructure/persistence/database.py::create_engine`.

**RED for a `/verify`-round-1 finding, not for a slice under `/implement`** (G-28 / AC-21 of
`docs/specs/tailoring-generate-documents/feature-spec.md`): SQLAlchemy renders every bound parameter
into a `DBAPIError`'s own `str()` by default. Reproduced by hand at `/verify`: a
`SELECT CAST(:p AS integer)` with a sentinel string bound as `p`, run through this project's own
`create_engine(get_settings())`, raised a `DBAPIError` with the sentinel sitting in `str(exc)`.

**Strengthened after api-dev's round-1 `hide_parameters=True` attempt turned out not to be enough**,
and this file's own weaker assertion (checking only `str(exc)`) is why nobody noticed at first:
`hide_parameters=True` deletes SQLAlchemy's own `[parameters: (...)]` line, which is genuinely all
`str(exc)` shows — but the underlying asyncpg exception is still chained onto the `DBAPIError` (via
`raise ... from orig`), and *that* exception's own message still quotes the value verbatim
(`invalid input for query argument $1: '<value>'`). `traceback.format_exception(exc)` walks that
chain, and that rendering — not `str(exc)` — is what Celery's "raised unexpected: <exc>" line and
Sentry's own exception-walking both actually produce. A test that only checks `str(exc)` can go green
on a fix that leaves the real leak completely intact, which is exactly what happened here. Decided at
`/verify`: `create_engine` needs to register a `handle_error` listener that rebuilds the error with
the driver's own text withheld and the `__cause__`/`__context__` chain cut, while preserving the
exception type, sqlstate, table/constraint names and the SQL — a stronger contract than a bare engine
flag can express, which is what this test now checks for rather than assumes.

Why this is more than a formatting nit: in the worker, `ExecuteTailoringRun` step 7 saves a
succeeded run's `tailored_cv` and `cover_letter` through `CommittingTailoringRunRepository`
(`infrastructure/tasks/container.py`). If that `UPDATE` fails at the database — a dropped
connection, a CHECK violation — the exception is left to escape `run_tailoring` on purpose (G-28).
Celery 5.6.3 ACKs a task that raises (`task_acks_on_failure_or_timeout=True`), so nothing redelivers
it; the run is left `running`, and the stale-run sweep (G-25', `tasks/tailoring_sweep.py`) is what
recovers it a beat tick later. Celery logs "raised unexpected: <exc>" (which renders the full
chain, not merely `str(exc)`) and Sentry walks the same chain when it captures the exception, so
either one can carry the tailored document itself unless the chain is actually cut, not merely
`str()`-quiet. `include_local_variables=False` (Sentry) does not help here — the leak is in the
exception's own message and its chained cause, not in a frame's locals.

This test goes through the **real** engine factory rather than a hand-built exception, because a fake
raising a constructed `DBAPIError` cannot discriminate whether the sanitizing behaviour was actually
wired into `create_engine` — only the real driver's own error-rendering path can prove that one way or
the other.
"""

from __future__ import annotations

import traceback

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from tailorcraft.infrastructure.persistence.database import create_engine
from tailorcraft.infrastructure.settings import Settings

_SENTINEL = "QA_ENGINE_PARAM_LEAK_SENTINEL_9f3c2b"


async def test_a_statement_failing_at_the_database_never_renders_its_bound_parameter_in_the_exception_message(
    settings: Settings,
) -> None:
    """Built through `create_engine(settings)` — the exact factory `main.py`'s lifespan and
    `tasks/container.py::tailoring_use_case` both call — against the real `tailorcraft_test`
    database, never a fake.

    `CAST(:p AS integer)` fails at Postgres (an `invalid_text_representation` error), with the
    sentinel bound as `p` — the identical statement shape used to reproduce the leak at `/verify`,
    chosen because it needs no table, no transaction and no cleanup: nothing here survives even a
    dropped connection, let alone a rolled-back transaction.
    """
    engine = create_engine(settings)
    try:
        async with engine.connect() as conn:
            with pytest.raises(DBAPIError) as exc_info:
                await conn.execute(text("SELECT CAST(:p AS integer)"), {"p": _SENTINEL})
    finally:
        await engine.dispose()

    # The positive proof the assertions below cannot pass vacuously: a statement that quietly
    # succeeded, or failed with some other exception type entirely, would say nothing about the
    # sanitizer. `isinstance` rather than `type(...) is`, on purpose: the router's G-13/G-14 handlers
    # catch `SQLAlchemyError`, so a sanitizer that rebuilt the error as some unrelated type would
    # silently turn those 503s into 500s — this is the check that would catch that regression too.
    exc = exc_info.value
    assert isinstance(exc, DBAPIError)

    # Celery's "raised unexpected: <exc>" and Sentry's own capture both render more than `str(exc)`
    # — they walk `__cause__`/`__context__`, which is exactly what a bare `hide_parameters=True`
    # leaves untouched: it deletes SQLAlchemy's own `[parameters: ...]` line but leaves the chained
    # asyncpg exception, whose own message still quotes the value, fully intact underneath it.
    rendered_chain = "".join(traceback.format_exception(exc))

    assert _SENTINEL not in str(exc), (
        "the bound parameter leaked into the DBAPIError's own message (G-28, AC-21)"
    )
    assert _SENTINEL not in rendered_chain, (
        "the bound parameter leaked into the exception's rendered chain (__cause__/__context__) — "
        "this is what Celery logs and what Sentry walks, and hiding it from str(exc) alone does not "
        "reach it (G-28, AC-21)"
    )
