"""Application tests for `RecordRunFailure` (slice 5.2, T14 RED, AC-9, AC-10, AC-11, F-3 .. F-9, F-19).

The one place a run is recorded failed: mark, refund, count `runs_failed`, save, publish, in that
order (plan §0.4). The order is asserted from a shared journal, because it is what lets the worker's
single commit carry the refund and the count.

Not provable here, on fakes with no transaction: that a failed save takes the refund and the count
with it (AC-9's "neither survives", F-14). Those are in `test_pending_adapters.py`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tailorcraft.domain.billing.events import CreditRefunded
from tailorcraft.domain.billing.value_objects import ChargedRunRef, CreditEntryKind
from tailorcraft.domain.identity.ownership import GuestOwner
from tailorcraft.domain.identity.value_objects import GuestSessionId
from tailorcraft.domain.shared.counters import DemandCounter
from tailorcraft.domain.tailoring.errors import (
    TailoringAlreadyDecided,
    TailoringRunConcurrentlyModified,
)
from tailorcraft.domain.tailoring.events import TailoringRunFailed
from tailorcraft.domain.tailoring.value_objects import TailoringFailureReason, TailoringRunStatus
from tailorcraft.infrastructure.clock import FixedClock
from tests.integration.billing.support import a_user_id
from tests.integration.fakes import RecordingDailyCounter
from tests.integration.owners import a_draft, queued_run, running_run
from tests.integration.tailoring.credits_support import Rig


class _DatabaseDown(Exception):
    """Not a `RuntimeError`: `NotImplementedError` is one, and would satisfy the assertion."""


_LATE = datetime(2026, 11, 7, 23, 59, 59, tzinfo=UTC)

# Every reason a run can fail with. `ABANDONED` belongs to a run that was `running`; `NOT_QUEUED` is
# the one failure of a run that never started. Every other reason is recorded from `running`.
_REASONS = list(TailoringFailureReason)


def test_the_closed_set_of_reasons_is_still_ten() -> None:
    """A new reason must be given a refund test on purpose: this is the tripwire."""
    assert len(_REASONS) == 10


@pytest.mark.parametrize("reason", _REASONS)
async def test_a_charged_runs_failure_refunds_the_credit_and_counts_it(
    reason: TailoringFailureReason, clock: FixedClock
) -> None:
    rig = Rig(clock.now())
    run = await rig.run(running=reason is not TailoringFailureReason.NOT_QUEUED)
    assert await rig.balance() == 0

    await rig.record_failure(run, reason, clock.now())

    assert (run.status, run.failure_reason, run.completed_at) == (
        TailoringRunStatus.FAILED,
        reason,
        clock.now(),
    )
    assert await rig.balance() == 1
    account = await rig.credits.find(rig.owner.user_id)
    assert account is not None
    refunds = [e for e in account.entries if e.kind is CreditEntryKind.REFUND]
    assert [(e.credits, e.run, e.at) for e in refunds] == [
        (1, ChargedRunRef(value=run.id.value), clock.now())
    ]
    assert rig.count(DemandCounter.RUNS_FAILED) == 1
    assert len(rig.counters.calls) == 1


async def test_the_refund_and_the_count_are_written_before_the_save_and_events_follow_it(
    clock: FixedClock,
) -> None:
    rig = Rig(clock.now())
    run = await rig.run(running=True)

    await rig.record_failure(run, TailoringFailureReason.LLM_TIMED_OUT, clock.now())

    assert rig.journal == [
        "credits.save",
        "count:runs_failed",
        "runs.save:failed",
        "events.publish",
    ]


async def test_it_publishes_the_failure_and_the_refund_and_nothing_else(
    clock: FixedClock,
) -> None:
    rig = Rig(clock.now())
    run = await rig.run(running=True)

    await rig.record_failure(run, TailoringFailureReason.LLM_REFUSED, clock.now())

    assert sorted(type(e).__name__ for e in rig.events.published) == [
        "CreditRefunded",
        "TailoringRunFailed",
    ]
    failed = next(e for e in rig.events.published if isinstance(e, TailoringRunFailed))
    assert (failed.tailoring_run_id, failed.reason) == (run.id, TailoringFailureReason.LLM_REFUSED)
    refunded = next(e for e in rig.events.published if isinstance(e, CreditRefunded))
    assert refunded.run == ChargedRunRef(value=run.id.value)


async def test_an_uncharged_runs_failure_refunds_nothing_but_is_still_counted(
    clock: FixedClock,
) -> None:
    """AC-10, F-19, F-28: a run made by the old API during the deploy window, or a guest run claimed
    before release, has no charge, so its failure writes no entry."""
    rig = Rig(clock.now())
    run = await rig.run(charged=False, running=True)
    entries_before = await rig.entries()

    await rig.record_failure(run, TailoringFailureReason.LLM_ERROR, clock.now())

    assert run.status is TailoringRunStatus.FAILED
    assert await rig.entries() == entries_before
    assert await rig.balance() == 1
    assert rig.credits.save_calls == 0
    assert "credits.save" not in rig.journal
    assert rig.count(DemandCounter.RUNS_FAILED) == 1
    assert [type(e) for e in rig.events.published] == [TailoringRunFailed]


async def test_a_guest_owned_runs_failure_touches_no_account_and_is_counted(
    clock: FixedClock,
) -> None:
    rig = Rig(clock.now())
    run = running_run(GuestOwner(GuestSessionId(value=a_user_id().value)), clock.now())
    await rig.runs.add(run)
    finds_before = rig.credits.find_calls

    await rig.record_failure(run, TailoringFailureReason.LLM_UNAVAILABLE, clock.now())

    assert run.status is TailoringRunStatus.FAILED
    assert rig.credits.find_calls == finds_before
    assert rig.credits.save_calls == 0
    assert rig.count(DemandCounter.RUNS_FAILED) == 1


async def test_one_run_is_refunded_once_even_though_a_second_failure_is_attempted(
    clock: FixedClock,
) -> None:
    rig = Rig(clock.now())
    run = await rig.run(running=True)
    await rig.record_failure(run, TailoringFailureReason.LLM_ERROR, clock.now())

    with pytest.raises(TailoringAlreadyDecided):
        await rig.record_failure(run, TailoringFailureReason.ABANDONED, clock.now())

    assert await rig.balance() == 1
    assert rig.count(DemandCounter.RUNS_FAILED) == 1  # not 2


async def test_a_run_already_succeeded_cannot_be_failed_and_nothing_is_written(
    clock: FixedClock,
) -> None:
    rig = Rig(clock.now())
    run = await rig.run(running=True)
    draft = a_draft()
    run.mark_succeeded(draft.documents, draft.metrics, clock.now())
    run.release_events()

    with pytest.raises(TailoringAlreadyDecided):
        await rig.record_failure(run, TailoringFailureReason.ABANDONED, clock.now())

    assert await rig.balance() == 0  # the charge is kept
    assert rig.credits.save_calls == 0
    assert rig.counters.calls == []
    assert rig.runs.save_calls == []


async def test_a_lost_save_race_propagates_and_publishes_nothing(clock: FixedClock) -> None:
    """F-14, the application half: the conflict reaches the caller and no event announces a failure
    that did not land. (That the refund is rolled back with it is the adapters' proof.)"""
    rig = Rig(clock.now(), conflict_on_save=1)
    run = await rig.run(running=True)

    with pytest.raises(TailoringRunConcurrentlyModified):
        await rig.record_failure(run, TailoringFailureReason.ABANDONED, clock.now())

    assert rig.events.published == []
    assert "events.publish" not in rig.journal


async def test_a_failing_counter_stops_the_failure_before_the_run_is_saved(
    clock: FixedClock,
) -> None:
    """P-3: the count is part of the fact's transaction, so its failure is the fact's failure."""
    rig = Rig(
        clock.now(), counters=RecordingDailyCounter(increment_raises=_DatabaseDown("database down"))
    )
    run = await rig.run(running=True)

    with pytest.raises(_DatabaseDown):
        await rig.record_failure(run, TailoringFailureReason.LLM_ERROR, clock.now())

    assert rig.runs.save_calls == []
    assert rig.events.published == []


async def test_a_failure_never_touches_the_runs_metrics(clock: FixedClock) -> None:
    """AC-11: a refund never touches a run's `LlmCallMetrics`; a failed run keeps them `None`."""
    rig = Rig(clock.now())
    run = await rig.run(running=True)

    await rig.record_failure(run, TailoringFailureReason.LLM_OUTPUT_INVALID, clock.now())

    assert run.metrics is None
    assert run.documents is None


async def test_the_counter_day_is_the_instants_utc_date_not_the_systems(clock: FixedClock) -> None:
    rig = Rig(_LATE)
    run = await rig.run(running=True)

    await rig.record_failure(run, TailoringFailureReason.LLM_ERROR, _LATE)

    assert rig.counters.calls == [(DemandCounter.RUNS_FAILED, _LATE.date())]


async def test_a_refund_needs_the_run_to_be_the_one_that_was_charged(clock: FixedClock) -> None:
    """Two runs, one charge: failing the uncharged one must not spend the charged one's refund."""
    rig = Rig(clock.now(), extra=1)
    charged = await rig.run(running=True)
    uncharged = queued_run(rig.owner, clock.now() - timedelta(minutes=1))
    await rig.runs.add(uncharged)

    await rig.record_failure(uncharged, TailoringFailureReason.NOT_QUEUED, clock.now())

    assert await rig.balance() == 1  # 2 credits - 1 charge, no refund
    assert charged.status is TailoringRunStatus.RUNNING
