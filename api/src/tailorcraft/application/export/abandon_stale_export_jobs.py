"""The `AbandonStaleExportJobs` use case: the beat sweep that records an export job whose worker was
lost.

Read it next to `render_export_job.RenderExportJob` step 3, which records the same outcome for the
same reason from the other side — a redelivered task that happens to arrive past the window. The
sweep exists because that path alone does not deliver AC-20: a pool child killed mid-render has its
message **acked**, a SIGKILLed main process redelivers only after the broker's visibility timeout,
and a redelivery that does arrive promptly finds a fresh `rendering` job and returns `SKIPPED`.
Each of those leaves a job `rendering` for ever behind a control that says *Preparing…* (X-29).
"""

from __future__ import annotations

from dataclasses import dataclass

from tailorcraft.domain.export.ports import ExportJobRepository
from tailorcraft.domain.shared.clock import Clock
from tailorcraft.domain.shared.events import EventPublisherPort


@dataclass(frozen=True, slots=True)
class AbandonStaleExportJobsResult:
    """What one tick of the sweep did. Three counts, and nothing it logs is text.

    - `swept` — jobs recorded `failed` / `abandoned` this tick. The task logs it as `swept_count`.
    - `conflicts` — jobs whose `save` raised `ExportJobConcurrentlyModified` (X-39; ADR-0015 §3): a
      worker or a redelivery decided the job between the sweep's read and its write. Whichever
      wrote first stands, which is the correct outcome, so a conflict neither overwrites it nor
      aborts the batch — the sweep counts it and moves on.
    - `examined` — how many candidates the repository handed over this tick. It is the bound's own
      readout: `examined == batch_limit` is the signal that a backlog is being worked through a
      batch at a time and the next tick has more to do, which is exactly what an operator wants to
      see after an outage and cannot infer from `swept` alone.

    **The field names differ from `AbandonStaleTailoringRunsResult`'s (`abandoned`, `skipped`,
    `conflicts`), and the difference is deliberate rather than drift.** `swept` is the count in the
    task's log line and the word the ADR uses for this sweep; `examined` replaces `skipped` because
    it is the more useful of the two derivable numbers — the skips are `examined - swept -
    conflicts`, and a non-zero skip count with no concurrent writer still reads out of that
    subtraction as the adapter-drift signal it is. Making the two results identical would have been
    the first step toward the generalization the class docstring below refuses.

    Counts rather than the jobs themselves: a caller wanting per-job detail already has it, because
    each abandonment publishes `ExportFailed` carrying the job's id — and a conflict publishes
    nothing, because its save never landed and there is no fact to announce.

    All three fields are required. A default on a count is a way for a construction site to forget
    one, which 1.4 learned on `conflicts` the slow way.
    """

    swept: int
    conflicts: int
    examined: int


