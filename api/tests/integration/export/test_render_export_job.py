"""Application tests for `RenderExportJob` (T6, RED).

**Why fakes, not a real Postgres:** the same reason `test_request_export.py`'s module docstring
gives. This use case is tested against the ports it actually depends on:
`ExportJobRepository` (`FakeExportJobRepository`), `TailoringRunRepository`
(`FakeTailoringRunRepository`, used directly — no `GuestSessionRepository` at all, per
`RenderExportJob`'s own docstring on why re-authorizing here would be actively wrong),
`DocumentRendererPort` (`FakeDocumentRenderer`) and `FileStorePort` (`InMemoryFileStore` /
`AlwaysFailingFileStore`).

**Why this file matters more than most.** `RenderExportJob` catches `DocumentRenderFailed` and
records it as a state of the aggregate (ADR-0014 §2) — the deliberate *opposite* of
`RenderDocumentInline`, which lets the identical exception family propagate. A reader arriving from
that module may "fix" this one to match. Every failure-reason assertion below therefore checks the
**recording** — the outcome `__call__` returns, the job's `status` and `failure_reason` — rather than
merely wrapping the call in "did not raise" (AC-19).

Every assertion below states what `RenderExportJob.__call__` **should** do per technical-plan.md's
"Application layer" §2 ("Flow") and feature-spec.md's failure contract rows X-25 … X-35, X-37, never
what the (currently `NotImplementedError`) code was observed doing. Because the skeleton's `__call__`
body is an unconditional `raise NotImplementedError`, every test below is expected to fail on that
line: a real red, not a vacuous pass, and not an `ImportError`.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest

from tailorcraft.application.export.render_export_job import (
    RenderExportJob,
    RenderExportJobCommand,
    RenderExportJobOutcome,
)
from tailorcraft.domain.export.errors import (
    DocumentRenderError,
    DocumentRenderFailed,
    DocumentRenderFailedOnDocument,
    DocumentRenderOutputTooLarge,
    DocumentRenderTimedOut,
    ExportJobConcurrentlyModified,
)
from tailorcraft.domain.export.events import ExportFailed, ExportReady, ExportStarted
from tailorcraft.domain.export.export_job import ExportJob
from tailorcraft.domain.export.value_objects import (
    ExportFailureReason,
    ExportFormat,
    ExportJobId,
    ExportJobStatus,
)
from tailorcraft.domain.identity.value_objects import GuestSessionId
from tailorcraft.domain.tailoring.tailoring_run import TailoringRun
from tailorcraft.domain.tailoring.value_objects import TailoredCv, TailoredDocumentKind
from tailorcraft.infrastructure.clock import FixedClock
from tests.integration.export.support import a_documents, succeeded_run
from tests.integration.fakes import (
    AlwaysFailingFileStore,
    FakeDocumentRenderer,
    FakeExportJobRepository,
    FakeTailoringRunRepository,
    InMemoryFileStore,
    RecordingEventPublisher,
)


def _a_session_id() -> GuestSessionId:
    return GuestSessionId(value=uuid4())


def _use_case(
    jobs: FakeExportJobRepository,
    runs: FakeTailoringRunRepository,
    renderer: FakeDocumentRenderer,
    files: InMemoryFileStore | AlwaysFailingFileStore,
    events: RecordingEventPublisher,
    clock: FixedClock,
    *,
    stale_after_seconds: int = 300,
) -> RenderExportJob:
    return RenderExportJob(
        jobs, runs, renderer, files, events, clock, stale_after_seconds=stale_after_seconds
    )


def _a_queued_job(
    *, run: TailoringRun, document: TailoredDocumentKind, format: ExportFormat
) -> ExportJob:
    assert run.completed_at is not None
    return ExportJob.request(
        id=ExportJobId(value=uuid4()),
        guest_session_id=run.guest_session_id,
        tailoring_run_id=run.id,
        document=document,
        format=format,
        run_version=run.version,
        requested_at=run.completed_at,
    )


def _running_run_with_same_id(succeeded: TailoringRun) -> TailoringRun:
    """A `RUNNING` `TailoringRun` sharing `succeeded`'s id, built the only legal way — never by
    mutating the succeeded instance — for the `SOURCE_UNAVAILABLE` guard test below, which needs a
    run repository lookup to answer with a run that is not `succeeded` while a job still names it.
    """
    run = TailoringRun.request(
        id=succeeded.id,
        guest_session_id=succeeded.guest_session_id,
        base_cv_id=succeeded.base_cv_id,
        job_posting_id=succeeded.job_posting_id,
        requested_at=succeeded.requested_at,
    )
    run.mark_started(succeeded.requested_at)
    return run


# --- READY ----------------------------------------------------------------------------------


async def test_ready_render_stores_the_file_under_storage_ref_and_records_byte_size(
    clock: FixedClock,
) -> None:
    session_id = _a_session_id()
    run = succeeded_run(session_id=session_id)
    runs = FakeTailoringRunRepository()
    await runs.add(run)
    job = _a_queued_job(run=run, document=TailoredDocumentKind.CV, format=ExportFormat.PDF)
    jobs = FakeExportJobRepository()
    await jobs.add(job)
    data = b"%PDF-1.7 fake bytes"
    renderer = FakeDocumentRenderer(data)
    files = InMemoryFileStore()
    events = RecordingEventPublisher()
    use_case = _use_case(jobs, runs, renderer, files, events, clock)

    outcome = await use_case(RenderExportJobCommand(export_job_id=job.id))

    assert outcome is RenderExportJobOutcome.READY
    stored = await jobs.get(job.id)
    assert stored.status is ExportJobStatus.READY
    assert stored.file_key == stored.storage_ref
    assert stored.byte_size == len(data)
    assert stored.render_duration_ms is not None
    assert stored.render_duration_ms >= 0
    assert files.data[stored.storage_ref.key] == data

    assert renderer.calls == [(a_documents().cv.value, TailoredDocumentKind.CV, ExportFormat.PDF)]

    started = [e for e in events.published if isinstance(e, ExportStarted)]
    ready = [e for e in events.published if isinstance(e, ExportReady)]
    assert len(started) == 1
    assert len(ready) == 1
    assert ready[0].byte_size == len(data)


async def test_ready_render_of_the_cover_letter_reads_the_cover_letter_source(
    clock: FixedClock,
) -> None:
    session_id = _a_session_id()
    run = succeeded_run(session_id=session_id)
    runs = FakeTailoringRunRepository()
    await runs.add(run)
    job = _a_queued_job(
        run=run, document=TailoredDocumentKind.COVER_LETTER, format=ExportFormat.DOCX
    )
    jobs = FakeExportJobRepository()
    await jobs.add(job)
    renderer = FakeDocumentRenderer(b"docx bytes")
    files = InMemoryFileStore()
    events = RecordingEventPublisher()
    use_case = _use_case(jobs, runs, renderer, files, events, clock)

    outcome = await use_case(RenderExportJobCommand(export_job_id=job.id))

    assert outcome is RenderExportJobOutcome.READY
    assert renderer.calls == [
        (a_documents().cover_letter.value, TailoredDocumentKind.COVER_LETTER, ExportFormat.DOCX)
    ]


# --- AC-19: one test per failure reason the adapters can produce -----------------------------


@pytest.mark.parametrize(
    ("exc", "expected_reason"),
    [
        (DocumentRenderFailedOnDocument(), ExportFailureReason.RENDER_FAILED),
        (DocumentRenderTimedOut(), ExportFailureReason.RENDER_TIMED_OUT),
        (DocumentRenderOutputTooLarge(), ExportFailureReason.OUTPUT_TOO_LARGE),
        (DocumentRenderError(), ExportFailureReason.RENDER_ERROR),
    ],
)
async def test_each_renderer_failure_is_recorded_as_failed_with_its_own_reason(
    clock: FixedClock, exc: DocumentRenderFailed, expected_reason: ExportFailureReason
) -> None:
    session_id = _a_session_id()
    run = succeeded_run(session_id=session_id)
    runs = FakeTailoringRunRepository()
    await runs.add(run)
    job = _a_queued_job(run=run, document=TailoredDocumentKind.CV, format=ExportFormat.PDF)
    jobs = FakeExportJobRepository()
    await jobs.add(job)
    renderer = FakeDocumentRenderer(exc)
    files = InMemoryFileStore()
    events = RecordingEventPublisher()
    use_case = _use_case(jobs, runs, renderer, files, events, clock)

    outcome = await use_case(RenderExportJobCommand(export_job_id=job.id))

    assert outcome is RenderExportJobOutcome.FAILED
    stored = await jobs.get(job.id)
    assert stored.status is ExportJobStatus.FAILED
    assert stored.failure_reason is expected_reason
    assert files.data == {}

    failed = [e for e in events.published if isinstance(e, ExportFailed)]
    assert len(failed) == 1
    assert failed[0].reason is expected_reason


async def test_file_store_failure_is_recorded_failed_file_store_unavailable(
    clock: FixedClock,
) -> None:
    session_id = _a_session_id()
    run = succeeded_run(session_id=session_id)
    runs = FakeTailoringRunRepository()
    await runs.add(run)
    job = _a_queued_job(run=run, document=TailoredDocumentKind.CV, format=ExportFormat.PDF)
    jobs = FakeExportJobRepository()
    await jobs.add(job)
    renderer = FakeDocumentRenderer(b"bytes that never get stored")
    files = AlwaysFailingFileStore()
    events = RecordingEventPublisher()
    use_case = _use_case(jobs, runs, renderer, files, events, clock)

    outcome = await use_case(RenderExportJobCommand(export_job_id=job.id))

    assert outcome is RenderExportJobOutcome.FAILED
    stored = await jobs.get(job.id)
    assert stored.status is ExportJobStatus.FAILED
    assert stored.failure_reason is ExportFailureReason.FILE_STORE_UNAVAILABLE

    failed = [e for e in events.published if isinstance(e, ExportFailed)]
    assert len(failed) == 1
    assert failed[0].reason is ExportFailureReason.FILE_STORE_UNAVAILABLE


# --- X-33: MISSING — the job (or its row) is simply gone --------------------------------------


async def test_unknown_job_id_returns_missing_without_raising(clock: FixedClock) -> None:
    jobs = FakeExportJobRepository()
    runs = FakeTailoringRunRepository()
    renderer = FakeDocumentRenderer(b"unused")
    files = InMemoryFileStore()
    events = RecordingEventPublisher()
    use_case = _use_case(jobs, runs, renderer, files, events, clock)

    outcome = await use_case(RenderExportJobCommand(export_job_id=ExportJobId(value=uuid4())))

    assert outcome is RenderExportJobOutcome.MISSING
    assert renderer.calls == []
    assert events.published == []


# --- X-34: SKIPPED — already decided, or still fresh in RENDERING -----------------------------


@pytest.mark.parametrize("failure", [False, True])
async def test_already_decided_job_is_skipped_with_no_render(
    clock: FixedClock, failure: bool
) -> None:
    session_id = _a_session_id()
    run = succeeded_run(session_id=session_id)
    runs = FakeTailoringRunRepository()
    await runs.add(run)
    job = _a_queued_job(run=run, document=TailoredDocumentKind.CV, format=ExportFormat.PDF)
    job.mark_started(clock.now())
    if failure:
        job.mark_failed(ExportFailureReason.RENDER_ERROR, clock.now())
        expected_status = ExportJobStatus.FAILED
    else:
        job.mark_ready(byte_size=1, render_duration_ms=1, at=clock.now())
        expected_status = ExportJobStatus.READY
    jobs = FakeExportJobRepository()
    await jobs.add(job)
    renderer = FakeDocumentRenderer(b"never rendered")
    files = InMemoryFileStore()
    events = RecordingEventPublisher()
    use_case = _use_case(jobs, runs, renderer, files, events, clock)

    outcome = await use_case(RenderExportJobCommand(export_job_id=job.id))

    assert outcome is RenderExportJobOutcome.SKIPPED
    assert renderer.calls == []
    stored = await jobs.get(job.id)
    assert stored.status is expected_status


async def test_fresh_rendering_job_is_skipped_with_no_render(clock: FixedClock) -> None:
    session_id = _a_session_id()
    run = succeeded_run(session_id=session_id)
    runs = FakeTailoringRunRepository()
    await runs.add(run)
    job = _a_queued_job(run=run, document=TailoredDocumentKind.CV, format=ExportFormat.PDF)
    job.mark_started(clock.now() - timedelta(seconds=10))
    jobs = FakeExportJobRepository()
    await jobs.add(job)
    renderer = FakeDocumentRenderer(b"never rendered")
    files = InMemoryFileStore()
    events = RecordingEventPublisher()
    use_case = _use_case(jobs, runs, renderer, files, events, clock, stale_after_seconds=300)

    outcome = await use_case(RenderExportJobCommand(export_job_id=job.id))

    assert outcome is RenderExportJobOutcome.SKIPPED
    assert renderer.calls == []
    stored = await jobs.get(job.id)
    assert stored.status is ExportJobStatus.RENDERING


# --- X-29: ABANDONED — RENDERING past the stale window -----------------------------------------


async def test_rendering_job_past_the_stale_window_is_recorded_abandoned(
    clock: FixedClock,
) -> None:
    session_id = _a_session_id()
    # The run completes 400 s ago rather than at `succeeded_run`'s default of 300 s, because
    # `_a_queued_job` requests the job at `run.completed_at` and this test then starts it 301 s ago:
    # with the default the job would be *started* one second before it was *requested*, which XJ-5
    # forbids and the aggregate refuses. The 300 s window this test is about is measured from
    # `started_at`, so moving the run back leaves the thing under test untouched.
    run = succeeded_run(session_id=session_id, completed_at=clock.now() - timedelta(seconds=400))
    runs = FakeTailoringRunRepository()
    await runs.add(run)
    job = _a_queued_job(run=run, document=TailoredDocumentKind.CV, format=ExportFormat.PDF)
    job.mark_started(clock.now() - timedelta(seconds=301))
    jobs = FakeExportJobRepository()
    await jobs.add(job)
    renderer = FakeDocumentRenderer(b"never rendered")
    files = InMemoryFileStore()
    events = RecordingEventPublisher()
    use_case = _use_case(jobs, runs, renderer, files, events, clock, stale_after_seconds=300)

    outcome = await use_case(RenderExportJobCommand(export_job_id=job.id))

    assert outcome is RenderExportJobOutcome.ABANDONED
    assert renderer.calls == []
    stored = await jobs.get(job.id)
    assert stored.status is ExportJobStatus.FAILED
    assert stored.failure_reason is ExportFailureReason.ABANDONED

    failed = [e for e in events.published if isinstance(e, ExportFailed)]
    assert len(failed) == 1
    assert failed[0].reason is ExportFailureReason.ABANDONED


# --- X-35: the run is gone, or is no longer succeeded ------------------------------------------


async def test_run_gone_at_render_time_returns_missing(clock: FixedClock) -> None:
    session_id = _a_session_id()
    run = succeeded_run(session_id=session_id)
    job = _a_queued_job(run=run, document=TailoredDocumentKind.CV, format=ExportFormat.PDF)
    jobs = FakeExportJobRepository()
    await jobs.add(job)
    runs = FakeTailoringRunRepository()  # the run itself was never added: purged away
    renderer = FakeDocumentRenderer(b"never rendered")
    files = InMemoryFileStore()
    events = RecordingEventPublisher()
    use_case = _use_case(jobs, runs, renderer, files, events, clock)

    outcome = await use_case(RenderExportJobCommand(export_job_id=job.id))

    assert outcome is RenderExportJobOutcome.MISSING
    assert renderer.calls == []
    # The job row is left untouched — there is nothing to record a failure on.
    stored = await jobs.get(job.id)
    assert stored.status is ExportJobStatus.RENDERING


async def test_run_no_longer_succeeded_records_source_unavailable(clock: FixedClock) -> None:
    session_id = _a_session_id()
    run = succeeded_run(session_id=session_id)
    job = _a_queued_job(run=run, document=TailoredDocumentKind.CV, format=ExportFormat.PDF)
    jobs = FakeExportJobRepository()
    await jobs.add(job)
    # Force an impossible-by-state situation: the run this job named is no longer `succeeded`.
    # `RenderExportJob` has no way to reach this through the ordinary lifecycle (a run is
    # `succeeded` when a job is created and terminal thereafter), so this is a guard, not a path.
    unsucceeded_run = _running_run_with_same_id(run)
    runs = FakeTailoringRunRepository()
    await runs.add(unsucceeded_run)
    renderer = FakeDocumentRenderer(b"never rendered")
    files = InMemoryFileStore()
    events = RecordingEventPublisher()
    use_case = _use_case(jobs, runs, renderer, files, events, clock)

    outcome = await use_case(RenderExportJobCommand(export_job_id=job.id))

    assert outcome is RenderExportJobOutcome.FAILED
    assert renderer.calls == []
    stored = await jobs.get(job.id)
    assert stored.status is ExportJobStatus.FAILED
    assert stored.failure_reason is ExportFailureReason.SOURCE_UNAVAILABLE


async def test_run_version_moved_on_records_source_changed_before_any_render_call(
    clock: FixedClock,
) -> None:
    session_id = _a_session_id()
    run = succeeded_run(session_id=session_id)
    runs = FakeTailoringRunRepository()
    await runs.add(run)
    job = _a_queued_job(run=run, document=TailoredDocumentKind.CV, format=ExportFormat.PDF)
    jobs = FakeExportJobRepository()
    await jobs.add(job)
    # The user edited the document after the job was requested: the run's version moves on.
    run.revise_cv(TailoredCv("z" * 450), expected_version=run.version, at=clock.now())
    renderer = FakeDocumentRenderer(b"must never be reached")
    files = InMemoryFileStore()
    events = RecordingEventPublisher()
    use_case = _use_case(jobs, runs, renderer, files, events, clock)

    outcome = await use_case(RenderExportJobCommand(export_job_id=job.id))

    assert outcome is RenderExportJobOutcome.FAILED
    assert renderer.calls == []  # no worker time spent on a document nobody asked for
    stored = await jobs.get(job.id)
    assert stored.status is ExportJobStatus.FAILED
    assert stored.failure_reason is ExportFailureReason.SOURCE_CHANGED

    failed = [e for e in events.published if isinstance(e, ExportFailed)]
    assert len(failed) == 1
    assert failed[0].reason is ExportFailureReason.SOURCE_CHANGED


# --- AC-6 / X-30: two deliveries in flight at once — the loser is skipped before rendering -----


async def test_two_concurrent_deliveries_the_loser_is_skipped_with_one_render_call(
    clock: FixedClock,
) -> None:
    """The mirror of 1.3's AC-8: two `RenderExportJob` invocations racing on **the same job id**,
    each backed by its own repository instance holding its own independently-loaded `ExportJob`
    object, so neither `mark_started` call raises `ExportAlreadyStarted` in-process — only the
    repository's own version check (ADR-0015 §3, `ExportJobConcurrentlyModified`) can tell them
    apart, exactly as `TailoringRunConcurrentlyModified` does for `ExecuteTailoringRun`."""
    session_id = _a_session_id()
    run = succeeded_run(session_id=session_id)
    runs = FakeTailoringRunRepository()
    await runs.add(run)

    job_id = ExportJobId(value=uuid4())
    assert run.completed_at is not None

    def _independently_loaded_copy() -> ExportJob:
        assert run.completed_at is not None
        return ExportJob.request(
            id=job_id,
            guest_session_id=session_id,
            tailoring_run_id=run.id,
            document=TailoredDocumentKind.CV,
            format=ExportFormat.PDF,
            run_version=run.version,
            requested_at=run.completed_at,
        )

    winner_jobs = FakeExportJobRepository()
    await winner_jobs.add(_independently_loaded_copy())
    # The loser's very own `save` — its first and only one in this test — is the one that must
    # lose the race, so `conflict_on_save=1` fires exactly there.
    loser_jobs = FakeExportJobRepository(conflict_on_save=1)
    await loser_jobs.add(_independently_loaded_copy())

    renderer = FakeDocumentRenderer(b"rendered once")
    files = InMemoryFileStore()
    winner_events = RecordingEventPublisher()
    loser_events = RecordingEventPublisher()
    winner_use_case = _use_case(winner_jobs, runs, renderer, files, winner_events, clock)
    loser_use_case = _use_case(loser_jobs, runs, renderer, files, loser_events, clock)
    cmd = RenderExportJobCommand(export_job_id=job_id)

    winner_outcome = await winner_use_case(cmd)
    assert winner_outcome is RenderExportJobOutcome.READY
    assert len(renderer.calls) == 1

    loser_outcome = await loser_use_case(cmd)

    assert loser_outcome is RenderExportJobOutcome.SKIPPED
    assert len(renderer.calls) == 1  # unchanged: the loser never reached the renderer
    assert loser_events.published == []  # nothing published on the losing path


# --- File before row (ADR-0006 §2) -------------------------------------------------------------


class _FailOnSecondSaveRepository(FakeExportJobRepository):
    """A `FakeExportJobRepository` whose **second** `save` call raises
    `ExportJobConcurrentlyModified` and whose first succeeds normally — unlike
    `conflict_on_save`, which fires on whichever call comes *next* and so cannot let step 4's
    save (recording `RENDERING`) land while still failing step 9's (recording the outcome). That
    distinction is exactly what this test needs: step 8 must have already run — and therefore
    already written the file — by the time step 9's save is reached and refused.
    """

    def __init__(self) -> None:
        super().__init__()
        self._save_calls = 0

    async def save(self, job: ExportJob) -> None:
        self._save_calls += 1
        if self._save_calls == 2:
            raise ExportJobConcurrentlyModified(job.id)
        await super().save(job)


async def test_a_failing_mark_ready_save_still_leaves_the_file_on_the_store(
    clock: FixedClock,
) -> None:
    """Step 8 writes the bytes before step 9 records `mark_ready` (technical-plan.md's "Step 8 —
    file before row"). A repository whose *second* `save` call — the one recording the outcome —
    fails must therefore still find the bytes sitting on the store: the crash window's survivor is
    an orphan file 1.6 can sweep, never a `ready` row pointing at nothing.
    """
    session_id = _a_session_id()
    run = succeeded_run(session_id=session_id)
    runs = FakeTailoringRunRepository()
    await runs.add(run)
    job = _a_queued_job(run=run, document=TailoredDocumentKind.CV, format=ExportFormat.PDF)
    jobs = _FailOnSecondSaveRepository()
    await jobs.add(job)
    data = b"bytes that must survive a failed outcome save"
    renderer = FakeDocumentRenderer(data)
    files = InMemoryFileStore()
    events = RecordingEventPublisher()
    use_case = _use_case(jobs, runs, renderer, files, events, clock)

    with pytest.raises(ExportJobConcurrentlyModified):
        await use_case(RenderExportJobCommand(export_job_id=job.id))

    assert files.data[job.storage_ref.key] == data


# --- `rendering` is visible to the fake before the render happens ------------------------------


async def test_mark_started_is_committed_before_the_renderer_is_ever_called(
    clock: FixedClock,
) -> None:
    """Step 4's second commit (technical-plan.md): the save recording `RENDERING` must land before
    step 7 calls the renderer, so a polling client sees `rendering` for the duration of a slow
    render rather than jumping straight from `queued` to a terminal status. The renderer here reads
    the job straight back out of the repository the instant it is invoked, which is what turns "was
    it saved by then?" into a plain equality assertion rather than a guess based on the end state.
    """
    session_id = _a_session_id()
    run = succeeded_run(session_id=session_id)
    runs = FakeTailoringRunRepository()
    await runs.add(run)
    job = _a_queued_job(run=run, document=TailoredDocumentKind.CV, format=ExportFormat.PDF)
    jobs = FakeExportJobRepository()
    await jobs.add(job)
    observed_status_at_render: list[ExportJobStatus] = []

    class _ObservingRenderer(FakeDocumentRenderer):
        async def render(
            self, markdown: str, *, document: TailoredDocumentKind, format: ExportFormat
        ) -> bytes:
            observed_status_at_render.append((await jobs.get(job.id)).status)
            return await super().render(markdown, document=document, format=format)

    renderer = _ObservingRenderer(b"data")
    files = InMemoryFileStore()
    events = RecordingEventPublisher()
    use_case = _use_case(jobs, runs, renderer, files, events, clock)

    await use_case(RenderExportJobCommand(export_job_id=job.id))

    assert observed_status_at_render == [ExportJobStatus.RENDERING]
