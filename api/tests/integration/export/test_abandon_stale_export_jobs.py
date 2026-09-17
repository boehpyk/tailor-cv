"""Application tests for `AbandonStaleExportJobs` (T6, RED).

The beat sweep that delivers X-29: an `ExportJob` that claims to be `RENDERING` but whose worker was
lost — a pool child killed, the main process SIGKILLed, a message lost — is recorded
`failed`/`abandoned` on a timer, because redelivery alone does not bring it back (the export mirror
of 1.3's G-25').

**Why fakes, not a real Postgres:** the same reason every file in this package gives. This use case
is tested against the one port it depends on, `ExportJobRepository`, via `FakeExportJobRepository`,
plus `RecordingEventPublisher` and `FixedClock` — mirroring `test_abandon_stale_tailoring_runs.py`'s
own module docstring almost exactly, because this is that file's use case with the aggregate
swapped (technical-plan.md's "Application layer" §7 says so explicitly and refuses to generalize the
two).

Every assertion below states what `AbandonStaleExportJobs.__call__` **should** do per that section's
"Flow" and feature-spec.md's X-29 and X-39, never what the (currently `NotImplementedError`) code was
observed doing. Because the skeleton's `__call__` body is an unconditional `raise
NotImplementedError`, every test below is expected to fail on that line: a real red, not a vacuous
pass, and not an `ImportError`.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from uuid import uuid4

from tailorcraft.application.export.abandon_stale_export_jobs import (
    AbandonStaleExportJobs,
    AbandonStaleExportJobsResult,
)
from tailorcraft.domain.export.errors import ExportJobConcurrentlyModified
from tailorcraft.domain.export.events import ExportFailed
from tailorcraft.domain.export.export_job import ExportJob
from tailorcraft.domain.export.value_objects import (
    ExportFailureReason,
    ExportFormat,
    ExportJobId,
    ExportJobStatus,
)
from tailorcraft.domain.identity.value_objects import GuestSessionId
from tailorcraft.domain.shared.events import DomainEvent
from tailorcraft.domain.tailoring.value_objects import TailoredDocumentKind, TailoringRunId
from tailorcraft.infrastructure.clock import FixedClock
from tests.integration.fakes import FakeExportJobRepository, RecordingEventPublisher


def _a_session_id() -> GuestSessionId:
    return GuestSessionId(value=uuid4())


def _a_run_id() -> TailoringRunId:
    return TailoringRunId(value=uuid4())


def _rendering_job(*, requested_at: datetime, started_at: datetime) -> ExportJob:
    job = ExportJob.request(
        id=ExportJobId(value=uuid4()),
        guest_session_id=_a_session_id(),
        tailoring_run_id=_a_run_id(),
        document=TailoredDocumentKind.CV,
        format=ExportFormat.PDF,
        run_version=1,
        requested_at=requested_at,
    )
    job.mark_started(started_at)
    return job


def _use_case(
    jobs: FakeExportJobRepository,
    events: RecordingEventPublisher,
    clock: FixedClock,
    *,
    stale_after_seconds: int = 300,
    batch_limit: int = 100,
) -> AbandonStaleExportJobs:
    return AbandonStaleExportJobs(
        jobs, events, clock, stale_after_seconds=stale_after_seconds, batch_limit=batch_limit
    )


def _failed_events_by_job(
    events: RecordingEventPublisher,
) -> dict[ExportJobId, ExportFailed]:
    return {e.export_job_id: e for e in events.published if isinstance(e, ExportFailed)}


class _ConflictingSaveRepository(FakeExportJobRepository):
    """A `FakeExportJobRepository` whose `save` raises `ExportJobConcurrentlyModified` for one
    specific job id and behaves normally for every other — mirroring
    `test_abandon_stale_tailoring_runs.py`'s `_ConflictingSaveRepository` exactly, for the same
    reason: a conflict on the **middle** of several stale jobs in listing order, sandwiched between
    two ordinary saves, is a shape the plain `conflict_on_save` counter cannot produce because it
    fires on whichever `save` call comes next regardless of which job it is for."""

    def __init__(self, conflicting_job_id: ExportJobId) -> None:
        super().__init__()
        self._conflicting_job_id = conflicting_job_id

    async def save(self, job: ExportJob) -> None:
        if job.id == self._conflicting_job_id:
            raise ExportJobConcurrentlyModified(job.id)
        await super().save(job)


# --- 1. A stale job is recorded failed/abandoned; the event carries the job id ------------------


async def test_a_stale_rendering_job_is_recorded_failed_abandoned(clock: FixedClock) -> None:
    jobs = FakeExportJobRepository()
    job = _rendering_job(
        requested_at=clock.now() - timedelta(seconds=310),
        started_at=clock.now() - timedelta(seconds=301),
    )
    await jobs.add(job)
    events = RecordingEventPublisher()
    use_case = _use_case(jobs, events, clock)

    result = await use_case()

    assert result == AbandonStaleExportJobsResult(swept=1, conflicts=0, examined=1)
    stored = await jobs.get(job.id)
    assert stored.status is ExportJobStatus.FAILED
    assert stored.failure_reason is ExportFailureReason.ABANDONED
    assert stored.completed_at == clock.now()

    failed = [e for e in events.published if isinstance(e, ExportFailed)]
    assert len(failed) == 1
    assert failed[0].export_job_id == job.id
    assert failed[0].reason is ExportFailureReason.ABANDONED


# --- 2. A fresh rendering job is untouched while a stale one in the same batch is abandoned ------


async def test_a_fresh_rendering_job_is_untouched_while_a_stale_one_is_abandoned(
    clock: FixedClock,
) -> None:
    fresh = _rendering_job(
        requested_at=clock.now() - timedelta(seconds=10),
        started_at=clock.now() - timedelta(seconds=5),
    )
    stale = _rendering_job(
        requested_at=clock.now() - timedelta(seconds=400),
        started_at=clock.now() - timedelta(seconds=301),
    )
    jobs = FakeExportJobRepository()
    await jobs.add(fresh)
    await jobs.add(stale)
    events = RecordingEventPublisher()
    use_case = _use_case(jobs, events, clock)

    result = await use_case()

    assert result == AbandonStaleExportJobsResult(swept=1, conflicts=0, examined=1)
    unchanged = await jobs.get(fresh.id)
    assert unchanged.status is ExportJobStatus.RENDERING
    assert unchanged.failure_reason is None
    failed_by_job = _failed_events_by_job(events)
    assert fresh.id not in failed_by_job

    processed = await jobs.get(stale.id)
    assert processed.status is ExportJobStatus.FAILED
    assert processed.failure_reason is ExportFailureReason.ABANDONED
    assert stale.id in failed_by_job


# --- 3. Batch bound: at most `batch_limit` jobs handled per tick ---------------------------------


async def test_batch_is_bounded_by_the_limit_and_examined_reports_it(clock: FixedClock) -> None:
    jobs = FakeExportJobRepository()
    for _ in range(5):
        stale = _rendering_job(
            requested_at=clock.now() - timedelta(seconds=400),
            started_at=clock.now() - timedelta(seconds=301),
        )
        await jobs.add(stale)
    events = RecordingEventPublisher()
    use_case = _use_case(jobs, events, clock, batch_limit=3)

    result = await use_case()

    assert result.examined == 3
    assert result.swept == 3
    assert result.conflicts == 0
    remaining_rendering = [j for j in jobs.all() if j.status is ExportJobStatus.RENDERING]
    assert len(remaining_rendering) == 2


# --- 4. An empty tick is an ordinary, zero-count answer ------------------------------------------


async def test_a_tick_with_nothing_stale_returns_all_zero_counts(clock: FixedClock) -> None:
    jobs = FakeExportJobRepository()
    events = RecordingEventPublisher()
    use_case = _use_case(jobs, events, clock)

    result = await use_case()

    assert result == AbandonStaleExportJobsResult(swept=0, conflicts=0, examined=0)
    assert events.published == []


# --- 5. A conflict mid-batch is counted and the batch continues (X-39) --------------------------


async def test_a_conflict_on_the_middle_job_is_counted_and_the_batch_continues(
    clock: FixedClock,
) -> None:
    first = _rendering_job(
        requested_at=clock.now() - timedelta(seconds=400),
        started_at=clock.now() - timedelta(seconds=310),
    )
    contested = _rendering_job(
        requested_at=clock.now() - timedelta(seconds=400),
        started_at=clock.now() - timedelta(seconds=305),
    )
    last = _rendering_job(
        requested_at=clock.now() - timedelta(seconds=400),
        started_at=clock.now() - timedelta(seconds=301),
    )
    jobs = _ConflictingSaveRepository(contested.id)
    await jobs.add(first)
    await jobs.add(contested)
    await jobs.add(last)
    events = RecordingEventPublisher()
    use_case = _use_case(jobs, events, clock)

    result = await use_case()

    assert result == AbandonStaleExportJobsResult(swept=2, conflicts=1, examined=3)
    failed_by_job = _failed_events_by_job(events)
    assert first.id in failed_by_job
    assert last.id in failed_by_job
    assert contested.id not in failed_by_job

    still_rendering = await jobs.get(contested.id)
    assert still_rendering.status is ExportJobStatus.RENDERING


# --- 6. Save strictly before publish -------------------------------------------------------------


async def test_the_save_lands_before_the_event_is_published(clock: FixedClock) -> None:
    jobs = FakeExportJobRepository()
    job = _rendering_job(
        requested_at=clock.now() - timedelta(seconds=310),
        started_at=clock.now() - timedelta(seconds=301),
    )
    await jobs.add(job)

    class _SnapshotSavedOnFirstPublish(RecordingEventPublisher):
        def __init__(self, saved: Sequence[ExportJob]) -> None:
            super().__init__()
            self._saved = saved
            self.saved_count_at_first_publish: int | None = None

        async def publish(self, *events: DomainEvent) -> None:
            if self.saved_count_at_first_publish is None:
                self.saved_count_at_first_publish = len(self._saved)
            await super().publish(*events)

    events = _SnapshotSavedOnFirstPublish(jobs.saved)
    use_case = _use_case(jobs, events, clock)

    await use_case()

    assert events.saved_count_at_first_publish == 1
