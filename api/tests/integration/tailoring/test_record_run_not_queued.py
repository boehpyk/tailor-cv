"""Application tests for `RecordRunNotQueued` (slice 5.2, T14 RED, AC-9's `not_queued` site, F-10).

The handler's second transaction after the broker refused the enqueue: the run fails `not_queued`, its
credit comes back and `runs_failed` counts. The handler commits; if *that* fails the run stays
`queued` and charged, and the sweep's lost-queued list recovers it (F-11, in the sweep's tests).
"""

from __future__ import annotations

from uuid import uuid4

from tailorcraft.application.tailoring.record_run_not_queued import (
    RecordRunNotQueued,
    RecordRunNotQueuedCommand,
)
from tailorcraft.domain.shared.counters import DemandCounter
from tailorcraft.domain.tailoring.value_objects import (
    TailoringFailureReason,
    TailoringRunId,
    TailoringRunStatus,
)
from tailorcraft.infrastructure.clock import FixedClock
from tests.integration.tailoring.credits_support import Rig


def _use_case(rig: Rig, clock: FixedClock) -> RecordRunNotQueued:
    return RecordRunNotQueued(rig.runs, rig.record_failure, clock)


async def test_a_refused_enqueue_fails_the_run_not_queued_and_gives_the_credit_back(
    clock: FixedClock,
) -> None:
    rig = Rig(clock.now())
    run = await rig.run()
    assert await rig.balance() == 0

    await _use_case(rig, clock)(RecordRunNotQueuedCommand(tailoring_run_id=run.id))

    stored = await rig.runs.get(run.id)
    assert (stored.status, stored.failure_reason, stored.completed_at) == (
        TailoringRunStatus.FAILED,
        TailoringFailureReason.NOT_QUEUED,
        clock.now(),
    )
    assert await rig.balance() == 1
    assert rig.count(DemandCounter.RUNS_FAILED) == 1


async def test_it_goes_through_the_one_failure_path_in_its_order(clock: FixedClock) -> None:
    rig = Rig(clock.now())
    run = await rig.run()

    await _use_case(rig, clock)(RecordRunNotQueuedCommand(tailoring_run_id=run.id))

    assert rig.journal == [
        "credits.save",
        "count:runs_failed",
        "runs.save:failed",
        "events.publish",
    ]


async def test_an_uncharged_run_is_failed_with_no_refund(clock: FixedClock) -> None:
    rig = Rig(clock.now())
    run = await rig.run(charged=False)

    await _use_case(rig, clock)(RecordRunNotQueuedCommand(tailoring_run_id=run.id))

    assert (await rig.runs.get(run.id)).status is TailoringRunStatus.FAILED
    assert rig.credits.save_calls == 0
    assert await rig.balance() == 1


async def test_a_run_that_is_gone_is_nothing_to_record(clock: FixedClock) -> None:
    rig = Rig(clock.now())

    await _use_case(rig, clock)(
        RecordRunNotQueuedCommand(tailoring_run_id=TailoringRunId(value=uuid4()))
    )

    assert rig.journal == []
    assert rig.counters.calls == []
    assert rig.events.published == []
