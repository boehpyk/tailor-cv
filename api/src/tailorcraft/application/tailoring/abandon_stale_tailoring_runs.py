"""The `AbandonStaleTailoringRuns` use case: the beat sweep that records a run whose worker was lost.

Read it next to `execute_tailoring_run.ExecuteTailoringRun` step 3, which records the same outcome
for the same reason from the other side — a redelivered task that happens to arrive past the window.
The sweep exists because that path alone does not deliver AC-12 (feature-spec.md, "Amended at
/verify round 1"): a pool child killed mid-call has its message **acked**, a SIGKILLed main process
redelivers only after the broker's hour-long visibility timeout, and a redelivery that does arrive
promptly finds a fresh `running` run and returns `SKIPPED`. Each of those leaves a run `running`
for ever behind a UI that says "still working. Don't refresh." (G-25').
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from tailorcraft.domain.shared.clock import Clock
from tailorcraft.domain.shared.events import EventPublisherPort
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

    Counts rather than the runs themselves: a caller that wanted per-run detail already has it,
    because each abandonment publishes `TailoringRunFailed` carrying the run's id.
    """

    abandoned: int
    skipped: int


class AbandonStaleTailoringRuns:
    """Record every `RUNNING` run that no worker can still be working on as `failed` / `abandoned`,
    a bounded batch per tick, oldest first. Run by Celery beat every minute (G-25'); the codebase's
    first beat job.

    **No command dataclass, and the absence is deliberate.** Every other use case in this package
    takes a frozen input dataclass as its contract, because every other one acts on behalf of a
    caller who names what to act on. This one names nothing: it acts on whatever is stale *now*, and
    its two bounds — the window and the batch size — are configuration fixed at construction. A
    zero-field command would be a contract with nothing in it.

    Flow:

    1. ``now = clock.now()``; ``stale_after = timedelta(seconds=stale_after_seconds)``. **One instant
       for the whole batch**, so the listing, the re-check and every `completed_at` written this tick
       agree about when "now" was.
    2. ``candidates = await runs.list_stale_running(started_before=now - stale_after,
       limit=batch_size)`` — which runs, in what order and with what guarantees is that port
       method's docstring (`TailoringRunRepository.list_stale_running`).
    3. For each candidate, in order:

       - ``if not run.is_stale(now, stale_after)``: count it `skipped` and move on — no mark, no
         save, no publish. This is the guard against adapter drift: the query and the rule are two
         expressions of one judgement, and the aggregate's is the one that decides.
       - ``run.mark_failed(TailoringFailureReason.ABANDONED, now)``; ``await runs.save(run)``;
         ``await events.publish(*run.release_events())``; count it `abandoned`. **Save strictly
         before publish**: a publish that ran first would announce a fact a failed save is about to
         un-happen. `ExecuteTailoringRun._record_failure` performs the same three steps in the same
         order.

    4. Return ``AbandonStaleTailoringRunsResult(abandoned=..., skipped=...)``.

    **Why there is no `except TailoringAlreadyDecided` around `mark_failed` (G-36).** A run decided
    between the listing and the mark is the case that exception names, and the `is_stale` re-check
    already catches it: `is_stale` is `False` for any decided run, and there is no `await` between the
    re-check and `mark_failed`, so nothing can decide the run in that gap. A run decided while the
    loop was awaiting an *earlier* run's save or publish is seen by its own re-check and skipped. The
    catch could therefore never fire — it would be dead code wearing a docstring about a race.

    What neither guard can see is a decision written **by another process** straight to the database:
    a redelivered `ExecuteTailoringRun` reaching step 3 for the same run. The object this sweep loaded
    still says `RUNNING`, so the sweep marks and saves it, and whichever write lands second wins. That
    is G-36 and it is benign by construction: the only other writer that can act on a run past the
    window is step 3, which records the same `abandoned` reason, so the row ends in the same state
    with a `completed_at` a moment apart, and two `TailoringRunFailed` lines are logged instead of
    one. A *live* worker cannot be that other writer, because the window sits above the task's hard
    time limit and the pool child is killed before it could write — holding that inequality is the
    composition root's job, not this class's.

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
    boundary. Any failure other than the skip above propagates — a database that is down makes
    `list_stale_running` or `save` raise, the task raises, nothing further is recorded this tick, and
    the next tick tries again. That retry is safe because the sweep is idempotent by the aggregate's
    own rules: an abandoned run is no longer `RUNNING`, so it is never listed twice.

    **This layer does not log** (see `execute_tailoring_run.py`'s module comment). The per-run line
    is the published `TailoringRunFailed`; the per-tick line — `swept_count`, `duration_ms` — is the
    task's, built from the result.

    **Why the save-then-publish sequence is not extracted into a helper shared with
    `ExecuteTailoringRun._record_failure`.** The rule is not those three lines. Every use case in this
    codebase that saves an aggregate publishes after the save, `ExecuteTailoringRun` steps 4 and 7
    and `RequestTailoringRun` included, and the rule's one home is `EventPublisherPort`'s own
    docstring ("called by a use case **after** a successful save"). A helper covering one of those
    sequences would be one home for one copy of a rule that has many, bought with keyword-argument
    noise at four call sites. Each site points at the other instead.
    """

    def __init__(
        self,
        runs: TailoringRunRepository,
        events: EventPublisherPort,
        clock: Clock,
        stale_after_seconds: int = 300,
        batch_size: int = 100,
    ) -> None:
        self._runs = runs
        self._events = events
        self._clock = clock
        self._stale_after_seconds = stale_after_seconds
        self._batch_size = batch_size

    async def __call__(self) -> AbandonStaleTailoringRunsResult:
        # Step 1. Read once: the listing, every re-check and every `completed_at` share this instant.
        now = self._clock.now()
        stale_after = timedelta(seconds=self._stale_after_seconds)

        # Step 2. Bounded and oldest first, by the port's contract; the next tick takes the rest.
        candidates = await self._runs.list_stale_running(
            started_before=now - stale_after, limit=self._batch_size
        )

        abandoned = 0
        skipped = 0
        for run in candidates:
            # Step 3. The aggregate decides, not the query. `is_stale` is `False` for a decided run
            # too, and nothing is awaited between this check and `mark_failed`, which is why there
            # is no `except TailoringAlreadyDecided` below (see the class docstring, G-36).
            if not run.is_stale(now, stale_after):
                skipped += 1
                continue
            run.mark_failed(TailoringFailureReason.ABANDONED, now)
            # Save strictly before publish, as in `ExecuteTailoringRun._record_failure`.
            await self._runs.save(run)
            await self._events.publish(*run.release_events())
            abandoned += 1

        return AbandonStaleTailoringRunsResult(abandoned=abandoned, skipped=skipped)