class AbandonStaleExportJobs:
    """Record every `RENDERING` job that no worker can still be working on as `failed` /
    `abandoned`, a bounded batch per tick, oldest first. Run by Celery beat every 60 s (AC-20).

    **No command dataclass, and the absence is deliberate**, exactly as in
    `AbandonStaleTailoringRuns`. Every other use case in this package takes a frozen input
    dataclass as its contract, because every other one acts on behalf of a caller who names what to
    act on. This one names nothing: it acts on whatever is stale *now*, and its two bounds — the
    window and the batch size — are configuration fixed at construction. A zero-field command would
    be a contract with nothing in it.

    Flow (technical-plan.md, "Application layer" §7; T7 implements it):

    1. ``now = clock.now()``; ``stale_after = timedelta(seconds=stale_after_seconds)``. **One
       instant for the whole batch**, so the listing, every re-check and every `completed_at`
       written this tick agree about when "now" was.
    2. ``candidates = await jobs.list_stale_rendering(started_before=now - stale_after,
       limit=batch_limit)`` — which jobs, in what order and with what guarantees is that port
       method's docstring.
    3. For each candidate, in order:

       - ``if not job.is_stale(now, stale_after)``: skip it — no mark, no save, no publish, and no
         count of its own beyond `examined`. This is the guard against adapter drift: the query and
         the aggregate's rule are two expressions of one judgement, and the aggregate's decides.
       - ``job.mark_failed(ExportFailureReason.ABANDONED, now)``; ``await jobs.save(job)``;
         ``await events.publish(*job.release_events())``; count it `swept`. **Save strictly before
         publish**: a publish that ran first would announce a fact a failed save is about to
         un-happen.
       - ``except ExportJobConcurrentlyModified``: count it `conflicts` and move on (X-39). The
         batch is not aborted — the next job in it is still stale and still waiting for its answer.

    4. Return ``AbandonStaleExportJobsResult(swept=..., conflicts=..., examined=len(candidates))``.

    **This is `AbandonStaleTailoringRuns` line for line with the aggregate swapped, and it is
    deliberately not generalized over the two** (ADR-0016 (c)). A `StaleSweep[Aggregate]` taking a
    repository, a lister and a "how to abandon" callable is the obvious refactor and it is the
    wrong one: **the two sweeps share a shape and not a rule.** The shape is a bounded ordered
    batch with a re-check and a tolerated conflict. The rule is *what a lost worker means here*,
    and it differs — a lost tailoring run means a paid call that may or may not have happened and a
    user owed twelve seconds of explanation; a lost export means a worker second and possibly an
    orphan file under a key 1.6 will sweep. The transitions differ too (`TailoringFailureReason` vs
    `ExportFailureReason`, two enums that only look alike), and a generic sweep would have to take
    the transition as a parameter — which is a strategy object guessing at the one thing that makes
    each sweep what it is. **Two thirty-line use cases are cheaper than one abstraction with a type
    parameter and a strategy**, and they can diverge without anyone having to prove the divergence
    is safe for the other. CLAUDE.md's rule for aggregates — shared shape is not shared behaviour —
    applies to the use cases over them.

    **Why no `except ExportAlreadyDecided` around `mark_failed`.** A job decided between the listing
    and the mark is the case that exception names, and the `is_stale` re-check already catches it:
    `is_stale` is `False` for any decided job, and there is no `await` between the re-check and
    `mark_failed`, so nothing in this process can decide the job in that gap. A job decided while
    the loop was awaiting an *earlier* job's save or publish is seen by its own re-check. The catch
    could therefore never fire — it would be dead code wearing a docstring about a race.

    What neither guard can see is a decision written **by another process** straight to the
    database: a redelivered `RenderExportJob` reaching step 3 for the same job. The object this
    sweep loaded still says `RENDERING`, so the sweep marks it and tries to save. The version check
    settles it — the write that lands first stands, the second is refused as
    `ExportJobConcurrentlyModified`, and the sweep counts it and carries on (X-39). A *live* worker
    cannot be that other writer, because the window sits above the task's hard time limit and the
    pool child is killed before it could write; holding that inequality is the composition root's
    job (`create_celery`'s second startup guard, D3), not this class's.

    **Why not re-enqueue each stale job instead?** Because redelivery is the mechanism that just
    failed. A re-enqueued message would travel through the same broker and the same worker that
    lost the job, and if it arrived it would only reach step 3 and record what this class records
    directly, one round trip later. It would never re-render either: an abandoned job stays
    abandoned, and *Export again* creates a new job at the current version.

    **Why no new event?** `ExportFailed(reason=abandoned)` already states the business fact. An
    `ExportSwept` would describe the *mechanism* that noticed, which no listener needs, and it
    would widen a field set the privacy test fixes.

    **Why the batch bound?** After an outage the backlog can be every job that was in flight. One
    tick stays bounded in rows loaded, in memory and in duration whatever the backlog, and the next
    tick takes the rest sixty seconds later. Oldest first means the people who have waited longest
    get their answer first. The tick expires at 55 s, under the interval, so two ticks never
    overlap on the same rows.

    **Transactions and failure.** This use case does not commit, exactly as `RenderExportJob` does
    not: the beat task's composition root decides the boundary, and its committing repository
    contains a refused save in a SAVEPOINT so the other candidates this loop still holds keep their
    loaded state. That is not a nicety — a whole-session rollback expires the entire identity map
    *inside* the flush, and the next `is_stale` would be a lazy load on an `AsyncSession`, i.e.
    `MissingGreenlet` (CLAUDE.md, measured at 1.4's verify). Any failure other than the skip and
    the conflict above propagates: a database that is down makes `list_stale_rendering` or `save`
    raise, the task raises, nothing further is recorded this tick, and the next tick tries again.
    That retry is safe because the sweep is idempotent by the aggregate's own rules — an abandoned
    job is no longer `RENDERING`, so it is never listed twice.

    **This layer does not log** (see `render_export_job.py`'s module comment). The per-job line is
    the published `ExportFailed`; the per-tick line — `swept_count`, `conflicts`, `duration_ms` —
    is the task's, built from the result.
    """

    def __init__(
        self,
        jobs: ExportJobRepository,
        events: EventPublisherPort,
        clock: Clock,
        stale_after_seconds: int = 300,
        batch_limit: int = 100,
    ) -> None:
        self._jobs = jobs
        self._events = events
        self._clock = clock
        self._stale_after_seconds = stale_after_seconds
        self._batch_limit = batch_limit

    async def __call__(self) -> AbandonStaleExportJobsResult:
        raise NotImplementedError
