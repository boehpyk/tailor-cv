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
loop, and disposed before that loop closes — and inside each of the three builders that followed it,
for the same reason and with no exception made for the cheap ones.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from tailorcraft.application.export.abandon_stale_export_jobs import AbandonStaleExportJobs
from tailorcraft.application.export.render_export_job import RenderExportJob
from tailorcraft.application.retention.purge_expired_guest_sessions import PurgeExpiredGuestSessions
from tailorcraft.application.tailoring.abandon_stale_tailoring_runs import AbandonStaleTailoringRuns
from tailorcraft.application.tailoring.execute_tailoring_run import ExecuteTailoringRun
from tailorcraft.domain.export.export_job import ExportJob
from tailorcraft.domain.export.ports import ExportJobRepository
from tailorcraft.domain.export.value_objects import ExportFormat, ExportJobId
from tailorcraft.domain.identity.value_objects import GuestSessionId
from tailorcraft.domain.retention.value_objects import RetentionWindow
from tailorcraft.domain.tailoring.ports import LlmPort, TailoringRunRepository
from tailorcraft.domain.tailoring.tailoring_run import TailoringRun
from tailorcraft.domain.tailoring.value_objects import TailoredDocumentKind, TailoringRunId
from tailorcraft.infrastructure.clock import SystemClock
from tailorcraft.infrastructure.events.logging_publisher import LoggingEventPublisher
from tailorcraft.infrastructure.export.renderer import MarkdownDocumentRenderer
from tailorcraft.infrastructure.files.local_file_store import LocalFileStore
from tailorcraft.infrastructure.llm.gemini import GeminiLlm
from tailorcraft.infrastructure.persistence.database import create_engine, create_session_factory
from tailorcraft.infrastructure.persistence.registry import configure_mappings
from tailorcraft.infrastructure.retention.data_access import (
    CommittingExpiredGuestDataAdapter,
    OverdueBacklog,
)
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
        """Flush inside a SAVEPOINT, **then commit** — the whole reason this class exists.

        Safe to commit under the aggregate the caller is still holding only because
        `create_session_factory` sets `expire_on_commit=False`. With SQLAlchemy's default, this
        commit would expire every attribute of `run`, and the *next* attribute access — step 6's
        `run.mark_failed`, say — would trigger a lazy refresh, which an async session raises on
        rather than quietly issuing SQL. Loud, but only once you hit it, and only in the worker.

        **A conflict is contained to the one run that lost, and the SAVEPOINT is what contains it.**
        A failed flush rolls back on its way out, and that rollback restores the snapshot of the
        nearest transaction *boundary*. At the root boundary — the only one there is without a
        SAVEPOINT — SQLAlchemy restores it by expiring **every** instance in the identity map
        (`SessionTransaction._restore_snapshot(dirty_only=False)`), not the one whose `UPDATE` was
        refused. The first version of this method then called `session.rollback()`, which closes
        that dead transaction but expires nothing further: the damage was already done in the flush.
        The stale-run sweep found it (slice 1.4's `/verify`): a conflict on the first candidate
        expired the second, and the sweep's next `run.is_stale(...)` — a plain attribute read with
        no `await`, from ordinary application code — tried to lazy-load `_status` on an
        `AsyncSession` outside `greenlet_spawn` and raised `MissingGreenlet`. E-20's "the batch
        continues" was true against the fake repository and false against this one.

        The same lesson as "read the id before the flush" in the inner repository, one level up: a
        `rollback()` is a statement about the **whole session**, never about the one object that
        failed. Inside a SAVEPOINT the boundary is the nested transaction, and its snapshot restore is
        `dirty_only=True` — the run that was flushed is expired (its in-memory copy was wrong, and
        the caller drops it anyway), every other loaded instance keeps its state, the outer
        transaction stays active, and the next candidate's `is_stale` is a plain read again.

        **The `expunge` before `begin_nested` is load-bearing, and it is not in SQLAlchemy's textbook
        example.** `begin_nested()` flushes on entry, unconditionally, so that the SAVEPOINT starts
        from a clean session (`SessionTransaction._take_snapshot`; `no_autoflush` does not reach it,
        because it is a direct `flush()`, not an autoflush). The textbook mutates *inside* the
        `with` block, so it never meets this. A use case mutates the aggregate and *then* calls
        `save` — that is the port's contract — so `run` is already dirty when this method is
        entered, and a bare `begin_nested()` would flush it in `__aenter__`: outside the SAVEPOINT,
        outside the inner repository's `StaleDataError` translation, and back to expiring the whole
        identity map. Measured against 2.0.52 before this shape was chosen. Detaching the run for the
        moment the SAVEPOINT opens keeps its modifications (`state.modified` survives `expunge`) and
        leaves the session with nothing to flush; the inner `save` re-attaches it with `add()` and
        flushes it where the SAVEPOINT can bound the failure. A run that arrives already detached
        (the inner docstring's "session closed between the load and the save" case) skips the
        `expunge` and is re-attached by the same `add()`.

        The failure this does *not* contain is a database failure that is not a conflict (G-35): it
        propagates, the sweep raises, the task's error boundary rolls the session back, and the runs
        already committed one-per-save stay committed — the SAVEPOINT changes nothing there.
        """
        if run in self._session:
            self._session.expunge(run)
        async with self._session.begin_nested():
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

    async def list_stale_running(
        self, started_before: datetime, limit: int
    ) -> Sequence[TailoringRun]:
        """A read, so **no commit**, like the other read pass-throughs. The sweep's writes go through
        `save`, which commits one abandoned run at a time. A failure part-way through a batch then
        keeps every run already recorded, and the next tick lists only the rest."""
        return await self._inner.list_stale_running(started_before, limit)


