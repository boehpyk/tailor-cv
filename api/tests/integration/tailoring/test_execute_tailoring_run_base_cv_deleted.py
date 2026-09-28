"""Application tests for `ExecuteTailoringRun`'s step 5 split (slice 2.3, T10 RED, AC-12, H-22).

**No skeleton — this is red against today's real code.** A user-owned run names a saved CV; the user
deletes that CV in another tab after the request and before the worker reaches step 5 (technical
plan §0.4(a): the reference dangles on purpose). Today step 5 catches `BaseCvNotFound` together with
`JobPostingNotFound` and returns `MISSING`, leaving the run `running` for the stale sweep to call
`abandoned` five minutes later — which lies about why. 2.3 records **`failed` / `base_cv_deleted`**
from `running`, **before** the paid call: the fake `LlmPort`'s call count is **0**, the outcome is
`FAILED`, and `TailoringRunFailed` is published.

"LLM called 0 times" is satisfied by today's code too — so it is never asserted alone: each test
pairs it with the recorded state (`FAILED`, the reason, the two saves), which today's code does not
produce. A missing *posting* stays `MISSING` (unreachable by design, §0.4): that test is green on
arrival and is kept as the guard that the split did not swallow the other branch.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

from tailorcraft.application.tailoring.execute_tailoring_run import (
    ExecuteTailoringRun,
    ExecuteTailoringRunCommand,
    ExecuteTailoringRunOutcome,
)
from tailorcraft.domain.identity.ownership import UserOwner
from tailorcraft.domain.identity.value_objects import UserId
from tailorcraft.domain.tailoring.events import TailoringRunFailed
from tailorcraft.domain.tailoring.value_objects import TailoringFailureReason, TailoringRunStatus
from tailorcraft.infrastructure.clock import FixedClock
from tests.integration.fakes import (
    FakeBaseCvRepository,
    FakeJobPostingRepository,
    FakeLlm,
    FakeTailoringRunRepository,
    RecordingEventPublisher,
)
from tests.integration.owners import a_draft, extracted_cv, pasted_posting, queued_run


class _World:
    def __init__(self, clock: FixedClock) -> None:
        self.clock = clock
        self.cvs = FakeBaseCvRepository()
        self.postings = FakeJobPostingRepository()
        self.runs = FakeTailoringRunRepository()
        self.llm = FakeLlm(a_draft())
        self.events = RecordingEventPublisher()
        self.user = UserOwner(UserId(value=uuid4()))

    def use_case(self) -> ExecuteTailoringRun:
        return ExecuteTailoringRun(
            self.runs, self.cvs, self.postings, self.llm, self.events, self.clock
        )


async def _requested_then_cv_deleted(w: _World) -> ExecuteTailoringRunCommand:
    """Request a run over a saved CV and a posting, then delete the saved CV — the order H-22 names."""
    earlier = w.clock.now() - timedelta(minutes=1)
    cv = extracted_cv(w.user, earlier)
    posting = pasted_posting(w.user, earlier)
    await w.cvs.add(cv)
    await w.postings.add(posting)
    run = queued_run(w.user, earlier, base_cv_id=cv.id, job_posting_id=posting.id)
    await w.runs.add(run)
    await w.cvs.remove(cv.id, w.user)  # the user deleted the saved CV in another tab
    return ExecuteTailoringRunCommand(tailoring_run_id=run.id)


async def test_a_run_whose_saved_cv_was_deleted_is_recorded_failed_base_cv_deleted(
    clock: FixedClock,
) -> None:
    w = _World(clock)
    cmd = await _requested_then_cv_deleted(w)

    outcome = await w.use_case()(cmd)

    assert outcome is ExecuteTailoringRunOutcome.FAILED
    run = await w.runs.get(cmd.tailoring_run_id)
    assert run.status is TailoringRunStatus.FAILED
    assert run.failure_reason is TailoringFailureReason.BASE_CV_DELETED
    assert run.completed_at == clock.now()
    assert w.llm.calls == []


async def test_base_cv_deleted_is_recorded_from_running_before_the_model_is_called(
    clock: FixedClock,
) -> None:
    """Step 4 saved `running`; step 5 saves `failed` — two saves, in that order, and no call in
    between (the paid call never happens)."""
    w = _World(clock)
    cmd = await _requested_then_cv_deleted(w)

    await w.use_case()(cmd)

    assert w.runs.save_calls == [TailoringRunStatus.RUNNING, TailoringRunStatus.FAILED]
    assert w.llm.calls == []


async def test_base_cv_deleted_publishes_tailoring_run_failed_with_the_reason(
    clock: FixedClock,
) -> None:
    w = _World(clock)
    cmd = await _requested_then_cv_deleted(w)

    await w.use_case()(cmd)

    failed = [e for e in w.events.published if isinstance(e, TailoringRunFailed)]
    assert [(e.tailoring_run_id, e.reason) for e in failed] == [
        (cmd.tailoring_run_id, TailoringFailureReason.BASE_CV_DELETED)
    ]


async def test_a_redelivery_after_base_cv_deleted_is_skipped(clock: FixedClock) -> None:
    """TR-3: the reason is recorded through `mark_failed`, so a second delivery finds a decided run
    and is refused — still no paid call."""
    w = _World(clock)
    cmd = await _requested_then_cv_deleted(w)
    await w.use_case()(cmd)

    second = await w.use_case()(cmd)

    assert second is ExecuteTailoringRunOutcome.SKIPPED
    assert (await w.runs.get(cmd.tailoring_run_id)).failure_reason is (
        TailoringFailureReason.BASE_CV_DELETED
    )
    assert w.llm.calls == []


async def test_a_missing_posting_still_returns_missing_without_a_paid_call(
    clock: FixedClock,
) -> None:
    """Unreachable by design (§0.4) and unchanged: green on arrival, the guard that the split kept
    the posting branch as it was."""
    w = _World(clock)
    earlier = clock.now() - timedelta(minutes=1)
    cv = extracted_cv(w.user, earlier)
    await w.cvs.add(cv)
    posting = pasted_posting(w.user, earlier)  # never added
    run = queued_run(w.user, earlier, base_cv_id=cv.id, job_posting_id=posting.id)
    await w.runs.add(run)

    outcome = await w.use_case()(ExecuteTailoringRunCommand(tailoring_run_id=run.id))

    assert outcome is ExecuteTailoringRunOutcome.MISSING
    assert w.llm.calls == []
