"""Application tests for `AbandonStaleTailoringRuns`' credits and recovery (slice 5.2, T14 RED, AC-9,
AC-10, AC-12, F-11, F-12, F-13, F-14, plan §0.4, §0.5).

The sweep handles **two lists in one tick**, each bounded by the same `batch_size`: stale `running`
runs (`abandoned`) and `queued` runs whose task was lost, past `queued_lost_after_seconds` (`not
queued`, strict, like `is_stale`). Both record through `RecordRunFailure`, so a charged run is
refunded and counted. It lists **ids** and re-reads each run by id before judging it, because a
worker's failed save now rolls back the whole transaction and expires every instance the loop still
holds (plan §0.4).

Conflict containment (F-14) is asserted from the outside: the loser is counted and the batch goes on.
That the loser's *refund and count* are rolled back with its save is in `test_pending_adapters.py`.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta

from tailorcraft.application.tailoring.abandon_stale_tailoring_runs import (
    AbandonStaleTailoringRuns,
    AbandonStaleTailoringRunsResult,
)
from tailorcraft.application.tailoring.execute_tailoring_run import (
    ExecuteTailoringRun,
    ExecuteTailoringRunCommand,
    ExecuteTailoringRunOutcome,
)
from tailorcraft.application.tailoring.record_run_failure import RecordRunFailure
from tailorcraft.domain.billing.value_objects import ChargedRunRef, CreditEntryKind
from tailorcraft.domain.shared.counters import DemandCounter
from tailorcraft.domain.tailoring.errors import TailoringRunConcurrentlyModified
from tailorcraft.domain.tailoring.events import TailoringRunFailed
from tailorcraft.domain.tailoring.tailoring_run import TailoringRun
from tailorcraft.domain.tailoring.value_objects import (
    TailoringFailureReason,
    TailoringRunId,
    TailoringRunStatus,
)
from tailorcraft.infrastructure.clock import FixedClock
from tests.integration.fakes import FakeBaseCvRepository, FakeJobPostingRepository, FakeLlm
from tests.integration.owners import a_draft, running_run
from tests.integration.tailoring.credits_support import JournalRuns, Rig

_STALE = timedelta(seconds=301)  # past the default 300 s window
_LOST = 1800  # queued_lost_after_seconds


def _sweep(
    rig: Rig, clock: FixedClock, *, batch_size: int = 100, lost_after: int = _LOST
) -> AbandonStaleTailoringRuns:
    return AbandonStaleTailoringRuns(
        rig.runs,
        clock,
        batch_size=batch_size,
        record_failure=rig.record_failure,
        queued_lost_after_seconds=lost_after,
    )


async def _failed_events(rig: Rig) -> dict[TailoringRunId, TailoringFailureReason]:
    return {
        e.tailoring_run_id: e.reason
        for e in rig.events.published
        if isinstance(e, TailoringRunFailed)
    }


async def test_a_stale_charged_run_is_abandoned_refunded_and_counted(clock: FixedClock) -> None:
    rig = Rig(clock.now())
    run = await rig.run(running=True, age=_STALE)

    result = await _sweep(rig, clock)()

    assert result == AbandonStaleTailoringRunsResult(
        abandoned=1, skipped=0, conflicts=0, lost_queued=0
    )
    stored = await rig.runs.get(run.id)
    assert (stored.status, stored.failure_reason) == (
        TailoringRunStatus.FAILED,
        TailoringFailureReason.ABANDONED,
    )
    assert await rig.balance() == 1
    assert rig.count(DemandCounter.RUNS_FAILED) == 1


async def test_a_lost_queued_charged_run_is_failed_not_queued_refunded_and_counted(
    clock: FixedClock,
) -> None:
    """AC-12, F-11: recovery of a run whose task never arrived."""
    rig = Rig(clock.now())
    run = await rig.run(age=timedelta(seconds=_LOST + 1))

    result = await _sweep(rig, clock)()

    assert result == AbandonStaleTailoringRunsResult(
        abandoned=0, skipped=0, conflicts=0, lost_queued=1
    )
    stored = await rig.runs.get(run.id)
    assert (stored.status, stored.failure_reason, stored.completed_at) == (
        TailoringRunStatus.FAILED,
        TailoringFailureReason.NOT_QUEUED,
        clock.now(),
    )
    assert await rig.balance() == 1
    assert rig.count(DemandCounter.RUNS_FAILED) == 1
    assert (await _failed_events(rig))[run.id] is TailoringFailureReason.NOT_QUEUED


async def test_a_run_queued_exactly_the_window_ago_is_not_yet_lost(clock: FixedClock) -> None:
    """Strict, like `is_stale`: the boundary instant belongs to the run."""
    rig = Rig(clock.now())
    run = await rig.run(age=timedelta(seconds=_LOST))

    result = await _sweep(rig, clock)()

    assert result.lost_queued == 0
    assert (await rig.runs.get(run.id)).status is TailoringRunStatus.QUEUED
    assert await rig.balance() == 0
    assert rig.counters.calls == []


async def test_the_lost_window_is_the_constructors_not_a_constant(clock: FixedClock) -> None:
    rig = Rig(clock.now())
    run = await rig.run(age=timedelta(seconds=901))

    default = await _sweep(rig, clock)()
    assert default.lost_queued == 0
    custom = await _sweep(rig, clock, lost_after=900)()

    assert custom.lost_queued == 1
    assert (await rig.runs.get(run.id)).failure_reason is TailoringFailureReason.NOT_QUEUED


async def test_a_queued_run_is_never_abandoned_and_a_running_one_is_never_lost(
    clock: FixedClock,
) -> None:
    """A `queued` run, however old, is not `abandoned` (that belongs to a started run); a `running`
    run younger than the stale window is left to its worker, however old its request."""
    rig = Rig(clock.now(), extra=1)
    queued = await rig.run(age=timedelta(seconds=_LOST - 60))
    live = await rig.run(running=True, age=timedelta(seconds=10))

    result = await _sweep(rig, clock)()

    assert result == AbandonStaleTailoringRunsResult(
        abandoned=0, skipped=0, conflicts=0, lost_queued=0
    )
    assert (await rig.runs.get(queued.id)).status is TailoringRunStatus.QUEUED
    assert (await rig.runs.get(live.id)).status is TailoringRunStatus.RUNNING


async def test_both_lists_are_swept_in_one_tick_each_bounded_by_the_batch_size(
    clock: FixedClock,
) -> None:
    rig = Rig(clock.now(), extra=5)
    for _ in range(3):
        await rig.run(running=True, age=_STALE)
    for _ in range(3):
        await rig.run(age=timedelta(seconds=_LOST + 1))

    result = await _sweep(rig, clock, batch_size=2)()

    assert (result.abandoned, result.lost_queued) == (2, 2)
    assert rig.count(DemandCounter.RUNS_FAILED) == 4


async def test_the_lost_list_takes_the_oldest_first(clock: FixedClock) -> None:
    rig = Rig(clock.now(), extra=1)
    newer = await rig.run(age=timedelta(seconds=_LOST + 10))
    older = await rig.run(age=timedelta(seconds=_LOST + 500))

    await _sweep(rig, clock, batch_size=1)()

    assert (await rig.runs.get(older.id)).status is TailoringRunStatus.FAILED
    assert (await rig.runs.get(newer.id)).status is TailoringRunStatus.QUEUED


async def test_an_uncharged_stale_run_is_abandoned_with_no_refund(clock: FixedClock) -> None:
    """AC-10, F-19."""
    rig = Rig(clock.now())
    run = await rig.run(charged=False, running=True, age=_STALE)

    result = await _sweep(rig, clock)()

    assert result.abandoned == 1
    assert (await rig.runs.get(run.id)).status is TailoringRunStatus.FAILED
    assert rig.credits.save_calls == 0
    assert await rig.balance() == 1
    assert rig.count(DemandCounter.RUNS_FAILED) == 1


async def test_an_uncharged_lost_queued_run_is_recovered_with_no_refund(clock: FixedClock) -> None:
    rig = Rig(clock.now())
    run = await rig.run(charged=False, age=timedelta(seconds=_LOST + 1))

    result = await _sweep(rig, clock)()

    assert result.lost_queued == 1
    assert (await rig.runs.get(run.id)).status is TailoringRunStatus.FAILED
    assert rig.credits.save_calls == 0


class _ListsAStaleTwin(JournalRuns):
    """`list_stale_running` hands back a RUNNING, stale twin of a run that is already FAILED in the
    store, with the same id: the shape of a listed instance that went stale in memory because a
    rollback or another worker decided the row meanwhile."""

    def __init__(self, journal: list[str], twin: TailoringRun) -> None:
        super().__init__(journal)
        self._twin = twin
        self.find_calls: list[TailoringRunId] = []

    async def list_stale_running(
        self, started_before: datetime, limit: int
    ) -> Sequence[TailoringRun]:
        return [self._twin]

    async def find(self, run_id: TailoringRunId) -> TailoringRun | None:
        self.find_calls.append(run_id)
        return await super().find(run_id)


async def test_each_listed_run_is_re_read_by_id_and_a_decided_row_is_skipped(
    clock: FixedClock,
) -> None:
    """Ids first, then `find` per id (plan §0.4): the sweep judges the row as it is now, never the
    instance the listing returned. Working from the twin would fail an already-failed run again."""
    rig = Rig(clock.now())
    stored = await rig.run(running=True, age=_STALE)
    await rig.record_failure(stored, TailoringFailureReason.LLM_ERROR, clock.now())  # decided
    twin = running_run(rig.owner, clock.now() - _STALE, run_id=stored.id)
    runs = _ListsAStaleTwin(rig.journal, twin)
    await runs.add(stored)
    rig.runs = runs
    rig.journal.clear()
    sweep = AbandonStaleTailoringRuns(
        runs,
        clock,
        record_failure=RecordRunFailure(runs, rig.refund, rig.counters, rig.events),
    )

    result = await sweep()

    assert runs.find_calls == [stored.id]
    assert result == AbandonStaleTailoringRunsResult(
        abandoned=0, skipped=1, conflicts=0, lost_queued=0
    )
    assert rig.journal == []  # nothing written: not refunded again, not counted again


class _OneRunLosesItsSave(JournalRuns):
    def __init__(self, journal: list[str], loser: TailoringRunId) -> None:
        super().__init__(journal)
        self._loser = loser

    async def save(self, run: TailoringRun) -> None:
        if run.id == self._loser:
            raise TailoringRunConcurrentlyModified(run.id)
        await super().save(run)


async def test_a_lost_race_is_counted_and_the_next_run_is_still_refunded(
    clock: FixedClock,
) -> None:
    """F-14 from outside: exactly one side decides each run; the loser does not stop the batch."""
    rig = Rig(clock.now(), extra=1)
    first = await rig.run(running=True, age=timedelta(seconds=900))
    second = await rig.run(running=True, age=_STALE)
    runs = _OneRunLosesItsSave(rig.journal, first.id)
    for run in (first, second):
        await runs.add(run)
    sweep = AbandonStaleTailoringRuns(
        runs,
        clock,
        record_failure=RecordRunFailure(runs, rig.refund, rig.counters, rig.events),
    )

    result = await sweep()

    assert (result.abandoned, result.conflicts) == (1, 1)
    failed = await _failed_events(rig)
    assert second.id in failed
    assert first.id not in failed  # no event announces a failure that did not land
    assert (await runs.get(second.id)).status is TailoringRunStatus.FAILED
    account = await rig.credits.find(rig.owner.user_id)
    assert account is not None
    refunded = [e.run for e in account.entries if e.kind is CreditEntryKind.REFUND]
    assert ChargedRunRef(value=second.id.value) in refunded


async def test_a_task_delivered_after_the_recovery_is_skipped_with_no_llm_call(
    clock: FixedClock,
) -> None:
    """AC-12: sweep, then the lost task finally arrives."""
    rig = Rig(clock.now())
    run = await rig.run(age=timedelta(seconds=_LOST + 1))
    await _sweep(rig, clock)()
    llm = FakeLlm(a_draft())
    late = ExecuteTailoringRun(
        rig.runs,
        FakeBaseCvRepository(),
        FakeJobPostingRepository(),
        llm,
        rig.events,
        clock,
        record_failure=rig.record_failure,
        counters=rig.counters,
    )
    balance_after_recovery = await rig.balance()

    outcome = await late(ExecuteTailoringRunCommand(tailoring_run_id=run.id))

    assert outcome is ExecuteTailoringRunOutcome.SKIPPED
    assert llm.calls == []
    assert await rig.balance() == balance_after_recovery == 1
    assert rig.count(DemandCounter.RUNS_FAILED) == 1
