"""The `RenderExportJob` use case: the worker's half of a queued export — pick up a `queued` job,
render the run's current document into the job's format, store the bytes, and record what happened.

Read this module's class docstring next to `request_export.RequestExport`'s. The two are
deliberately asymmetric in how they reach a `TailoringRun`, and each says why by pointing at the
other. `ExecuteTailoringRun` and `RequestTailoringRun` draw the identical contrast one context over.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from tailorcraft.domain.export.errors import (
    DocumentRenderFailed,
    ExportJobConcurrentlyModified,
)
from tailorcraft.domain.export.export_job import ExportJob
from tailorcraft.domain.export.ports import DocumentRendererPort, ExportJobRepository
from tailorcraft.domain.export.value_objects import (
    ExportFailureReason,
    ExportJobId,
    ExportJobStatus,
)
from tailorcraft.domain.shared.clock import Clock
from tailorcraft.domain.shared.events import EventPublisherPort
from tailorcraft.domain.shared.files import FileStorePort, FileStoreUnavailable
from tailorcraft.domain.tailoring.ports import TailoringRunRepository
from tailorcraft.domain.tailoring.value_objects import (
    TailoredDocumentKind,
    TailoringRunStatus,
)

# **This layer does not log** — the convention `application/tailoring/execute_tailoring_run.py`
# writes out in full, and this slice keeps it. The application layer's channel for saying what
# happened is `events.publish(...)`: `LoggingEventPublisher` logs every field of every event
# generically, so publishing `ExportFailed` *is* the log line, and it is the one the privacy test
# captures. A second logging channel opened here would emit records that test cannot see, and a
# privacy test blind to a channel passes vacuously for everything sent down it.
#
# Where a finer-grained label is genuinely needed — `export.run_missing` at step 5's unreachable
# branch, `outcome=skipped` per invocation — it is the **task's** line
# (`infrastructure/tasks/export.py`), which is infrastructure and has `structlog`.


@dataclass(frozen=True, slots=True)
class RenderExportJobCommand:
    """The job to render, and nothing else.

    One field, on purpose (ADR-0014 §5, §6; ADR-0016). The id is the only argument the queued task
    ever receives, and everything else this use case needs it reads back from the row — which is
    what makes the task idempotent by construction rather than by a flag. There is no second copy
    of the inputs travelling through Redis that could disagree with the database, and no payload a
    redelivery could replay against a different row.

    The id does a second job here that it does nowhere else: it is also the sole input to the
    file's key (`FileRef.for_export`, XJ-7), so a redelivery that somehow raced past step 4 would
    write the same bytes to the same key rather than leaving a second orphan.
    """

    export_job_id: ExportJobId


class RenderExportJobOutcome(StrEnum):
    """What the worker did — the task's whole return vocabulary.

    Five values rather than a `bool` or a raised exception, because the Celery task above this use
    case is a **thin entry point** whose only job is to translate an outcome into a log line
    (ADR-0005). Four of the five are perfectly ordinary and none of them is an error the task
    should retry: `task_acks_late = True` makes redelivery real, and a redelivery of a job that is
    already decided, already rendering, gone, or abandoned is an expected event, not a fault.

    A `StrEnum` so the value logs as `outcome=skipped` rather than as
    `RenderExportJobOutcome.SKIPPED`, matching `ExportJobStatus` and `ExportFailureReason` next
    door.

    Deliberately **not** the same enum as `ExportJobStatus`: this describes what *this invocation*
    did, and the job's status describes where the *job* stands. `SKIPPED`, `MISSING` and
    `ABANDONED` have no status counterpart at all, and a `FAILED` outcome and a `failed` status are
    only incidentally spelled alike — the outcome is the worker's report, the status is the
    recorded fact. `ExecuteTailoringRunOutcome` is the same five words for the same five reasons,
    and the two enums are still two enums: they are the return vocabularies of two different tasks
    over two different aggregates, and a shared one would have to be widened by whichever of them
    grew a sixth outcome first.
    """

    READY = "ready"  # rendered, stored, and recorded — the only outcome that produces a file
    FAILED = "failed"  # a reason was recorded on the job: the render, the store or the source
    SKIPPED = "skipped"  # already decided, rendering inside the window, or lost the start race
    MISSING = "missing"  # the job (or its run) is gone — the session was purged
    ABANDONED = "abandoned"  # redelivered past the stale window; recorded `failed` / `abandoned`


class RenderExportJob:
    """Render one `queued` `ExportJob` and record the outcome — success or failure — as a **state of
    the aggregate**, never as an exception that escapes the task.

    **The contrast with `RequestExport`, stated as a contrast because it is the design.** That use
    case composes `GetTailoringRunForSession` — a *use case*, carrying an authorization rule —
    precisely so the "not mine → 404" check cannot be forgotten at a new entry point. This one
    takes `TailoringRunRepository`, used directly, with no ownership check anywhere in it, and that
    is deliberate rather than the check having been dropped on the way to the worker.

    The two are answering different questions. `RequestExport` acts *on behalf of a caller who
    might not own these rows*, and must find out. This one acts on behalf of nobody: **the job it
    loads already encodes the authorization decision**, made and committed at request time against
    a session that was live then. `job.tailoring_run_id` is not an id a stranger supplied — it is
    an id this system wrote into a row after checking it.

    Re-running the check here would also be actively **wrong**, not merely redundant. It resolves
    the session, and a guest session can expire (or be purged, ADR-0006) between the request and
    the pickup — a queue backlog is enough. The check would then raise `GuestSessionExpired` and
    fail a render **for a reason that has nothing to do with rendering**, on work the user is
    already waiting for. The authorization question was asked at the only moment it had a
    meaningful answer. (1.3's `ExecuteTailoringRun` makes this argument first; this is the second
    aggregate it applies to, unchanged.)

    Flow (technical-plan.md, "Application layer" §2; T7 implements it):

    1. ``job = await jobs.find(cmd.export_job_id)``; ``None`` → return `MISSING` (X-33). No
       exception and no retry — `find` returns `None` rather than raising exactly so absence is an
       ordinary branch in the worker. One log line at the task.
    2. ``job.status`` terminal (`READY` / `FAILED`) → return `SKIPPED` (X-34). **No render.** This
       is AC-18, and it is the aggregate's `ExportAlreadyDecided` doing the work: a redelivery
       cannot buy a second render.
    3. ``job.status is RENDERING``: if ``job.is_stale(now, stale_after)``, then
       ``job.mark_failed(ABANDONED, now)``, save, publish, return `ABANDONED`; otherwise return
       `SKIPPED`. The stale window is the only thing that tells "a worker is mid-render right now"
       apart from "a worker died holding this job and nobody is coming back".
    4. ``job.mark_started(clock.now())``; ``await jobs.save(job)`` **and commit** — with
       `ExportJobConcurrentlyModified` **caught** → return `SKIPPED` **before any render**
       (X-30, AC-6). Nothing is published on that path: the start did not happen.
    5. ``run = await runs.find(job.tailoring_run_id)``. ``None`` → return `MISSING` (X-35). Not
       `SUCCEEDED` → ``mark_failed(SOURCE_UNAVAILABLE)``, save, publish, `FAILED`.
       ``not job.was_requested_for(run.version)`` → ``mark_failed(SOURCE_CHANGED)``, save, publish,
       `FAILED` (X-31), **before the render**, so no worker second is spent on a document nobody
       asked for.
    6. ``docs = run.current_documents`` — not `None` on a succeeded run, narrowed for `mypy` as
       1.3's step 5 narrows `extracted_text`; ``source = docs.cv if job.document is CV else
       docs.cover_letter``.
    7. ``started = time.perf_counter()``; ``data = await renderer.render(source.value,
       document=job.document, format=job.format)`` — **`DocumentRenderFailed` is caught here** and
       recorded: ``mark_failed(exc.reason)``, save, publish, `FAILED` (X-25…X-27).
    8. ``await files.put(job.storage_ref, data)`` — `FileStoreUnavailable` caught →
       ``mark_failed(FILE_STORE_UNAVAILABLE)`` (X-28).
    9. ``job.mark_ready(byte_size=len(data), render_duration_ms=..., at=now)``; save; publish;
       return `READY`.

    Five of those steps carry a decision that is invisible from the code alone, so each is written
    out here as well as commented at its own line in T7.

    **Step 4 — two commits, and this is the second place in the codebase that needs them.** The
    save that records `RENDERING` is committed **on its own**, in a separate transaction from the
    one that records the outcome, because the client is polling `GET /api/export-jobs/{id}`
    throughout. A single transaction would show `queued` for the whole render and then jump
    straight to a terminal status — indistinguishable, from the outside, from a job nobody ever
    picked up, which is exactly the "still working" vs. "this failed" distinction the frontend owes
    the user. This is why the worker's unit of work is explicit rather than one request-scoped
    session, and why `ExportJobRepository.save` deliberately says nothing about transactions: the
    boundary stays the caller's, and the worker's composition root closes it.

    **Step 4, the other half — the version check is what makes two deliveries render once.** The
    aggregate cannot see the race: two deliveries of one job both read `queued`, both pass
    `ExportAlreadyStarted` against their own in-memory copy, and only the repository's version
    check (ADR-0015 §3) settles it. So the save that records `RENDERING` is the one place here that
    catches `ExportJobConcurrentlyModified`, and it returns `SKIPPED` **before** step 7 rather than
    after it — the loser loses before spending the worker seconds, and AC-6 asserts the fake
    renderer's call count is exactly 1.

    **Step 5 — both branches are guards, not paths** (X-35). A run that is gone means the guest
    session was purged between the enqueue and now; but the purge cascades, so the job row went
    with it, and the branch is effectively unreachable. It returns `MISSING` — log one line, do not
    raise — **because there is no job left to record a failure on**. A run that is not `SUCCEEDED`
    is unreachable by state: a run is `succeeded` when the job is created and terminal thereafter.
    It is recorded `source_unavailable` rather than left to fall through, so an impossibility that
    becomes possible shows up as a reason in the failure breakdown instead of as a job stuck
    `rendering` until the sweep.

    **Step 5, the version comparison — before the render, not after.** It is the cheapest check in
    the sequence and it invalidates everything after it, so putting it anywhere later would mean
    paying for a render whose output is already known to be stale. Nothing is ever re-pointed at
    the new version (XJ-9): `source_changed` is recorded, and *Export again* creates a **new** job
    at the current version.

    **Step 7 — `DocumentRenderFailed` is caught here, and that is the deliberate opposite of
    `RenderDocumentInline`**, which lets the identical exception propagate to a 500 and records
    nothing. The line that decides which shape applies is ADR-0014 §2 — *was anything spent, and is
    there an artifact to own?* Here the answer is yes: a worker second has gone and a row exists
    that the client is polling, so the job owns the fact that it happened and the reason it
    produced nothing. A failed export is a **recorded state**, never an exception escaping the task
    (AC-19). For whoever is tempted to simplify this into a propagating error: **an application
    test asserts the recording, not the propagation** (T6, AC-19), so that change turns a test red
    rather than passing quietly — which is the whole point of writing it that way.

    **Step 8 — file before row** (ADR-0006 §2). The bytes are stored *before* `mark_ready` writes
    the key, so the crash window between the two leaves an **orphan file** — findable by id, swept
    by 1.6 — rather than a `ready` row pointing at nothing, which is a 410 for a file the user can
    see in their list. Choose the crash window whose survivor is recoverable, again.

    **`render_duration_ms` is measured here with `time.perf_counter`, not in the adapter and not
    from the `Clock`.** The `Clock` port is whole-second by contract (ADR-0007), so it cannot
    measure a two-second render at all — 1.3's argument for `llm_duration_ms`, unchanged. It is
    measured in the use case rather than the adapter because the number is the use case's to record
    on the aggregate: an adapter that returned a duration alongside its bytes would be reporting on
    itself, and `DocumentRendererPort` deliberately has no field for it.

    **What this use case does not do.** It declares no retry and needs none (AC-18): retrying is a
    decision that requires knowing what kind of failure occurred, and only the adapter has that
    (ADR-0014 §6). Two retry layers do not add, they multiply. It also never deletes: a failed
    render leaves whatever the store may hold to 1.6's sweep, because a delete on a failure path is
    a second way to lose a file.
    """

    def __init__(
        self,
        jobs: ExportJobRepository,
        runs: TailoringRunRepository,
        renderer: DocumentRendererPort,
        files: FileStorePort,
        events: EventPublisherPort,
        clock: Clock,
        stale_after_seconds: int = 300,
    ) -> None:
        self._jobs = jobs
        self._runs = runs
        self._renderer = renderer
        self._files = files
        self._events = events
        self._clock = clock
        self._stale_after_seconds = stale_after_seconds

    async def __call__(self, cmd: RenderExportJobCommand) -> RenderExportJobOutcome:
        # Step 1. `find`, not `get`: absence is an ordinary branch for the worker, not an exception.
        # The 24-hour purge (ADR-0006) cascades a job away while its message is still in Redis, and
        # `task_acks_late=True` makes redelivery of an already-purged id real rather than
        # theoretical (X-33).
        job = await self._jobs.find(cmd.export_job_id)
        if job is None:
            return RenderExportJobOutcome.MISSING

        # Step 2. A decided job is done, for good (XJ-4). Returning *before* the render is AC-18:
        # a redelivery cannot buy a second render. The aggregate would refuse `mark_started`
        # anyway; this branch makes the refusal a return value rather than an exception the task
        # would have to interpret (X-34).
        if job.status in (ExportJobStatus.READY, ExportJobStatus.FAILED):
            return RenderExportJobOutcome.SKIPPED

        now = self._clock.now()
        stale_after = timedelta(seconds=self._stale_after_seconds)

        # Step 3. A job that is already `RENDERING` has two very different explanations, and the
        # stale window is the only thing that tells them apart: a worker is mid-render right now
        # (leave it alone), or a worker died holding it and nobody is coming back (X-29). The rule
        # lives on the aggregate — `ExportJob.is_stale` is its one home, shared with the beat
        # sweep — so the two can never disagree about which jobs are dead.
        if job.status is ExportJobStatus.RENDERING:
            if job.is_stale(now, stale_after):
                await self._record_failure(job, ExportFailureReason.ABANDONED, now)
                return RenderExportJobOutcome.ABANDONED
            return RenderExportJobOutcome.SKIPPED

        # Step 4. **The first of two transactions.** `rendering` has to be durable *before* the
        # render, not with its outcome: the client polls throughout, and a single transaction would
        # show `queued` for the whole render and then jump to a terminal status — indistinguishable
        # from a job nobody ever picked up, which is exactly the "still working" vs. "this failed"
        # distinction the frontend owes the user. Committing is the caller's job
        # (`ExportJobRepository.save` says nothing about transactions on purpose); the worker's
        # composition root closes this one here.
        job.mark_started(now)
        try:
            await self._jobs.save(job)
        except ExportJobConcurrentlyModified:
            # Two deliveries of one job in flight at once (X-30, AC-6). The aggregate cannot see
            # the race — both copies read `queued` and both pass `ExportAlreadyStarted` against
            # themselves — so only the repository's version check settles it. The loser returns
            # **before** the render, so the fake renderer's call count stays 1. Nothing is
            # published: the start did not happen, and the `ExportStarted` that `mark_started`
            # recorded is dropped with this in-memory copy.
            return RenderExportJobOutcome.SKIPPED
        await self._events.publish(*job.release_events())

        # Step 5. Straight to the repository, with no ownership check — see the class docstring:
        # the job already encodes the authorization decision made at request time, and re-asking
        # against a session that may have expired since would fail a render for a reason that has
        # nothing to do with rendering.
        run = await self._runs.find(job.tailoring_run_id)
        if run is None:
            # Effectively unreachable: the only way a run disappears is the guest-session purge,
            # which cascades this job row away with it, so step 1 would have returned `MISSING`
            # already. Handled as `MISSING` rather than as a failure reason **because there would
            # be no job left to record a failure on** (X-35).
            return RenderExportJobOutcome.MISSING

        # Unreachable by state — a run is `succeeded` when the job is created and terminal
        # thereafter — but recorded rather than left to fall through, so an impossibility that
        # becomes possible shows up as a reason in the failure breakdown instead of as a job stuck
        # `rendering` until the sweep (X-35). The `documents is None` arm is the *same*
        # impossibility spelled the other way (TR-2: succeeded iff documents), folded into this
        # guard rather than asserted below it: one branch, no `assert` in production code, and the
        # narrowing `mypy` needs for step 6 comes free — 1.3's step 5 narrows `extracted_text` the
        # same way, by handling the impossible value rather than by claiming it cannot happen.
        documents = run.current_documents
        if run.status is not TailoringRunStatus.SUCCEEDED or documents is None:
            await self._record_failure(job, ExportFailureReason.SOURCE_UNAVAILABLE, now)
            return RenderExportJobOutcome.FAILED

        if not job.was_requested_for(run.version):
            # X-31. **Before the render**, because it is the cheapest check in the sequence and it
            # invalidates everything after it: rendering first would pay worker seconds for output
            # already known to be stale. Nothing is ever re-pointed at the new version (XJ-9) —
            # *Export again* creates a new job at the version the user can now see.
            await self._record_failure(job, ExportFailureReason.SOURCE_CHANGED, now)
            return RenderExportJobOutcome.FAILED

        # Step 6. `current_documents` is the revision if one exists, else the draft (ADR-0015 §1)
        # — this use case does not choose, it reads the one the aggregate calls current. Narrowed
        # by the guard above.
        source = documents.cv if job.document is TailoredDocumentKind.CV else documents.cover_letter

        # Step 7. **`DocumentRenderFailed` is caught here, and that is the deliberate opposite of
        # `RenderDocumentInline`**, which lets the identical exception family propagate to a 500 and
        # records nothing. ADR-0014 §2 draws the line — *was anything spent, and is there an
        # artifact to own?* Here: yes and yes. A worker second has gone and a row exists that the
        # client is polling, so the job owns the fact that it happened and the reason it produced
        # nothing (X-25…X-27, X-37, AC-19).
        #
        # The base class, not a tuple of its four subclasses: `DocumentRenderFailed` is what carries
        # `.reason`, and an allow-list of subclasses would be the same bet the extraction sweep
        # lost. For whoever is tempted to simplify this into a propagating error: the application
        # tests assert the **recording**, not the propagation, so that change turns them red.
        #
        # `perf_counter`, not the `Clock`: the port is whole-second by contract (ADR-0007) and
        # cannot measure a two-second render at all — 1.3's `llm_duration_ms` argument, unchanged.
        # Measured here rather than in the adapter because the number is this use case's to record
        # on the aggregate; an adapter returning a duration beside its bytes would be reporting on
        # itself, and `DocumentRendererPort` deliberately has no field for it.
        started = time.perf_counter()
        try:
            data = await self._renderer.render(
                source.value, document=job.document, format=job.format
            )
        except DocumentRenderFailed as exc:
            await self._record_failure(job, exc.reason, self._clock.now())
            return RenderExportJobOutcome.FAILED
        render_duration_ms = int((time.perf_counter() - started) * 1000)

        # Step 8. **File before row** (ADR-0006 §2). The bytes are stored *before* `mark_ready`
        # writes the key, so the crash window between the two leaves an **orphan file** — findable
        # by id, swept by 1.6 — rather than a `ready` row pointing at nothing, which is a 410 for a
        # file the user can see in their list. Choose the crash window whose survivor is
        # recoverable (X-28).
        try:
            await self._files.put(job.storage_ref, data)
        except FileStoreUnavailable:
            await self._record_failure(
                job, ExportFailureReason.FILE_STORE_UNAVAILABLE, self._clock.now()
            )
            return RenderExportJobOutcome.FAILED

        # Step 9. The second transaction. `clock.now()` is read again rather than reused from step
        # 4 — the whole point of two timestamps is that the gap between them is the render.
        job.mark_ready(
            byte_size=len(data), render_duration_ms=render_duration_ms, at=self._clock.now()
        )
        await self._jobs.save(job)
        await self._events.publish(*job.release_events())
        return RenderExportJobOutcome.READY

    async def _record_failure(
        self, job: ExportJob, reason: ExportFailureReason, at: datetime
    ) -> None:
        """Record a decided-as-failed job: `mark_failed`, save, then publish — in that order.

        The five failure paths above (`ABANDONED`, `SOURCE_UNAVAILABLE`, `SOURCE_CHANGED`, every
        `DocumentRenderFailed` and a failed `put`) differ only in the reason and the instant, so the
        save/publish ordering is written once. Publishing strictly **after** the save is the rule
        `ExecuteTailoringRun._record_failure` states: a publish that ran first would announce a fact
        a failed save is about to un-happen.

        `byte_size`, `render_duration_ms` and `file_key` are deliberately not touched. `mark_failed`
        already guarantees they stay `None` on every path (XJ-3), and a partially-filled row would
        show up in a byte or latency total as a real number nothing produced.
        """
        job.mark_failed(reason, at)
        await self._jobs.save(job)
        await self._events.publish(*job.release_events())
