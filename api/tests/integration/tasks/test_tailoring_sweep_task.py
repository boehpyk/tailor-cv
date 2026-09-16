"""Task tests for `infrastructure/tasks/tailoring_sweep.py::abandon_stale_tailoring_runs` (V5f,
test-after, /verify round 1).

Mirrors `test_tailoring_task.py`'s structure and its two invocation techniques, for the identical
sync/async-bridge reason documented there — `abandon_stale_tailoring_runs` is a **synchronous**
Celery task that bridges into async application code with `asyncio.run(_sweep())`
(`tasks/tailoring_sweep.py`'s own module docstring, and `tasks/container.py`'s):

1. **Where no visibility into this suite's own uncommitted data is needed** — inspecting the task
   object's declared retry behaviour, and one real end-to-end call through the task's own
   `asyncio.run()` bridge to check its log line — the tests below either call
   `abandon_stale_tailoring_runs()` literally (with `container.get_settings` patched to this suite's
   `settings`, so the fresh connection it opens lands on `tailorcraft_test`, never the dev database)
   or inspect the task object without executing it at all. A plain `def test_...` (not `async def`)
   is required for the literal call, exactly as `test_tailoring_task.py`'s own docstring explains:
   `asyncio.run()` refuses to start a second loop on top of pytest-asyncio's session-scoped one while
   an `async def` test's body is running.

2. **Where the task must see rows this test created** — the end-to-end abandon/leave-untouched test
   and the partial-batch-failure test — `_sweep` (the coroutine `abandon_stale_tailoring_runs` hands
   to `asyncio.run()`) is called directly, `await`ed from an already-async test, with
   `abandon_stale_runs_use_case` (as imported into `infrastructure.tasks.tailoring_sweep`) patched to
   yield the worker's real `_build_sweep_use_case(settings, session)` bound to *this test's own*
   session — the identical technique `test_tailoring_task.py`'s `_bind_task_to_this_sessions_worker`
   uses, one layer down.

Every run built below needs no real `BaseCv` or `JobPosting`: unlike `ExecuteTailoringRun`,
`AbandonStaleTailoringRuns` never looks either up (its own class docstring, and the mapping module's
own note that `base_cv_id`/`job_posting_id` carry no foreign key) — a random UUID is exactly as good
as a real one for every test here.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import delete
from sqlalchemy import text as sql_text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from tailorcraft.domain.identity.guest_session import GuestSession
from tailorcraft.domain.identity.value_objects import GuestSessionId
from tailorcraft.domain.intake.value_objects import BaseCvId
from tailorcraft.domain.posting.value_objects import JobPostingId
from tailorcraft.domain.tailoring.tailoring_run import TailoringRun
from tailorcraft.domain.tailoring.value_objects import (
    TailoringFailureReason,
    TailoringRunId,
    TailoringRunStatus,
)
from tailorcraft.infrastructure.clock import FixedClock

# Importing these two mapping modules is what runs `mapper_registry.map_imperatively(...)` for
# `GuestSession` and `TailoringRun` as an import side effect — collection order alone cannot be
# trusted to do this first when this file is run alone (`make test file=...`), and each
# `repositories.*` import below fails at ITS OWN top level otherwise (`test_tailoring_task.py`'s
# identical comment names the exact `AttributeError`).
from tailorcraft.infrastructure.persistence.mapping.identity.guest_session import (
    guest_session_table,
)
from tailorcraft.infrastructure.persistence.mapping.tailoring.tailoring_run import (
    tailoring_run_table,  # noqa: F401
)
from tailorcraft.infrastructure.persistence.repositories.identity.guest_session import (
    SqlAlchemyGuestSessionRepository,
)
from tailorcraft.infrastructure.persistence.repositories.tailoring.tailoring_run import (
    SqlAlchemyTailoringRunRepository,
)
from tailorcraft.infrastructure.settings import Settings
from tailorcraft.infrastructure.tasks import container as sweep_container
from tailorcraft.infrastructure.tasks import tailoring_sweep as sweep_task_module
from tailorcraft.infrastructure.tasks.tailoring_sweep import abandon_stale_tailoring_runs

# --- Test helpers ------------------------------------------------------------------------------


async def _persist_owner(
    session: AsyncSession, clock: FixedClock, *, token_hash: str
) -> GuestSession:
    repo = SqlAlchemyGuestSessionRepository(session)
    owner = GuestSession.start(
        id=repo.next_identity(), token_hash=token_hash, at=clock.now(), ttl_hours=24
    )
    await repo.add(owner)
    return owner


def _real_now() -> datetime:
    """Whole-second, matching the `Clock` contract (ADR-0007) — but deliberately **not** the
    suite's frozen `clock` fixture. `_build_sweep_use_case` (`tasks/container.py`) always binds
    `clock=SystemClock()`, the same as the worker's real composition root, with no seam to inject a
    test double — so every timestamp fed to a test that exercises the real `_sweep()` path (every
    test below that calls it) must be anchored to actual wall-clock time. Building "5 seconds ago"
    off `FixedClock`'s frozen instant instead would make it 5 seconds after whatever date the fixture
    is frozen at — which `SystemClock.now()` reads as either long past or (if the fixture is frozen
    in the future) never stale at all, not "fresh" — measured here after a first version of this file
    kept `clock.now() - timedelta(seconds=5)` and got `abandoned=2` for a run meant to be untouched.
    """
    return datetime.now(UTC).replace(microsecond=0)


def _running_run(
    runs: SqlAlchemyTailoringRunRepository,
    owner_id: GuestSessionId,
    *,
    requested_at_seconds_ago: int,
    started_at_seconds_ago: int,
    now: datetime,
) -> TailoringRun:
    run = TailoringRun.request(
        id=runs.next_identity(),
        guest_session_id=owner_id,
        base_cv_id=BaseCvId(value=uuid4()),
        job_posting_id=JobPostingId(value=uuid4()),
        requested_at=now - timedelta(seconds=requested_at_seconds_ago),
    )
    run.mark_started(now - timedelta(seconds=started_at_seconds_ago))
    return run


def _bind_sweep_to_this_sessions_worker(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, session: AsyncSession
) -> None:
    """The technique this module's docstring calls approach 2: `_sweep` — the coroutine
    `abandon_stale_tailoring_runs` hands to `asyncio.run()` — is left untouched; only the composition
    root it opens (`abandon_stale_runs_use_case`) is replaced with one bound to *this test's own*
    session, so rows this test just added are visible to it without opening a second, genuinely
    separate connection.
    """

    @asynccontextmanager
    async def _fake_sweep_use_case() -> AsyncIterator[tuple[Any, AsyncSession]]:
        yield sweep_container._build_sweep_use_case(settings, session), session

    monkeypatch.setattr(sweep_task_module, "abandon_stale_runs_use_case", _fake_sweep_use_case)


async def _reread(connection: AsyncConnection, run_id: TailoringRunId) -> TailoringRun:
    """Read a run back through a *brand-new* `AsyncSession` bound to the same test connection —
    proving the sweep's writes are genuinely durable in this transaction, not merely visible through
    the writer's own identity map. Same `async_sessionmaker` construction as `conftest.py`'s own
    `session` fixture."""
    factory = async_sessionmaker(
        bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )
    async with factory() as verify_session:
        return await SqlAlchemyTailoringRunRepository(verify_session).get(run_id)


# --- Structural: no Celery-level retry, and the body never calls one ----------------------------


def test_the_task_declares_no_autoretry_for_retry_backoff_or_max_retries_override() -> None:
    """G-35: the next tick is the retry, so nothing here may declare a second recovery layer.

    Mirrors `test_tailoring_task.py`'s identical inspection of `run_tailoring`. `max_retries` carries
    a class-level default (`3`) whether or not a task customises it, so the only way to prove THIS
    task did not override it is to compare against that same class default, never a bare literal.
    `ignore_result=True` is asserted directly, since it is exactly what makes G-25' log the counts
    instead of parking them in Redis (the task's own module docstring: "beat publishes and never
    looks back"). The source-text check is the belt to the attribute checks' suspenders: `bind=False`
    means the function has no `self` to call `.retry()` on at all, so a real `.retry(` call could
    only appear as a call on the module-level task object itself — a substring check catches it
    either way and turns "just add one for safety" into a red test rather than a silent second
    layer.
    """
    from celery.app.task import Task

    assert not hasattr(abandon_stale_tailoring_runs, "autoretry_for")
    assert not hasattr(abandon_stale_tailoring_runs, "retry_backoff")
    assert abandon_stale_tailoring_runs.max_retries == Task.max_retries
    assert abandon_stale_tailoring_runs.ignore_result is True

    source = inspect.getsource(abandon_stale_tailoring_runs.run)
    assert ".retry(" not in source, "the task body must never call retry() — the next tick is it"


# --- End to end against the database: a stale run is abandoned, a fresh one is untouched --------


async def test_a_stale_running_run_is_abandoned_and_a_fresh_one_is_untouched(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    session: AsyncSession,
    connection: AsyncConnection,
    clock: FixedClock,
) -> None:
    owner = await _persist_owner(session, clock, token_hash="30" * 32)
    runs = SqlAlchemyTailoringRunRepository(session)
    now = _real_now()
    stale = _running_run(
        runs, owner.id, requested_at_seconds_ago=1_000, started_at_seconds_ago=400, now=now
    )
    fresh = _running_run(
        runs, owner.id, requested_at_seconds_ago=10, started_at_seconds_ago=5, now=now
    )
    await runs.add(stale)
    await runs.add(fresh)
    await session.commit()
    _bind_sweep_to_this_sessions_worker(monkeypatch, settings, session)

    result = await sweep_task_module._sweep()

    # `>=`, not `==`: `list_stale_running` sees every genuinely committed `running` row in
    # `tailorcraft_test`, not only this test's own two. A leftover row committed by an earlier,
    # interrupted run of `test_invoking_the_real_task_logs_swept_and_skipped_counts_and_a_duration_
    # only` below (which commits through its own separate engine, outside any transactional
    # rollback) would push this count above 1 and break an exact equality on every later run — the
    # id-scoped checks just below are what actually proves THIS test's own stale run was the one
    # abandoned.
    assert result.abandoned >= 1
    assert result.skipped == 0

    reloaded_stale = await _reread(connection, stale.id)
    reloaded_fresh = await _reread(connection, fresh.id)
    assert reloaded_stale.status is TailoringRunStatus.FAILED
    assert reloaded_stale.failure_reason is TailoringFailureReason.ABANDONED
    assert reloaded_fresh.status is TailoringRunStatus.RUNNING
    assert reloaded_fresh.failure_reason is None


# --- G-35: a later run's save failing at the database must not undo an earlier one's abandonment ---


async def test_a_real_database_failure_on_a_later_run_leaves_an_earlier_run_durably_abandoned(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    session: AsyncSession,
    connection: AsyncConnection,
    clock: FixedClock,
) -> None:
    """G-35: the batch commits per run through `CommittingTailoringRunRepository.save`, one save per
    abandoned run — so a database failure recording the SECOND (later, per oldest-first ordering) run
    must not take the FIRST (earlier) run's already-committed abandonment back with it.

    Reproduced with a REAL constraint violation, `test_tailoring_task.py`'s own technique for its
    G-28/AC-21 test — never a mocked repository, per this agent's standing rule against mocking the
    database. There is no document text on this path (`RUNNING` rows never carry one, per the
    mapping's own CHECK) to key a constraint off the way that test does, so this one keys off the
    later run's own `id` *and* the specific write the sweep is about to make: `NOT (id = '<that id>'
    AND status = 'failed')`. The row satisfies this the moment it is seeded (`status = 'running'`),
    exactly as `ALTER TABLE ... ADD CONSTRAINT` requires of every existing row, and is violated only
    by the later run's own `UPDATE ... SET status = 'failed'` — the sweep's `save()` for that run,
    and nothing else in this test. A naive `id <> '<that id>'` constraint was tried first and rejected
    at `ADD CONSTRAINT` time itself, before the sweep ever ran: the row already existing with that id
    violates it immediately, which is exactly why the predicate has to name the future write, not the
    identity alone.
    """
    owner = await _persist_owner(session, clock, token_hash="31" * 32)
    runs = SqlAlchemyTailoringRunRepository(session)
    now = _real_now()
    earlier = _running_run(
        runs, owner.id, requested_at_seconds_ago=1_000, started_at_seconds_ago=900, now=now
    )
    later = _running_run(
        runs, owner.id, requested_at_seconds_ago=800, started_at_seconds_ago=700, now=now
    )
    # Captured now, as plain values — not read off `earlier`/`later` after the failure below, which
    # leaves `session` needing a rollback and its objects expired. Accessing an expired attribute
    # would try a lazy reload through this same (synchronous, already-failed) context and raise
    # `MissingGreenlet`, not a meaningful assertion failure.
    earlier_id = earlier.id
    later_id = later.id
    await runs.add(earlier)
    await runs.add(later)
    await session.commit()

    constraint_name = "qa_v5f_sweep_batch_guard"
    await session.execute(
        sql_text(
            f"ALTER TABLE tailoring_run ADD CONSTRAINT {constraint_name} "
            f"CHECK (NOT (id = '{later_id.value}' AND status = 'failed'))"
        )
    )
    await session.commit()
    _bind_sweep_to_this_sessions_worker(monkeypatch, settings, session)

    try:
        with pytest.raises(DBAPIError) as exc_info:
            await sweep_task_module._sweep()
    finally:
        await session.rollback()
        await session.execute(
            sql_text(f"ALTER TABLE tailoring_run DROP CONSTRAINT IF EXISTS {constraint_name}")
        )
        await session.commit()

    # The positive proof the rest of this test cannot pass vacuously: the batch's second save must
    # actually have failed at the database, and the exception must have escaped `_sweep` rather than
    # being swallowed — `pytest.raises` above already proves the second half.
    exc = exc_info.value
    assert isinstance(exc, DBAPIError)
    assert constraint_name in str(exc), (
        "debuggability regression: the constraint's own name must survive, or a real production "
        "incident becomes unattributable to the rule that actually fired"
    )

    reloaded_earlier = await _reread(connection, earlier_id)
    assert reloaded_earlier.status is TailoringRunStatus.FAILED, (
        "G-35: the earlier run's save committed through its own transaction and must survive a "
        "later run's failure in the same batch"
    )
    assert reloaded_earlier.failure_reason is TailoringFailureReason.ABANDONED


# --- MAJOR 2 (/verify slice 1.4): a conflict on one run must not crash the batch on the next -----


async def test_a_version_conflict_on_the_earlier_run_does_not_crash_the_sweep_on_the_later_run(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    session: AsyncSession,
    connection: AsyncConnection,
    clock: FixedClock,
) -> None:
    """MAJOR 2 — before commit 6ccab15, `CommittingTailoringRunRepository.save`
    (`infrastructure/tasks/container.py`) rolled the whole session back on
    `TailoringRunConcurrentlyModified`. A root-boundary `AsyncSession.rollback()` restores via
    `_restore_snapshot(dirty_only=False)`, which expires **every** instance the session holds, not
    only the one whose save just failed — so the next iteration of `AbandonStaleTailoringRuns.
    __call__`'s loop (`application/tailoring/abandon_stale_tailoring_runs.py`) calls
    `run.is_stale(now, stale_after)` on the *next* candidate, a plain synchronous attribute read
    (`domain/tailoring/tailoring_run.py::is_stale` touches `self._status` and `self._started_at`)
    with no `await` in front of it. On an expired attribute of an object bound to an
    `AsyncSession`, that read tries an implicit lazy-refresh, and outside any `greenlet_spawn`
    context (which only wraps SQLAlchemy's own awaited calls, never a bare Python property access
    made from ordinary application code) that refresh raises `sqlalchemy.exc.MissingGreenlet`
    instead of resuming the sweep. The fix (6ccab15) flushes the failing save inside a SAVEPOINT
    instead: a nested boundary's snapshot restore is `dirty_only=True`, so only the one run that
    was modified inside the SAVEPOINT is expired, and the sweep's other loaded candidates read
    without I/O.

    Reproduced with two real, committed `running` rows and a genuine optimistic-lock conflict — no
    mocked repository, matching this suite's own G-35 constraint test just above. `earlier`
    (`list_stale_running` orders `started_at` ascending, so it is processed first) has its `version`
    column bumped by a raw `UPDATE` issued on THIS test's own session/connection — the T15/AC-7
    two-session technique's single-session cousin: the row no longer matches what the identity-mapped
    ORM object believes it loaded, so the sweep's own `save(earlier)` fails exactly as a genuine
    concurrent writer (another worker, or a redelivered task) would make it fail. `later`'s row is
    left completely untouched; the only thing wrong with it afterward is that the Python object
    describing it now sits in a session `rollback()` just expired.

    **Expected once MAJOR 2 is fixed:** `_sweep()` does not raise; `earlier` is counted a conflict
    and its row stays `running` (the conflicting write never landed); `later` is abandoned normally.
    Today it is expected to fail with `MissingGreenlet` before any of those assertions run.
    """
    owner = await _persist_owner(session, clock, token_hash="34" * 32)
    runs = SqlAlchemyTailoringRunRepository(session)
    now = _real_now()
    earlier = _running_run(
        runs, owner.id, requested_at_seconds_ago=1_000, started_at_seconds_ago=900, now=now
    )
    later = _running_run(
        runs, owner.id, requested_at_seconds_ago=800, started_at_seconds_ago=700, now=now
    )
    earlier_id = earlier.id
    later_id = later.id
    await runs.add(earlier)
    await runs.add(later)
    await session.commit()

    # The version bump that makes the sweep's own later `save(earlier)` fail with a REAL
    # `StaleDataError` → `TailoringRunConcurrentlyModified` — issued on the same session the sweep
    # will use, so it is visible to the sweep's `list_stale_running` SELECT within the same
    # transaction without needing a commit of its own.
    await session.execute(
        sql_text("UPDATE tailoring_run SET version = version + 1 WHERE id = :id"),
        {"id": str(earlier_id.value)},
    )
    _bind_sweep_to_this_sessions_worker(monkeypatch, settings, session)

    result = await sweep_task_module._sweep()

    assert result.abandoned == 1
    assert result.conflicts == 1
    assert result.skipped == 0

    reloaded_earlier = await _reread(connection, earlier_id)
    assert reloaded_earlier.status is TailoringRunStatus.RUNNING, (
        "the conflicting write must never have landed — the row the raw UPDATE bumped is the only "
        "row this test changed directly"
    )

    reloaded_later = await _reread(connection, later_id)
    assert reloaded_later.status is TailoringRunStatus.FAILED, (
        "the earlier run's conflict must not crash the batch before the later, untouched-by-conflict "
        "run gets its turn"
    )
    assert reloaded_later.failure_reason is TailoringFailureReason.ABANDONED


# --- The sweep never builds an LLM adapter -------------------------------------------------------


async def test_the_sweep_never_constructs_an_llm_adapter(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, session: AsyncSession, clock: FixedClock
) -> None:
    """`AbandonStaleTailoringRuns` records that a call was lost; it never makes one
    (`container.py::abandon_stale_runs_use_case`'s own docstring: "not an economy"). Patches
    `container.GeminiLlm` to fail the test outright if the composition root ever calls it — a
    structural proof rather than an absence-of-evidence one, and it would catch a future edit that
    wired an `LlmPort` into `_build_sweep_use_case` by mistake.
    """

    def _fail_if_constructed(settings: Settings) -> Any:
        pytest.fail("AbandonStaleTailoringRuns must never construct an LLM adapter")

    monkeypatch.setattr(sweep_container, "GeminiLlm", _fail_if_constructed)
    _bind_sweep_to_this_sessions_worker(monkeypatch, settings, session)

    # No count assertion here on purpose: this test seeds no rows of its own, so `result.abandoned`
    # reflects EVERY genuinely committed stale `running` row visible in `tailorcraft_test` — a
    # leftover from an interrupted run elsewhere in this file would make an exact `== 0` fragile for
    # a reason that has nothing to do with what this test actually checks. The behaviour under test
    # is `_fail_if_constructed` never firing, which `pytest.fail` above already proves on its own:
    # a call to `_sweep()` that completes at all, with no exception raised, is the whole proof.
    await sweep_task_module._sweep()


# --- G-35 (verify round 2): the task's OWN error branch, exercised only by calling the real task ---


class _SweepDatabaseFailure(RuntimeError):
    """A distinctively-named exception so `error_type=type(exc).__name__` in the sweep's failure log
    line can be told apart from any other `RuntimeError`-shaped thing already in this suite's
    caplog."""


_SWEEP_FAILURE_LOG_LEAK_SENTINEL = "QA_V5F_SWEEP_FAILURE_LOG_LEAK_SENTINEL_8a1c3d"


def test_a_database_failure_through_the_real_task_logs_the_exceptions_type_and_reraises(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Every other test in this file calls `_sweep()` directly, so none of them ever runs
    `abandon_stale_tailoring_runs`'s OWN `except Exception:` branch (`tasks/tailoring_sweep.py:62-
    68`) — only the real task function's body wraps `asyncio.run(_sweep())` in a `try`, so only a
    literal call to `abandon_stale_tailoring_runs()` itself can exercise it, the same distinction
    `test_invoking_the_real_task_logs_swept_and_skipped_counts_and_a_duration_only` below draws for
    the success path.

    `abandon_stale_runs_use_case` is patched (in this MODULE's namespace, `sweep_task_module`, the
    same technique `_bind_sweep_to_this_sessions_worker` uses one call site over) with a fake whose
    `__aenter__` itself raises — never a mocked repository, but there is no real-database failure
    left to reuse here either: `_sweep`'s only two lines are opening the use case and calling it, and
    the DB-failure shape (a constraint violation on `save`) is already exercised end to end by
    `test_a_real_database_failure_on_a_later_run_leaves_an_earlier_run_durably_abandoned` above. What
    this test adds is new is the TASK's own translation of whatever escapes `_sweep()` into one log
    line, which that other test cannot see because it calls `_sweep()` directly and never reaches
    the task body at all.

    Proven to discriminate by locally changing `error_type=type(exc).__name__` to
    `error=str(exc)` in `tasks/tailoring_sweep.py`: every other test in this suite still passes,
    because none of them goes through this branch, and this test's sentinel assertion goes red.
    """

    class _RaisingSweepUseCase:
        """A plain async context manager, not `@asynccontextmanager`: a generator function whose
        body unconditionally raises before its `yield` leaves that `yield` statically unreachable,
        which mypy `--strict` correctly flags — there is no generator shape to preserve here, since
        `_sweep`'s `async with abandon_stale_runs_use_case() as (sweep, session):` only ever needs
        `__aenter__` to raise."""

        async def __aenter__(self) -> tuple[Any, AsyncSession]:
            raise _SweepDatabaseFailure(
                f"simulated database failure carrying {_SWEEP_FAILURE_LOG_LEAK_SENTINEL}"
            )

        async def __aexit__(self, *exc_info: object) -> None:
            return None

    monkeypatch.setattr(
        sweep_task_module, "abandon_stale_runs_use_case", lambda: _RaisingSweepUseCase()
    )

    with caplog.at_level(logging.INFO), pytest.raises(_SweepDatabaseFailure):
        abandon_stale_tailoring_runs()

    assert "tailoring.stale_run_sweep_failed" in caplog.text
    assert _SweepDatabaseFailure.__name__ in caplog.text, (
        "the failure line must carry the exception's type — logging its message instead is exactly "
        "the change this test exists to catch"
    )
    assert re.search(r'"?duration_ms"?\s*[:=]', caplog.text), caplog.text
    assert _SWEEP_FAILURE_LOG_LEAK_SENTINEL not in caplog.text, (
        "G-35/Constitution §8: the failure line logs the exception's TYPE only, never its message — "
        "a database error's message can quote a row"
    )


# --- One log line, real end to end through the task's own asyncio.run() bridge ------------------


def test_invoking_the_real_task_logs_swept_and_skipped_counts_and_a_duration_only(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    clock: FixedClock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """G-25': one log line per tick, `swept_count`/`skipped_count`/`duration_ms` — and per
    Constitution §8, nothing about a run's contents. A genuinely separate connection is required
    here, for the identical reason `test_tailoring_task.py`'s two literal-bridge tests give: this
    suite's shared `session` fixture is bound to a SAVEPOINT that is never committed, so a second,
    genuinely different connection (which is what `abandon_stale_tailoring_runs`'s own
    `asyncio.run()` opens) cannot see anything written through it. So this test seeds through its own
    throwaway engine, commits for real, and deletes it again in a `finally` — `test_tailoring_task.py`
    module docstring's own technique, for the same reason.
    """
    monkeypatch.setattr(sweep_container, "get_settings", lambda: settings)

    async def _seed_a_stale_run() -> tuple[GuestSessionId, TailoringRunId]:
        seeding_engine = create_async_engine(settings.test_database_url, poolclass=None)
        try:
            async with AsyncSession(seeding_engine, expire_on_commit=False) as seeding_session:
                owner = await _persist_owner(seeding_session, clock, token_hash="32" * 32)
                runs = SqlAlchemyTailoringRunRepository(seeding_session)
                stale = _running_run(
                    runs,
                    owner.id,
                    requested_at_seconds_ago=1_000,
                    started_at_seconds_ago=400,
                    now=_real_now(),
                )
                await runs.add(stale)
                await seeding_session.commit()
                return owner.id, stale.id
        finally:
            await seeding_engine.dispose()

    async def _reread_status(run_id: TailoringRunId) -> TailoringRun:
        verify_engine = create_async_engine(settings.test_database_url, poolclass=None)
        try:
            async with AsyncSession(verify_engine, expire_on_commit=False) as verify_session:
                return await SqlAlchemyTailoringRunRepository(verify_session).get(run_id)
        finally:
            await verify_engine.dispose()

    async def _forget(owner_id: GuestSessionId) -> None:
        cleanup_engine = create_async_engine(settings.test_database_url, poolclass=None)
        try:
            async with AsyncSession(cleanup_engine, expire_on_commit=False) as cleanup_session:
                await cleanup_session.execute(
                    delete(guest_session_table).where(guest_session_table.c.id == owner_id)
                )
                await cleanup_session.commit()
        finally:
            await cleanup_engine.dispose()

    owner_id, stale_run_id = asyncio.run(_seed_a_stale_run())
    try:
        with caplog.at_level(logging.INFO):
            result = abandon_stale_tailoring_runs()
        reloaded_stale = asyncio.run(_reread_status(stale_run_id))
    finally:
        asyncio.run(_forget(owner_id))

    assert result is None, "ignore_result=True: the task must return None regardless of the outcome"
    # The id-scoped check that makes the loosened count assertion below meaningful: THIS test's own
    # stale run must actually be the one the sweep recorded, not merely "some count went up".
    assert reloaded_stale.status is TailoringRunStatus.FAILED
    assert reloaded_stale.failure_reason is TailoringFailureReason.ABANDONED

    assert "tailoring.stale_runs_swept" in caplog.text
    # structlog's renderer here is process-wide and picked once by whichever test configures it
    # first (`configure_logging`, `infrastructure/observability.py`) — console-formatted
    # (`swept_count=1`) if this file runs alone, JSON (`"swept_count": 1`) inside the full suite. The
    # regexes below match either rendering rather than assuming one, so this test does not depend on
    # what ran before it.
    #
    # `>= 1`, not `== 1`: this task call sweeps EVERY genuinely committed stale `running` row in
    # `tailorcraft_test`, so a leftover from an earlier interrupted run of this same test (this test
    # commits for real, through its own engine, outside any transactional rollback) would push the
    # logged count above 1 and break an exact equality forever after. The reread above is what
    # actually proves this test's own run was swept; this regex only proves the count is consistent
    # with that having happened.
    swept_match = re.search(r'"?swept_count"?\s*[:=]\s*(\d+)', caplog.text)
    assert swept_match, caplog.text
    assert int(swept_match.group(1)) >= 1, caplog.text
    assert re.search(r'"?skipped_count"?\s*[:=]\s*0\b', caplog.text), caplog.text
    # T16: `conflicts` is the log line's fourth field (E-20; ADR-0015 §3) — this tick has no
    # concurrent writer, so it must be logged as 0 rather than merely present.
    assert re.search(r'"?conflicts"?\s*[:=]\s*0\b', caplog.text), caplog.text
    assert re.search(r'"?duration_ms"?\s*[:=]', caplog.text), caplog.text
    # Constitution §8: no run content, ever. `owner_id`/`tailored_cv`/`cover_letter` checks were
    # removed from here (verify round 2): every one of them was vacuously true regardless of what
    # this code logs — `abandon_stale_tailoring_runs`' success line names only `swept_count`,
    # `skipped_count` and `duration_ms` (this file's own module docstring; `RUNNING` rows never carry
    # a document, per the mapping's own CHECK), so none of those three strings has any path into
    # `caplog.text` for this assertion to actually guard. The real guard against a document leaking
    # through this task is `test_a_database_failure_through_the_real_task_logs_the_exceptions_type_
    # and_reraises` above, which drives a genuine failure with a sentinel in the exception's message
    # and asserts the sentinel never reaches a log line.
