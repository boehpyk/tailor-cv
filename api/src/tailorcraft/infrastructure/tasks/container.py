"""The **worker's** composition root — not FastAPI's.

`infrastructure/api/deps.py` is the other one. It is a set of FastAPI `Depends` providers, and every
resource in it hangs off a `Request` or off `app.state`: `get_session` opens one session per request
and commits it once, `get_app_settings` reads the settings the app was built with, every repository
takes a `SessionDep`. Reusing any of that in a Celery worker would mean manufacturing a fake
`Request` and a fake `FastAPI` app so that a dependency graph could be resolved against them — a
pretence, and one that would silently inherit the wrong transaction shape (see "Two commits" below).
So the worker gets its own root, deliberately, and the cost is stated rather than hidden:

**"A port with no binding is a bug" now has to be checked against two files, not one.** Every port
this slice needs must appear in `deps.py` *or* here, depending on which process needs it, and the two
lists are deliberately different: the worker binds no `GuestSessionRepository` (it never authorizes —
`ExecuteTailoringRun`'s docstring says why at length) and no `TailoringQueuePort` (it consumes the
queue, it does not publish to it), while the API binds both. The API *does* also bind `LlmPort`, and
that one is the odd entry in either file: no API route resolves it, because ADR-0014's entire point
is that the request does not call the model. `deps.get_llm` exists so that the question "which
adapter satisfies `LlmPort`?" has an answer in whichever root a reader opens, and so that an API test
has a `dependency_overrides` key with which to guarantee the suite can never reach Google. The
process that actually calls `tailor` is this one. Neither file is the complete list; the port list in
technical-plan.md is, and it is checked against both.

**Two commits, and the unit of work is therefore explicit.** `ExecuteTailoringRun` saves twice — step
4 records `running` before a twelve-second call, step 6/7 records the outcome after it — and those
two saves must land in *two separate transactions*, or a client polling
`GET /api/tailoring-runs/{id}` sees `queued` for the whole call and then jumps straight to a terminal
status, which is indistinguishable from a run nobody ever picked up. That is exactly the "still
working" vs. "this failed" distinction the frontend owes the user. `deps.get_session`'s
session-per-request shape has one commit in it and cannot express that, which is the concrete reason
this file exists rather than an import of that one.

**The footgun that governs this module: an asyncpg connection is bound to the event loop it was
created on.** `api/tests/conftest.py` documents the same thing from the other side — a session-scoped
engine under pytest-asyncio's default per-test loop gives `RuntimeError: got Future attached to a
different loop` on teardown, from tests that pass individually and fail only together. A Celery
worker is synchronous, so every task opens a *fresh* loop with `asyncio.run`; a module-level engine
would be bound to whichever loop happened to run the first task and would raise on the second. In a
worker that presents as "it worked in development and died under load", which is the worst way to
meet this bug. Hence: the engine is built **inside** `tailoring_use_case`, which runs inside the
loop, and disposed before that loop closes.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from tailorcraft.application.tailoring.abandon_stale_tailoring_runs import AbandonStaleTailoringRuns
from tailorcraft.application.tailoring.execute_tailoring_run import ExecuteTailoringRun
from tailorcraft.domain.identity.value_objects import GuestSessionId
from tailorcraft.domain.tailoring.errors import TailoringRunConcurrentlyModified
from tailorcraft.domain.tailoring.ports import LlmPort, TailoringRunRepository
from tailorcraft.domain.tailoring.tailoring_run import TailoringRun
from tailorcraft.domain.tailoring.value_objects import TailoringRunId
from tailorcraft.infrastructure.clock import SystemClock
from tailorcraft.infrastructure.events.logging_publisher import LoggingEventPublisher
from tailorcraft.infrastructure.llm.gemini import GeminiLlm
from tailorcraft.infrastructure.persistence.database import create_engine, create_session_factory
from tailorcraft.infrastructure.persistence.registry import configure_mappings
from tailorcraft.infrastructure.settings import Settings, get_settings


class CommittingTailoringRunRepository:
    """The worker's `TailoringRunRepository`: the ordinary one, except that **every write commits.**

    This is where "two commits" actually happens, and it is in the composition root rather than in
    the use case on purpose. `TailoringRunRepository.save` says nothing about transactions — *"make
    this the state you will hand back next time; when that becomes durable is the caller's
    boundary"* — precisely so the application layer can express "record `running`, call the model,
    record the outcome" without ever naming a transaction, which is a persistence concept it is not
    allowed to know about (ADR-0002). The boundary then differs per process: one transaction per HTTP
    request in the API, one per write here. `ExecuteTailoringRun` writes exactly twice on the
    ordinary paths, so "commit every write" and "two commits" are the same rule stated from the two
    ends.

    **Delegation rather than a subclass of `SqlAlchemyTailoringRunRepository`, and the reason is an
    import-order one worth knowing.** That module reads `TailoringRun._id` as a plain attribute at
    *import* time to build its `InstrumentedAttribute` casts, and those attributes only exist once
    `configure_mappings()` has run — so it cannot be imported at this module's top level, and a
    `class X(SqlAlchemyTailoringRunRepository)` statement is a top-level import by another name.
    `deps.py` solves the same problem by deferring its imports into the provider functions; a class
    statement has no equivalent, so the relationship becomes composition. The eight pass-throughs are
    the price; `mypy --strict` checking this against the `TailoringRunRepository` Protocol is what
    keeps them from drifting.
    """

    def __init__(self, inner: TailoringRunRepository, session: AsyncSession) -> None:
        self._inner = inner
        self._session = session

    def next_identity(self) -> TailoringRunId:
        return self._inner.next_identity()

    async def add(self, run: TailoringRun) -> None:
        await self._inner.add(run)
        await self._session.commit()

    async def save(self, run: TailoringRun) -> None:
        """Flush **and commit** — the whole reason this class exists.

        Safe to commit under the aggregate the caller is still holding only because
        `create_session_factory` sets `expire_on_commit=False`. With SQLAlchemy's default, this
        commit would expire every attribute of `run`, and the *next* attribute access — step 6's
        `run.mark_failed`, say — would trigger a lazy refresh, which an async session raises on
        rather than quietly issuing SQL. Loud, but only once you hit it, and only in the worker.

        A conflict rolls back **here**, before it is re-raised. The inner repository leaves the
        session in the state a failed flush leaves it in — unusable until a rollback — and in this
        process nothing else would roll it back: the `ExecuteTailoringRun` task returns `SKIPPED`
        and its closing commit would raise a second, unrelated error over the first, and the
        stale-run sweep counts the conflict and moves on to the next run in the same batch, whose
        own save would then fail for a reason that has nothing to do with that run. Rolling back is
        the caller's boundary (port docstring), and in the worker this class is the caller.
        """
        try:
            await self._inner.save(run)
        except TailoringRunConcurrentlyModified:
            await self._session.rollback()
            raise
        await self._session.commit()

    async def get(self, run_id: TailoringRunId) -> TailoringRun:
        return await self._inner.get(run_id)

    async def find(self, run_id: TailoringRunId) -> TailoringRun | None:
        return await self._inner.find(run_id)

    async def list_for_session(self, sid: GuestSessionId) -> Sequence[TailoringRun]:
        return await self._inner.list_for_session(sid)

    async def count_for_session(self, sid: GuestSessionId) -> int:
        return await self._inner.count_for_session(sid)

    async def find_active_for_session(self, sid: GuestSessionId) -> TailoringRun | None:
        return await self._inner.find_active_for_session(sid)

    async def list_stale_running(
        self, started_before: datetime, limit: int
    ) -> Sequence[TailoringRun]:
        """A read, so **no commit**, like the other read pass-throughs. The sweep's writes go through
        `save`, which commits one abandoned run at a time. A failure part-way through a batch then
        keeps every run already recorded, and the next tick lists only the rest."""
        return await self._inner.list_stale_running(started_before, limit)


@asynccontextmanager
async def tailoring_use_case() -> AsyncIterator[tuple[ExecuteTailoringRun, AsyncSession]]:
    """Build everything one tailoring task needs, yield it, and tear it down.

    Yields the session alongside the use case because the outer unit of work is the **task's**, not
    this function's: `tasks/tailoring.py` commits inside its own error boundary, the same convention
    the routers follow (`deps.py`), so that a commit failure is the task's recorded failure rather
    than a surprise during teardown. The two load-bearing commits happen *inside* the use case, one
    per write, through `CommittingTailoringRunRepository`; the task's commit closes the read
    transaction the last statements opened — which on the `MISSING` path (a run whose session was
    purged, G-26) is the only transaction there was.

    The engine is created here and disposed on the way out — **one engine per task invocation.** That
    looks wasteful, and it is: a connection pool built and discarded for a single task, so
    `pool_size=5` is in practice a pool of one. It is not negotiable, for the loop-binding reason in
    the module docstring, and the price is one TCP connect to a Postgres on the same Docker network
    in front of a task that is about to spend twelve seconds talking to Google. Named here so that if
    connection churn ever shows up in a `pg_stat_activity` sweep it is diagnosed in minutes rather
    than days — the same move technical-plan.md's OQ-9 makes for the worker holding a connection
    across the call.
    """
    settings = get_settings()

    # Imperative mapping's one failure mode: a mapping module nobody imports never runs, and the
    # aggregate stays silently unmapped until a query fails with a confusing error
    # (`persistence/registry.py`). The API's lifespan calls this and the test session has a fixture
    # for it; the worker has neither, and a task invoked directly — T35, and this slice's `/verify`
    # run — has no worker either. Idempotent and cheap after the first call: it is module imports,
    # which Python caches.
    configure_mappings()

    engine = create_engine(settings)
    # Built here rather than inside `_build_use_case` because this function owns lifecycles and that
    # one only binds ports: whoever builds a resource with a loop-bound connection closes it.
    llm = GeminiLlm(settings)
    try:
        factory = create_session_factory(engine)
        async with factory() as session:
            try:
                yield _build_use_case(settings, session, llm), session
            except Exception:
                await session.rollback()
                raise
    finally:
        try:
            # Before the engine, and for the same reason: the SDK's `httpx.AsyncClient` is bound to
            # this loop too, and its own `__del__` fallback cannot close it once `asyncio.run` has
            # shut the loop — one leaked client, with its TLS connections, per task.
            # `GeminiLlm.aclose` never raises, and the nested `finally` does not rely on that.
            await llm.aclose()
        finally:
            # Always, including on the error path: the loop is about to close under this engine, and
            # an asyncpg pool left holding connections bound to a dead loop is how "got Future
            # attached to a different loop" arrives one task later, blamed on the wrong code.
            await engine.dispose()


def _build_use_case(
    settings: Settings, session: AsyncSession, llm: LlmPort | None = None
) -> ExecuteTailoringRun:
    """Bind every port `ExecuteTailoringRun` declares — six of them, and no more.

    Split out of the context manager so the bindings read as a list rather than as the middle of a
    resource-management sandwich: this is the part a reviewer checks against the port list.

    The three repository imports are deferred to call time for the mapper-configuration reason
    `deps.py::get_base_cv_repository` documents at length, and it is sharper here — `configure_
    mappings()` runs a few lines above, in the same function, so hoisting these to the top of the
    module would break the file by moving them *before* the call that makes them legal.
    """
    from tailorcraft.infrastructure.persistence.repositories.intake.base_cv import (
        SqlAlchemyBaseCvRepository,
    )
    from tailorcraft.infrastructure.persistence.repositories.posting.job_posting import (
        SqlAlchemyJobPostingRepository,
    )
    from tailorcraft.infrastructure.persistence.repositories.tailoring.tailoring_run import (
        SqlAlchemyTailoringRunRepository,
    )

    return ExecuteTailoringRun(
        # `TailoringRunRepository` -> the committing wrapper. The API binds the bare
        # `SqlAlchemyTailoringRunRepository` for the same port; the difference *is* the boundary.
        runs=CommittingTailoringRunRepository(SqlAlchemyTailoringRunRepository(session), session),
        # `BaseCvRepository` and `JobPostingRepository` -> read-only here, so the bare adapters.
        base_cvs=SqlAlchemyBaseCvRepository(session),
        job_postings=SqlAlchemyJobPostingRepository(session),
        # `LlmPort` -> `GeminiLlm`, built with the real SDK client (its `generate` seam keeps its
        # strict default; the stub lives only in the adapter's own test module). `deps.get_llm`
        # makes the same binding, but this is the only process that ever *resolves* it: the API
        # never calls the model, which is the entire point of ADR-0014's queue.
        # `tailoring_use_case` passes the instance it will close; a caller that passes none gets a
        # fresh one and owns its lifecycle.
        llm=llm if llm is not None else GeminiLlm(settings),
        # `EventPublisherPort` -> `LoggingEventPublisher`, the same binding `deps.py` makes, and the
        # enforcement point for "an event carries no document body" (AC-22).
        events=LoggingEventPublisher(),
        # `Clock` -> `SystemClock`, whole-second at the source (ADR-0007).
        clock=SystemClock(),
        stale_after_seconds=settings.tailoring_stale_after_seconds,
    )


@asynccontextmanager
async def abandon_stale_runs_use_case() -> AsyncIterator[
    tuple[AbandonStaleTailoringRuns, AsyncSession]
]:
    """Build what one tick of the stale-run sweep needs, yield it, and tear it down (G-25').

    The same shape as `tailoring_use_case`, for the same reasons: mappings configured first, **one
    engine per invocation**, built inside the loop `asyncio.run` opened and disposed before that loop
    closes, and the session yielded so the task commits inside its own error boundary.

    **No LLM, and that is the point rather than an economy.** The sweep records that a call was lost.
    It never makes one, so `GeminiLlm` is not built, no SDK client is opened, and nothing here can
    spend money. `tailoring_use_case`'s `llm.aclose()` has no counterpart for the same reason.
    """
    settings = get_settings()
    # See `tailoring_use_case`: beat publishes this task to a worker that may never have run a
    # tailoring task, so nothing else is guaranteed to have imported the mappings.
    configure_mappings()

    engine = create_engine(settings)
    try:
        factory = create_session_factory(engine)
        async with factory() as session:
            try:
                yield _build_sweep_use_case(settings, session), session
            except Exception:
                await session.rollback()
                raise
    finally:
        # Always, including on G-35's path, for the loop-binding reason in the module docstring.
        await engine.dispose()


def _build_sweep_use_case(settings: Settings, session: AsyncSession) -> AbandonStaleTailoringRuns:
    """Bind the three ports `AbandonStaleTailoringRuns` declares, and no more.

    Split out for the reason `_build_use_case` is, and it is the seam a task test binds to its own
    session. The repository import is deferred for the mapper-configuration reason given there.
    """
    from tailorcraft.infrastructure.persistence.repositories.tailoring.tailoring_run import (
        SqlAlchemyTailoringRunRepository,
    )

    return AbandonStaleTailoringRuns(
        # `TailoringRunRepository` -> the committing wrapper, **one commit per abandoned run**, and
        # both halves of the failure contract depend on it. A database failure part-way through a
        # batch keeps every run already recorded (G-35), so the next tick lists only the rest. And a
        # run another process decided first (G-36) is skipped without taking the batch's earlier
        # writes back with it. One transaction per tick would make one bad row cost the whole batch.
        runs=CommittingTailoringRunRepository(SqlAlchemyTailoringRunRepository(session), session),
        # `EventPublisherPort` -> `LoggingEventPublisher`: each abandonment's `TailoringRunFailed`
        # is the per-run log line G-25' asks for (`tailoring_run_id`, `reason=abandoned`).
        events=LoggingEventPublisher(),
        # `Clock` -> `SystemClock`, whole-second at the source (ADR-0007). Read once per batch by the
        # use case, so every `completed_at` written in one tick is the same instant.
        clock=SystemClock(),
        # The same setting `_build_use_case` passes to `ExecuteTailoringRun` step 3. One window, both
        # recovery paths.
        stale_after_seconds=settings.tailoring_stale_after_seconds,
        # `batch_size` is left at the use case's default of 100, and **not made a setting**. A run
        # is only `running` while a worker slot holds it, so one lost worker strands at most
        # `--concurrency` runs (2 in production). A backlog of 100 would take fifty lost workers
        # inside a single five-minute window, and even then the next tick takes the rest a minute
        # later. A knob nobody has a reason to turn is a knob somebody will turn wrongly.
    )
