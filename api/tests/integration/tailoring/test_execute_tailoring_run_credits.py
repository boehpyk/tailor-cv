"""Application tests for `ExecuteTailoringRun`'s credits and counters (slice 5.2, T14 RED, AC-9, AC-10,
AC-11, AC-12's late delivery, F-3 .. F-9, F-13, F-19).

Every way the worker fails a run goes through `RecordRunFailure`, so each site refunds a charged run,
counts `runs_failed` and writes in the journal's order; a success keeps the charge, counts
`runs_succeeded` before its save and stores its metrics exactly as today. The fakes have no
transaction: "neither survives" is in `test_pending_adapters.py`.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from tailorcraft.application.tailoring.execute_tailoring_run import (
    ExecuteTailoringRun,
    ExecuteTailoringRunCommand,
    ExecuteTailoringRunOutcome,
)
from tailorcraft.domain.billing.value_objects import CreditEntryKind
from tailorcraft.domain.shared.counters import DemandCounter
from tailorcraft.domain.tailoring.errors import TailoringFailed
from tailorcraft.domain.tailoring.tailoring_run import TailoringRun
from tailorcraft.domain.tailoring.value_objects import TailoringFailureReason, TailoringRunStatus
from tailorcraft.infrastructure.clock import FixedClock
from tests.integration.fakes import FakeBaseCvRepository, FakeJobPostingRepository, FakeLlm
from tests.integration.owners import a_draft, extracted_cv, pasted_posting, uploaded_cv
from tests.integration.tailoring.credits_support import Rig

_LLM_REASONS = [
    TailoringFailureReason.LLM_UNAVAILABLE,
    TailoringFailureReason.LLM_RATE_LIMITED,
    TailoringFailureReason.LLM_REFUSED,
    TailoringFailureReason.LLM_TIMED_OUT,
    TailoringFailureReason.LLM_OUTPUT_INVALID,
    TailoringFailureReason.INPUTS_TOO_LARGE,
    TailoringFailureReason.LLM_ERROR,
]


class _Worker:
    """The rig's user's run, its CV and posting, and the use case wired to the rig."""

    def __init__(self, rig: Rig, clock: FixedClock, llm: FakeLlm, *, cv: str = "extracted") -> None:
        self.rig, self.clock, self.llm, self.cv_kind = rig, clock, llm, cv
        self.cvs = FakeBaseCvRepository()
        self.postings = FakeJobPostingRepository()

    async def prepare(self, *, charged: bool = True, running: bool = False) -> TailoringRun:
        """A queued run, or a `running` one whose worker started 4000 s ago (past the 300 s window)."""
        at = self.clock.now() - timedelta(seconds=4000 if running else 10)
        owner = self.rig.owner
        posting = pasted_posting(owner, at)
        await self.postings.add(posting)
        base_cv_id = None  # "absent": a run naming a CV that is not in the repository
        if self.cv_kind != "absent":
            cv = (extracted_cv if self.cv_kind == "extracted" else uploaded_cv)(owner, at)
            await self.cvs.add(cv)
            base_cv_id = cv.id
        return await self.rig.run(
            charged=charged,
            running=running,
            age=self.clock.now() - at,
            base_cv_id=base_cv_id,
            job_posting_id=posting.id,
        )

    def use_case(self) -> ExecuteTailoringRun:
        return ExecuteTailoringRun(
            self.rig.runs,
            self.cvs,
            self.postings,
            self.llm,
            self.rig.events,
            self.clock,
            record_failure=self.rig.record_failure,
            counters=self.rig.counters,
        )

    async def execute(self, run: TailoringRun) -> ExecuteTailoringRunOutcome:
        return await self.use_case()(ExecuteTailoringRunCommand(tailoring_run_id=run.id))


def _failure_is_recorded_in_order(journal: list[str]) -> bool:
    return (
        journal.index("credits.save")
        < journal.index("count:runs_failed")
        < journal.index("runs.save:failed")
    )


async def _assert_refunded_once(
    rig: Rig, run: TailoringRun, reason: TailoringFailureReason
) -> None:
    stored = await rig.runs.get(run.id)
    assert (stored.status, stored.failure_reason) == (TailoringRunStatus.FAILED, reason)
    assert await rig.balance() == 1
    account = await rig.credits.find(rig.owner.user_id)
    assert account is not None
    assert [e.kind for e in account.entries].count(CreditEntryKind.REFUND) == 1
    assert rig.count(DemandCounter.RUNS_FAILED) == 1
    assert rig.count(DemandCounter.RUNS_SUCCEEDED) == 0
    assert _failure_is_recorded_in_order(rig.journal)


async def test_a_success_keeps_the_charge_stores_its_metrics_and_counts_it(
    clock: FixedClock,
) -> None:
    """AC-11."""
    rig = Rig(clock.now())
    draft = a_draft()
    worker = _Worker(rig, clock, FakeLlm(draft))
    run = await worker.prepare()

    outcome = await worker.execute(run)

    assert outcome is ExecuteTailoringRunOutcome.SUCCEEDED
    stored = await rig.runs.get(run.id)
    assert stored.status is TailoringRunStatus.SUCCEEDED
    assert stored.metrics == draft.metrics
    assert stored.documents == draft.documents
    assert await rig.balance() == 0  # the charge stands
    account = await rig.credits.find(rig.owner.user_id)
    assert account is not None
    assert CreditEntryKind.REFUND not in [e.kind for e in account.entries]
    assert rig.count(DemandCounter.RUNS_SUCCEEDED) == 1
    assert rig.count(DemandCounter.RUNS_FAILED) == 0
    assert rig.counters.calls == [(DemandCounter.RUNS_SUCCEEDED, clock.now().date())]


async def test_the_success_is_counted_before_the_success_is_saved(clock: FixedClock) -> None:
    """The count shares the success save's commit, so it is written first."""
    rig = Rig(clock.now())
    worker = _Worker(rig, clock, FakeLlm(a_draft()))
    run = await worker.prepare()

    await worker.execute(run)

    assert "count:runs_succeeded" in rig.journal
    assert rig.journal.index("count:runs_succeeded") < rig.journal.index("runs.save:succeeded")