class CommittingExportJobRepository:
    """The worker's `ExportJobRepository`: the ordinary one, except that **every write commits.**

    **This is `CommittingTailoringRunRepository` above, written out a second time rather than
    generalized, and the duplication is the decision.** Read that class's `save` docstring for the
    reasoning — four paragraphs on `expire_on_commit=False`, on a failed flush expiring the whole
    identity map at the root boundary, on why `rollback()` is a statement about the session and never
    about the one object that failed, and on why `expunge` before `begin_nested()` is load-bearing
    and absent from SQLAlchemy's textbook example. Every word of it is true here, and none of it is
    repeated here.

    What a generic `Committing[T]` wrapper would have cost: it would have to be written against a
    supertype of two repository Protocols that share a *shape* and not a contract. They differ in
    every method beyond the four CRUD ones — `list_for_session` and `find_active_for_session` over
    there, `list_for_run`, `find_latest_for_key` and `list_stale_rendering` here — so the supertype
    would be four methods wide and each repository would need its own pass-throughs anyway, for the
    delegation reason below. Shared shape is not shared behaviour (CLAUDE.md), and the price of
    saying so is nine pass-through methods that `mypy --strict` checks against the Protocol.

    **Delegation rather than a subclass of `SqlAlchemyExportJobRepository`**, for the import-order
    reason `CommittingTailoringRunRepository` documents: that module reads `ExportJob._id` as a plain
    attribute at *import* time to build its `InstrumentedAttribute` casts, and those attributes exist
    only once `configure_mappings()` has run. A `class X(SqlAlchemyExportJobRepository)` statement is
    a top-level import by another name, so the relationship has to be composition.

    **Two commits per render, and they are the reason this exists.** `RenderExportJob` writes twice
    on the ordinary path — `rendering` before the render, then `ready` or `failed` after it — and
    those two saves must land in two separate transactions, or a client polling
    `GET /api/export-jobs/{id}` sees `queued` for the whole render and then jumps to a terminal
    status, which is indistinguishable from a job no worker ever picked up.
    """

    def __init__(self, inner: ExportJobRepository, session: AsyncSession) -> None:
        self._inner = inner
        self._session = session

    def next_identity(self) -> ExportJobId:
        return self._inner.next_identity()

    async def add(self, job: ExportJob) -> None:
        await self._inner.add(job)
        await self._session.commit()

    async def save(self, job: ExportJob) -> None:
        """Flush inside a SAVEPOINT, **then commit** — see `CommittingTailoringRunRepository.save`.

        The `expunge` is not a precaution here either: `begin_nested()` flushes on entry,
        unconditionally, to take its snapshot, and a use case mutates the aggregate *before* calling
        `save` — that is the port's contract — so a bare `begin_nested()` would flush the dirty job
        in `__aenter__`, outside the SAVEPOINT and outside the inner repository's `StaleDataError`
        translation, and a conflict would expire the whole identity map on its way out.

        That is exactly what the sweep cannot survive. `AbandonStaleExportJobs` keeps iterating
        loaded aggregates after a conflict (X-39: it counts one and continues), and the next
        candidate's `job.is_stale(...)` is a plain attribute read with no `await` — on an expired
        instance that is a lazy load on an `AsyncSession`, which is `MissingGreenlet`. 1.4's
        `/verify` met that bug in the run sweep; this class is shaped so the job sweep never can.
        """
        if job in self._session:
            self._session.expunge(job)
        async with self._session.begin_nested():
            await self._inner.save(job)
        await self._session.commit()

    async def get(self, job_id: ExportJobId) -> ExportJob:
        return await self._inner.get(job_id)

    async def find(self, job_id: ExportJobId) -> ExportJob | None:
        return await self._inner.find(job_id)

    async def list_for_run(self, run_id: TailoringRunId) -> Sequence[ExportJob]:
        return await self._inner.list_for_run(run_id)

    async def find_latest_for_key(
        self, run_id: TailoringRunId, document: TailoredDocumentKind, format: ExportFormat
    ) -> ExportJob | None:
        return await self._inner.find_latest_for_key(run_id, document, format)

    async def count_for_session(self, sid: GuestSessionId) -> int:
        return await self._inner.count_for_session(sid)

    async def list_stale_rendering(
        self, started_before: datetime, limit: int
    ) -> Sequence[ExportJob]:
        """A read, so **no commit**, like the other read pass-throughs. The sweep's writes go through
        `save`, which commits one abandoned job at a time. A failure part-way through a batch then
        keeps every job already recorded (X-38), and the next tick lists only the rest."""
        return await self._inner.list_stale_rendering(started_before, limit)


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


