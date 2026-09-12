"""Engine-level persistence tests for `infrastructure/persistence/database.py::create_engine`.

**RED for a `/verify`-round-1 finding, not for a slice under `/implement`** (G-28 / AC-21 of
`docs/specs/tailoring-generate-documents/feature-spec.md`): `create_engine` builds the async engine
without `hide_parameters=True`, so SQLAlchemy renders every bound parameter into a `DBAPIError`'s own
`str()`. Reproduced by hand at `/verify`: a `SELECT CAST(:p AS integer)` with a sentinel string bound
as `p`, run through this project's own `create_engine(get_settings())`, raised a `DBAPIError` with the
sentinel sitting in `str(exc)` — not merely in `repr(exc)`.

Why this is more than a formatting nit: in the worker, `ExecuteTailoringRun` step 7 saves a
succeeded run's `tailored_cv` and `cover_letter` through `CommittingTailoringRunRepository`
(`infrastructure/tasks/container.py`). If that `UPDATE` fails at the database — a dropped
connection, a CHECK violation — the exception is left to escape `run_tailoring` on purpose (G-28: so
`task_acks_late` can redeliver). Celery logs "raised unexpected: <exc>" and Sentry captures the
exception *value*, and with `hide_parameters` unset, that value can contain the tailored document
itself. `include_local_variables=False` (Sentry) does not help here — the leak is in the exception's
own message, not in a frame's locals.

This test goes through the **real** engine factory rather than a hand-built exception, because a fake
raising a constructed `DBAPIError` cannot discriminate whether `hide_parameters` was actually passed
to `create_async_engine` — only the real driver's own error-rendering path can prove that one way or
the other.
"""

from __future__ import annotations

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

    # The positive proof the assertion below cannot pass vacuously: a statement that quietly
    # succeeded, or failed with some other exception type entirely, would say nothing about
    # `hide_parameters`.
    exc = exc_info.value
    assert isinstance(exc, DBAPIError)

    assert _SENTINEL not in str(exc), (
        "the bound parameter leaked into the DBAPIError's own message — create_engine is missing "
        "hide_parameters=True (G-28, AC-21)"
    )
