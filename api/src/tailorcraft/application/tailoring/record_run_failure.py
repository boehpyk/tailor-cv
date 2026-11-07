"""`RecordRunFailure`: the one place a run is recorded as failed (slice 5.2, plan §0.4)."""

from __future__ import annotations

from datetime import datetime

from tailorcraft.application.billing.refund_credit_for_run import RefundCreditForRun
from tailorcraft.domain.shared.counters import DailyCounterPort
from tailorcraft.domain.shared.events import EventPublisherPort
from tailorcraft.domain.tailoring.ports import TailoringRunRepository
from tailorcraft.domain.tailoring.tailoring_run import TailoringRun
from tailorcraft.domain.tailoring.value_objects import TailoringFailureReason


class RecordRunFailure:
    """Mark, refund, count, save, publish: in that order, one transaction.

    **Why this is a shared collaborator when the failure sites used not to share one.** Until 5.2,
    `ExecuteTailoringRun._record_failure`, `AbandonStaleTailoringRuns` and the router's
    `record_not_queued` each wrote *mark, save, publish*, and their docstrings said why a helper over
    three lines was not worth it. A failure now also gives a credit back and counts `runs_failed`,
    and a money rule written three times is a money rule that drifts. So this is the one copy, and the
    five failure sites (abandoned, base CV deleted, CV text missing, the LLM's failures, not queued)
    reach it from three use cases.

    Flow of `__call__(run, reason, at)`:

    1. `run.mark_failed(reason, at)` (TR-3 / TR-4 as today; `TailoringRunAlreadyDecided` propagates).
    2. `refund(RefundCreditCommand(run.owner, ChargedRunRef(run.id.value), at))`: a guest owner, or an
       uncharged run, writes nothing (F-19, F-28).
    3. `counters.increment(RUNS_FAILED, at.date())`.
    4. `runs.save(run)`: **the worker's commit point**. The refund and the count were written first so
       that commit carries them. A failed save rolls the whole transaction back in the worker
       (`CommittingTailoringRunRepository`), refund included (F-14), and the error propagates
       (`TailoringRunConcurrentlyModified` for a lost race).
    5. `events.publish(*run.release_events(), *refund_events)`: after the save, never before.
    """

    def __init__(
        self,
        runs: TailoringRunRepository,
        refund: RefundCreditForRun,
        counters: DailyCounterPort,
        events: EventPublisherPort,
    ) -> None:
        self._runs = runs
        self._refund = refund
        self._counters = counters
        self._events = events

    async def __call__(
        self, run: TailoringRun, reason: TailoringFailureReason, at: datetime
    ) -> None:
        raise NotImplementedError