@asynccontextmanager
async def export_use_case() -> AsyncIterator[tuple[RenderExportJob, AsyncSession]]:
    """Build everything one export task needs, yield it, and tear it down (X-29…X-37).

    The same shape as `tailoring_use_case`, for the same three reasons: mappings configured first
    (beat and the queue both publish into a worker that may never have run any other task, so nothing
    else is guaranteed to have imported them), **one engine per task invocation** built inside the
    loop `asyncio.run` opened and disposed before that loop closes, and the session yielded so
    `tasks/export.py` commits inside its own error boundary rather than during teardown.

    **No LLM, and that is structural rather than an economy.** Exporting never calls a model — it
    renders Markdown that was already bought and stored — so `GeminiLlm` is not built, no SDK client
    is opened, and nothing on this path can spend money. `tailoring_use_case`'s `llm.aclose()`
    therefore has no counterpart here, and the whole `try/finally` collapses to disposing the engine.

    The renderer is the one resource here with real teardown to think about and none to do: WeasyPrint
    and `python-docx` hold no client, no socket and no loop-bound handle, and every synchronous call
    into them happens in `asyncio.to_thread` inside `MarkdownDocumentRenderer.render`. A thread left
    behind by a cancelled `wait_for` outlives this function by design — it is the hard time limit's
    problem, which is why the stale window must sit above that limit (`create_celery`'s second
    guard).
    """
    settings = get_settings()
    configure_mappings()

    engine = create_engine(settings)
    try:
        factory = create_session_factory(engine)
        async with factory() as session:
            try:
                yield _build_export_use_case(settings, session), session
            except Exception:
                await session.rollback()
                raise
    finally:
        # Always, including on X-32's path (Postgres unavailable while recording `ready`), for the
        # loop-binding reason in the module docstring.
        await engine.dispose()