@pytest.mark.parametrize("reason", _LLM_REASONS)
async def test_every_llm_failure_refunds_the_charged_run_and_counts_it(
    reason: TailoringFailureReason, clock: FixedClock
) -> None:
    """F-3 .. F-6, F-8."""
    rig = Rig(clock.now())
    worker = _Worker(rig, clock, FakeLlm(TailoringFailed(reason)))
    run = await worker.prepare()

    outcome = await worker.execute(run)

    assert outcome is ExecuteTailoringRunOutcome.FAILED
    await _assert_refunded_once(rig, run, reason)


async def test_a_base_cv_deleted_before_the_worker_read_it_refunds_and_counts(
    clock: FixedClock,
) -> None:
    """F-9."""
    rig = Rig(clock.now())
    llm = FakeLlm(a_draft())
    worker = _Worker(rig, clock, llm, cv="absent")
    run = await worker.prepare()

    outcome = await worker.execute(run)

    assert outcome is ExecuteTailoringRunOutcome.FAILED
    assert llm.calls == []
    await _assert_refunded_once(rig, run, TailoringFailureReason.BASE_CV_DELETED)


async def test_a_cv_with_no_text_fails_as_llm_error_refunds_and_counts(clock: FixedClock) -> None:
    rig = Rig(clock.now())
    llm = FakeLlm(a_draft())
    worker = _Worker(rig, clock, llm, cv="uploaded")
    run = await worker.prepare()

    outcome = await worker.execute(run)

    assert outcome is ExecuteTailoringRunOutcome.FAILED
    assert llm.calls == []
    await _assert_refunded_once(rig, run, TailoringFailureReason.LLM_ERROR)


async def test_a_stale_redelivery_abandons_the_run_refunds_and_counts(clock: FixedClock) -> None:
    """F-13's redelivery half."""
    rig = Rig(clock.now())
    llm = FakeLlm(a_draft())
    worker = _Worker(rig, clock, llm)
    run = await worker.prepare(running=True)

    outcome = await worker.execute(run)

    assert outcome is ExecuteTailoringRunOutcome.ABANDONED
    assert llm.calls == []
    await _assert_refunded_once(rig, run, TailoringFailureReason.ABANDONED)


async def test_an_uncharged_runs_llm_failure_refunds_nothing(clock: FixedClock) -> None:
    """AC-10, F-19: made by the old API during the deploy window; success was free, so is failure."""
    rig = Rig(clock.now())
    worker = _Worker(rig, clock, FakeLlm(TailoringFailed(TailoringFailureReason.LLM_ERROR)))
    run = await worker.prepare(charged=False)

    await worker.execute(run)

    assert (await rig.runs.get(run.id)).status is TailoringRunStatus.FAILED
    assert await rig.balance() == 1
    assert rig.credits.save_calls == 0
    assert rig.count(DemandCounter.RUNS_FAILED) == 1


async def test_an_uncharged_runs_success_is_counted_and_charges_nothing(clock: FixedClock) -> None:
    rig = Rig(clock.now())
    worker = _Worker(rig, clock, FakeLlm(a_draft()))
    run = await worker.prepare(charged=False)

    await worker.execute(run)

    assert await rig.balance() == 1
    assert rig.credits.save_calls == 0
    assert rig.count(DemandCounter.RUNS_SUCCEEDED) == 1


async def test_a_run_already_failed_and_refunded_is_skipped_on_a_late_delivery(
    clock: FixedClock,
) -> None:
    """AC-12, F-13: the late task finds a decided run, calls no LLM and refunds nothing twice."""
    rig = Rig(clock.now())
    llm = FakeLlm(a_draft())
    worker = _Worker(rig, clock, llm)
    run = await worker.prepare()
    await rig.record_failure(run, TailoringFailureReason.NOT_QUEUED, clock.now())
    journal_after_recovery = list(rig.journal)

    outcome = await worker.execute(run)

    assert outcome is ExecuteTailoringRunOutcome.SKIPPED
    assert llm.calls == []
    assert await rig.balance() == 1  # refunded once, not twice
    assert rig.journal == journal_after_recovery
    assert rig.count(DemandCounter.RUNS_FAILED) == 1


async def test_losing_the_running_save_skips_without_counting_or_refunding(
    clock: FixedClock,
) -> None:
    rig = Rig(clock.now(), conflict_on_save=1)
    llm = FakeLlm(a_draft())
    worker = _Worker(rig, clock, llm)
    run = await worker.prepare()

    outcome = await worker.execute(run)

    assert outcome is ExecuteTailoringRunOutcome.SKIPPED
    assert llm.calls == []
    assert rig.counters.calls == []
    assert rig.credits.save_calls == 0
