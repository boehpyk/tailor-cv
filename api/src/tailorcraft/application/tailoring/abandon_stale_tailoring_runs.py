"""The `AbandonStaleTailoringRuns` use case: the beat sweep that records a run whose worker was lost.

Read it next to `execute_tailoring_run.ExecuteTailoringRun` step 3, which records the same outcome
for the same reason from the other side — a redelivered task that happens to arrive past the window.
The sweep exists because that path alone does not deliver AC-12 (feature-spec.md, "Amended at
/verify round 1"): a pool child killed mid-call has its message **acked**, a SIGKILLed main process
redelivers only after the broker's visibility timeout (an hour by kombu's default, 600 s now), and a
redelivery that does arrive
promptly finds a fresh `running` run and returns `SKIPPED`. Each of those leaves a run `running`
for ever behind a UI that says "still working. Don't refresh." (G-25').
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from tailorcraft.application.tailoring.record_run_failure import RecordRunFailure
from tailorcraft.domain.shared.clock import Clock
from tailorcraft.domain.tailoring.errors import TailoringRunConcurrentlyModified
from tailorcraft.domain.tailoring.ports import TailoringRunRepository
from tailorcraft.domain.tailoring.value_objects import TailoringFailureReason


@dataclass(frozen=True, slots=True)
class AbandonStaleTailoringRunsResult:
    """What one tick of the sweep did — the beat task's whole return vocabulary, and nothing it logs
    is text.

    - `abandoned` — runs recorded `failed` / `abandoned` this tick. The task logs it as
      `swept_count` (G-25').
    - `skipped` — runs the repository listed that `TailoringRun.is_stale` did not judge stale by the
      time the loop reached them: decided in the meantime by something holding the same object, or
      never stale at all, which means the stale-run query and the aggregate's rule disagree. Neither
      is an error and neither aborts the batch. A non-zero `skipped` with no concurrent writer is the
      signature of adapter drift, and worth a look rather than a retry.
    - `conflicts` — runs whose `save` raised `TailoringRunConcurrentlyModified` (E-20; ADR-0015
      §3): a worker or a redelivery decided the run between the sweep's read and its write.
      Whichever wrote first stands, which is the correct outcome, so a conflict neither overwrites
      it nor aborts the batch — the sweep counts it and moves on to the next run.

    Counts rather than the runs themselves: a caller that wanted per-run detail already has it,
    because each abandonment publishes `TailoringRunFailed` carrying the run's id — and a conflict
    publishes nothing, because its save never landed and there is no fact to announce.

    All three fields are required, `conflicts` included: it was added in slice 1.4 and briefly
    carried a default so it could land ahead of the tests, but a default on a count is a way for a
    construction site to forget one, and every site now names all three.
    """

    abandoned: int
    skipped: int
    conflicts: int
    # Slice 5.2 (plan §0.5): queued runs past `queued_lost_after_seconds`, recorded `not_queued`.
    lost_queued: int


class AbandonStaleTailoringRuns:
    """Record every `RUNNING` run that no worker can still be working on as `failed` / `abandoned`,
    a bounded batch per tick, oldest first. Run by Celery beat every minute (G-25'); the codebase's
    first beat job.

    **No command dataclass, and the absence is deliberate.** Every other use case in this package
    takes a frozen input dataclass as its contract, because every other one acts on behalf of a
    caller who names what to act on. This one names nothing: it acts on whatever is stale *now*, and
    its two bounds — the window and the batch size — are configuration fixed at construction. A
    zero-field command would be a contract with nothing in it.

    Flow (slice 5.2, plan §0.4, §0.5):

    1. ``now = clock.now()``. **One instant for the whole batch**, so the listings, the re-checks and
       every `completed_at` written this tick agree about when "now" was.
    2. List **ids**, from both lists, each bounded by `batch_size`: stale `RUNNING` runs
       (`list_stale_running`) and `QUEUED` runs whose task was lost (`list_lost_queued`, past
       `queued_lost_after_seconds`). Ids, not instances: a conflicting save now rolls back the
       worker's whole transaction (so the loser's refund goes with it), which expires every instance
       still held, and an async session refuses the lazy refresh a stale instance would need.
    3. Per id: ``find`` it again and re-check it on the aggregate (`is_stale` / `is_lost_in_queue`).
       Gone or no longer due → `skipped`. Otherwise `RecordRunFailure` with `ABANDONED` /
       `NOT_QUEUED` (mark, refund, count, save, publish) → `abandoned` / `lost_queued`.
       ``except TailoringRunConcurrentlyModified`` → `conflicts`, and on to the next id (E-20,
       F-14): whichever side wrote first stands.

    **Why there is no `except TailoringAlreadyDecided` around `mark_failed` (G-36).** A run decided
    between the listing and the mark is the case that exception names, and the `is_stale` re-check
    already catches it: `is_stale` is `False` for any decided run, and there is no `await` between the
    re-check and `mark_failed`, so nothing can decide the run in that gap. A run decided while the
    loop was awaiting an *earlier* run's save or publish is seen by its own re-check and skipped. The
    catch could therefore never fire — it would be dead code wearing a docstring about a race.

    What neither guard can see is a decision written **by another process** straight to the database:
    a redelivered `ExecuteTailoringRun` reaching step 3 for the same run. The object this sweep loaded
    still says `RUNNING`, so the sweep marks it and tries to save it. Since slice 1.4 the repository's
    version check (ADR-0015 §3) is what settles that race: the write that lands first stands, the
    second is refused as `TailoringRunConcurrentlyModified`, and the sweep counts it as a conflict
    and carries on (E-20). In 1.3 the second write simply won, and that was benign by construction —
    the only other writer that can act on a run past the window is step 3, which records the same
    `abandoned` reason — so the version check buys correctness in the count, not in the row: one
    `TailoringRunFailed` per run instead of two. A *live* worker cannot be that other writer,
    because the window sits above the task's hard time limit and the pool child is killed before it
    could write — holding that inequality is the composition root's job, not this class's.

    **Why not re-enqueue each stale run through `ExecuteTailoringRun` instead?** Because redelivery is
    the mechanism that just failed. A re-enqueued message would travel through the same broker and
    the same worker that lost the run, and if it arrived it would only reach step 3 and record what
    this class records directly, one round trip later. It would never call the model again either:
    an abandoned run stays abandoned, and "Try again" creates a new run (TR-6).

    **Why no new event?** `TailoringRunFailed(reason=abandoned)` already states the business fact. A
    `TailoringRunSwept` would describe the *mechanism* that noticed, which no listener needs, and it
    would widen the field sets AC-22 fixes. Those field sets are unchanged.

    **Why the batch bound?** After an outage the backlog can be every run that was in flight. One
    tick stays bounded in rows loaded, in memory and in duration whatever the backlog, and the next
    tick takes the rest a minute later. Oldest first means the people who have waited longest get
    their answer first.

    **Transactions and failure (G-35).** This use case does not commit, exactly as
    `ExecuteTailoringRun` does not: the beat task's composition root decides the transaction
    boundary, one commit per recorded run, and since 5.2 a refused save rolls the whole transaction
    back so the loser's refund and count go with it (plan §0.4). That is why the loop holds ids, not
    instances. Any failure other than the skip and the conflict above propagates: a database that
    is down makes a listing or a save raise, the task raises, and the next tick tries again. That
    retry is safe because the sweep is idempotent by the aggregate's own rules: a failed run is no
    longer `RUNNING` or `QUEUED`, so it is never listed twice.

    **This layer does not log** (see `execute_tailoring_run.py`'s module comment). The per-run line
    is the published `TailoringRunFailed`; the per-tick line — `swept_count`, `duration_ms` — is the
    task's, built from the result.

    **Why the recording goes through `RecordRunFailure` (slice 5.2).** Until 5.2 this loop wrote
    *mark, save, publish* itself and said why a helper over three lines was not worth it. A failure
    now also refunds and counts, and a money rule written at every failure site drifts, so the one
    copy is `RecordRunFailure`'s; its docstring has the order and why.
    """

    def __init__(
        self,
        runs: TailoringRunRepository,
        clock: Clock,
        stale_after_seconds: int = 300,
        batch_size: int = 100,
        *,
        record_failure: RecordRunFailure,
        queued_lost_after_seconds: int = 1800,
    ) -> None:
        self._record_failure = record_failure
        self._queued_lost_after_seconds = queued_lost_after_seconds
        self._runs = runs
        self._clock = clock
        self._stale_after_seconds = stale_after_seconds
        self._batch_size = batch_size

    async def __call__(self) -> AbandonStaleTailoringRunsResult:
        now = self._clock.now()
        stale_after = timedelta(seconds=self._stale_after_seconds)
        lost_after = timedelta(seconds=self._queued_lost_after_seconds)

        # Ids first, both lists, before any write: see the class docstring's step 2.
        stale_ids = [
            run.id
            for run in await self._runs.list_stale_running(
                started_before=now - stale_after, limit=self._batch_size
            )
        ]
        lost_ids = [
            run.id
            for run in await self._runs.list_lost_queued(
                requested_before=now - lost_after, limit=self._batch_size
            )
        ]

        abandoned = lost_queued = skipped = conflicts = 0
        for run_id in stale_ids:
            run = await self._runs.find(run_id)
            # The aggregate decides, not the query. Nothing is awaited between this re-check and
            # `mark_failed` inside `RecordRunFailure`, which is why there is no
            # `except TailoringAlreadyDecided` (G-36).
            if run is None or not run.is_stale(now, stale_after):
                skipped += 1
                continue
            try:
                await self._record_failure(run, TailoringFailureReason.ABANDONED, now)
            except TailoringRunConcurrentlyModified:
                conflicts += 1
                continue
            abandoned += 1

        for run_id in lost_ids:
            run = await self._runs.find(run_id)
            if run is None or not run.is_lost_in_queue(now, lost_after):
                skipped += 1
                continue
            try:
                await self._record_failure(run, TailoringFailureReason.NOT_QUEUED, now)
            except TailoringRunConcurrentlyModified:
                conflicts += 1
                continue
            lost_queued += 1

        return AbandonStaleTailoringRunsResult(
            abandoned=abandoned, skipped=skipped, conflicts=conflicts, lost_queued=lost_queued
        )