def _build_export_use_case(settings: Settings, session: AsyncSession) -> RenderExportJob:
    """Bind every port `RenderExportJob` declares — six of them, and no more.

    Split out of the context manager for the reason `_build_use_case` is: this is the part a reviewer
    checks against the port list, and it should read as a list rather than as the middle of a
    resource-management sandwich. The two repository imports are deferred to call time for the
    mapper-configuration reason `_build_use_case` documents, which is sharper here — `configure_
    mappings()` runs a few lines above in the caller, so hoisting them to the top of the module would
    move them *before* the call that makes them legal.
    """
    from tailorcraft.infrastructure.persistence.repositories.export.export_job import (
        SqlAlchemyExportJobRepository,
    )
    from tailorcraft.infrastructure.persistence.repositories.tailoring.tailoring_run import (
        SqlAlchemyTailoringRunRepository,
    )

    return RenderExportJob(
        # `ExportJobRepository` -> the committing wrapper. The API binds the bare
        # `SqlAlchemyExportJobRepository` for the same port; the difference *is* the boundary, and
        # here it is what makes `rendering` visible to a polling client while the render is still in
        # flight.
        jobs=CommittingExportJobRepository(SqlAlchemyExportJobRepository(session), session),
        # `TailoringRunRepository` -> the **bare** adapter, not `CommittingTailoringRunRepository`,
        # and that is a statement rather than an oversight: this slice never writes to a
        # `TailoringRun`. Step 5 reads the run and compares `run.version` to `job.run_version`
        # (X-31). Wrapping it in something whose whole purpose is to commit writes would advertise a
        # write path that does not exist.
        runs=SqlAlchemyTailoringRunRepository(session),
        # `DocumentRendererPort` -> `MarkdownDocumentRenderer`, built with its **defaults**: the
        # refusing `url_fetcher` and the real `sanitize_html`. Both are testing seams with strict
        # defaults (the `HttpxTrafilaturaFetcher` / `GeminiLlm` pattern), and nothing under
        # `api/src/` passes either argument — this line and `deps.get_document_renderer` are the two
        # places that must keep not passing them, because a production binding that overrode the
        # fetcher would be an outbound request from a render (AC-30).
        renderer=MarkdownDocumentRenderer(settings),
        # `FileStorePort` -> `LocalFileStore` over `settings.upload_dir`, the one directory `api` and
        # `worker` both mount (ADR-0011). The worker writes here and the API reads it back for the
        # download; lose the volume on either side and exports break in a way no health check sees.
        files=LocalFileStore(settings.upload_dir),
        # `EventPublisherPort` -> `LoggingEventPublisher`, the same binding `deps.py` makes, and the
        # enforcement point for "an event carries ids, enums and numbers only" (AC-33).
        events=LoggingEventPublisher(),
        # `Clock` -> `SystemClock`, whole-second at the source (ADR-0007).
        clock=SystemClock(),
        # The same window the sweep uses, so the two recovery paths cannot disagree about which jobs
        # a worker can still be holding. `create_celery` refuses a value at or below the hard time
        # limit before either of them runs.
        stale_after_seconds=settings.export_stale_after_seconds,
    )


@asynccontextmanager
async def abandon_stale_export_jobs_use_case() -> AsyncIterator[
    tuple[AbandonStaleExportJobs, AsyncSession]
]:
    """Build what one tick of the stale-job sweep needs, yield it, and tear it down (X-29, AC-20).

    `abandon_stale_runs_use_case`'s shape exactly, over the other aggregate: mappings first, one
    engine per invocation built inside the loop and disposed before it closes, the session yielded so
    the task commits inside its own error boundary.

    **Neither a renderer nor a file store, and neither omission is an economy.** The sweep records
    that a render was lost. It never starts one, so nothing here can open a thread, and it never
    deletes the orphan file a killed worker may have left under the job's key — a delete on a failure
    path is a second way to lose a file, and 1.6's orphan sweep owns that tree (X-29, ADR-0011 §4).
    """
    settings = get_settings()
    # See `tailoring_use_case`: beat publishes this task to a worker that may never have run an
    # export task, so nothing else is guaranteed to have imported the mappings.
    configure_mappings()

    engine = create_engine(settings)
    try:
        factory = create_session_factory(engine)
        async with factory() as session:
            try:
                yield _build_export_sweep_use_case(settings, session), session
            except Exception:
                await session.rollback()
                raise
    finally:
        # Always, including on X-38's path, for the loop-binding reason in the module docstring.
        await engine.dispose()


