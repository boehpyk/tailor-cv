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
queue, it does not publish to it), while the API binds both and binds no `LlmPort` at all. Neither
file is the complete list; the port list in technical-plan.md is, and it is checked against both.

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

from sqlalchemy.ext.asyncio import AsyncSession

from tailorcraft.application.tailoring.execute_tailoring_run import ExecuteTailoringRun
from tailorcraft.domain.identity.value_objects import GuestSessionId
from tailorcraft.domain.tailoring.ports import TailoringRunRepository
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
    statement has no equivalent, so the relationship becomes composition. The seven pass-throughs are
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
        """
        await self._inner.save(run)
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
    try:
        factory = create_session_factory(engine)
        async with factory() as session:
            try:
                yield _build_use_case(settings, session), session
            except Exception:
                await session.rollback()
                raise
    finally:
        # Always, including on the error path: the loop is about to close under this engine, and an
        # asyncpg pool left holding connections bound to a dead loop is how "got Future attached to
        # a different loop" arrives one task later, blamed on the wrong code.
        await engine.dispose()


def _build_use_case(settings: Settings, session: AsyncSession) -> ExecuteTailoringRun:
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
        # strict default; the stub lives only in the adapter's own test module). This is the one
        # binding the API does not have — the API never calls the model, which is the entire point
        # of ADR-0014's queue.
        llm=GeminiLlm(settings),
        # `EventPublisherPort` -> `LoggingEventPublisher`, the same binding `deps.py` makes, and the
        # enforcement point for "an event carries no document body" (AC-22).
        events=LoggingEventPublisher(),
        # `Clock` -> `SystemClock`, whole-second at the source (ADR-0007).
        clock=SystemClock(),
        stale_after_seconds=settings.tailoring_stale_after_seconds,
    )