def _build_export_sweep_use_case(
    settings: Settings, session: AsyncSession
) -> AbandonStaleExportJobs:
    """Bind the three ports `AbandonStaleExportJobs` declares, and no more.

    Split out for the reason `_build_sweep_use_case` is, and it is the seam a task test binds to its
    own session. The repository import is deferred for the mapper-configuration reason given there.
    """
    from tailorcraft.infrastructure.persistence.repositories.export.export_job import (
        SqlAlchemyExportJobRepository,
    )

    return AbandonStaleExportJobs(
        # `ExportJobRepository` -> the committing wrapper, **one commit per abandoned job**, and both
        # halves of the failure contract depend on it. A database failure part-way through a batch
        # keeps every job already recorded (X-38), so the next tick lists only the rest. And a job a
        # worker decided first (X-39) is counted and skipped without taking the batch's earlier
        # writes back with it — which the SAVEPOINT in `save` is what makes possible at all. One
        # transaction per tick would make one conflicted row cost the whole batch.
        jobs=CommittingExportJobRepository(SqlAlchemyExportJobRepository(session), session),
        # `EventPublisherPort` -> `LoggingEventPublisher`: each abandonment's `ExportFailed` is the
        # per-job line X-29 asks for (`export_job_id`, `reason=abandoned`).
        events=LoggingEventPublisher(),
        # `Clock` -> `SystemClock`, whole-second at the source (ADR-0007). Read once per batch by the
        # use case, so every `completed_at` written in one tick is the same instant.
        clock=SystemClock(),
        # The same window `_build_export_use_case` passes to `RenderExportJob`'s own stale check. One
        # window, both recovery paths.
        stale_after_seconds=settings.export_stale_after_seconds,
        # `batch_limit` is left at the use case's default of 100, and **not made a setting**, for the
        # run sweep's reason: a job is only `rendering` while a worker slot holds it, so one lost
        # worker strands at most `--concurrency` jobs (2 in production). A backlog of 100 would take
        # fifty lost workers inside a single five-minute window, and even then the next tick takes
        # the rest a minute later. A knob nobody has a reason to turn is a knob somebody will turn
        # wrongly.
    )


@asynccontextmanager
async def purge_expired_guest_sessions_use_case() -> AsyncIterator[
    tuple[PurgeExpiredGuestSessions, OverdueBacklog, AsyncSession]
]:
    """Build one guest-purge run, yield it with its backlog reader, and tear it down (T22, AC-28).

    The two sweeps' shape, with two differences worth naming rather than diffing out:

    **It yields a second object.** The task's one log line owes `overdue_after` (AC-21) and the use
    case cannot supply it — see `OverdueBacklog`. Binding it here keeps the entry point thin: the
    task asks a question, it does not compose a query.

    **The engine is built inside this function**, in the loop `asyncio.run` just opened, and
    disposed before that loop closes. Not an economy and not a style: an asyncpg connection is bound
    to the loop that created it, so a module-level engine in a worker presents as "it worked in
    development and died under load" — pytest-asyncio's two loop scopes taught this codebase the
    same lesson from the other end (CLAUDE.md).

    **No `OrphanFileScannerPort` is bound, and the omission is the design** (ADR-0018, technical
    plan §3's wiring table). The orphan sweep is operator-run only, never on beat: it walks a volume
    and deletes files a database cross-check says nothing references, and a mistake there is the one
    irreversible loss this feature can cause. It gets its root in the CLI, where a human typed the
    command; nothing in this worker can start one.

    The session is yielded so the **task** commits inside its own error boundary, the convention
    every other entry point here follows. The load-bearing commits are not that one: they happen
    inside the run, one per deleted session, through `CommittingExpiredGuestDataAdapter`. This
    commit closes the read transaction the listing and the backlog count opened — which on a tick
    that found nothing overdue is the only transaction there was.
    """
    settings = get_settings()
    # See `tailoring_use_case`: beat publishes this task to a worker that may never have run any
    # other task, so nothing else is guaranteed to have imported the mapping modules. The purge's
    # own SQL is Core, but `FileRefType` and the tables it names are built at mapping import.
    configure_mappings()

    engine = create_engine(settings)
    try:
        factory = create_session_factory(engine)
        async with factory() as session:
            try:
                purge, backlog = _build_purge_use_case(settings, session)
                yield purge, backlog, session
            except Exception:
                await session.rollback()
                raise
    finally:
        # Always, including on R-1's path, for the loop-binding reason in the module docstring.
        await engine.dispose()


def _build_purge_use_case(
    settings: Settings, session: AsyncSession
) -> tuple[PurgeExpiredGuestSessions, OverdueBacklog]:
    """Bind the four ports `PurgeExpiredGuestSessions` declares, and the backlog reader beside it.

    Split out for the reason the other builders are: this is the part a reviewer checks against the
    port list. The adapter import is deferred to call time for the mapper-configuration reason
    `_build_use_case` documents — `configure_mappings()` runs in the function above, so hoisting it
    would move the import before the call that makes it legal.
    """
    from tailorcraft.infrastructure.persistence.retention.expired_guest_data import (
        SqlAlchemyExpiredGuestData,
    )

    # `ExpiredGuestDataPort` -> the committing wrapper, **one commit per deleted session**. It is
    # what makes "rows first, *committed*, then files" a statement about durability rather than
    # about ordering (ADR-0018 decision 2): the unlink that follows a `delete_session` is allowed to
    # rely on that row being gone for good. One transaction per run would make a mid-batch failure
    # un-partial and would unlink the files of sessions whose rows then came back.
    #
    # One adapter instance, shared by the use case and the backlog reader: the same session, the
    # same predicate, no second connection for a `COUNT`. `count_expired` is a read and the wrapper
    # does not commit it.
    data = CommittingExpiredGuestDataAdapter(SqlAlchemyExpiredGuestData(session), session)

    # `Clock` -> `SystemClock`, whole-second at the source (ADR-0007). The use case reads it **once**
    # per run (AC-6), so a batch cannot disagree with itself about when "now" was.
    clock = SystemClock()

    # `RetentionWindow` wraps `settings.guest_retention_hours` here and nowhere else — the one place
    # `os.environ`'s value becomes the type three readers share. A non-positive window is refused by
    # the value object at construction, which is why no startup guard is needed for it either.
    window = RetentionWindow(hours=settings.guest_retention_hours)

    purge = PurgeExpiredGuestSessions(
        data=data,
        # `FileStorePort` -> `LocalFileStore`, the same root `api` writes uploads and exports to and
        # the same one the renderer reads (ADR-0011). `delete` is `missing_ok` by contract, which is
        # half of why a re-run of this job is safe.
        files=LocalFileStore(settings.upload_dir),
        clock=clock,
        window=window,
        # `batch_limit` stays at the use case's default of 100 and is **not** a setting, for the
        # sweeps' reason. The bound exists so one tick cannot hold a worker slot for an hour; it is
        # not a throughput knob, because a tick that hits the bound is followed by another in an
        # hour and the backlog is visible on `/health/ready` the whole time. A box that stays behind
        # wants the CLI (`make purge`, which loops batches), not a bigger number here.
        # `dry_run` likewise stays False: a scheduled purge that deletes nothing would keep the
        # promise on paper only, and the dry run belongs to the operator's rehearsal.
    )
    return purge, OverdueBacklog(data, clock, window)
