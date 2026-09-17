"""Application tests for `DownloadExportFile` (T6, RED).

**Why fakes, not a real Postgres:** the same reason every file in this package gives. This use case
is tested against the ports it actually depends on — `FileStorePort` (`InMemoryFileStore` /
`MissingFileStore` / `AlwaysFailingFileStore`) and, through the *real* `GetExportJobForSession`,
`ExportJobRepository` (`FakeExportJobRepository`), `TailoringRunRepository`
(`FakeTailoringRunRepository`) and `GuestSessionRepository` (`FakeGuestSessionRepository`) — composed
exactly as production does, because the ownership check is `GetExportJobForSession`'s and this file's
job is to prove `DownloadExportFile` inherits it rather than re-implementing it.

Every assertion below states what `DownloadExportFile.__call__` **should** do per technical-plan.md's
"Application layer" §6 ("Flow") and feature-spec.md's failure contract rows X-43 … X-48, never what
the (currently `NotImplementedError`) code was observed doing. Because the skeleton's `__call__` body
is an unconditional `raise NotImplementedError`, every test below is expected to fail on that line: a
real red, not a vacuous pass, and not an `ImportError`.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from tailorcraft.application.export.download_export_file import DownloadExportFile
from tailorcraft.application.export.get_export_job import GetExportJobForSession
from tailorcraft.domain.export.errors import ExportNotReady
from tailorcraft.domain.export.export_job import ExportJob
from tailorcraft.domain.export.value_objects import ExportFailureReason, ExportFormat, ExportJobId
from tailorcraft.domain.identity.value_objects import GuestSessionId
from tailorcraft.domain.shared.files import FileStoreUnavailable, StoredFileMissing
from tailorcraft.domain.tailoring.value_objects import TailoredDocumentKind, TailoringRunId
from tailorcraft.infrastructure.clock import FixedClock
from tests.integration.export.support import succeeded_run
from tests.integration.fakes import (
    AlwaysFailingFileStore,
    FakeExportJobRepository,
    FakeGuestSessionRepository,
    FakeTailoringRunRepository,
    InMemoryFileStore,
    MissingFileStore,
    create_active_session,
)


def _use_case(
    jobs: FakeExportJobRepository,
    runs: FakeTailoringRunRepository,
    sessions: FakeGuestSessionRepository,
    files: InMemoryFileStore | MissingFileStore | AlwaysFailingFileStore,
    clock: FixedClock,
) -> DownloadExportFile:
    get_export_job = GetExportJobForSession(jobs, runs, sessions, clock)
    return DownloadExportFile(get_export_job, files)


def _a_job(
    *,
    session_id: GuestSessionId,
    run_id: TailoringRunId,
    run_version: int,
    clock: FixedClock,
) -> ExportJob:
    return ExportJob.request(
        id=ExportJobId(value=uuid4()),
        guest_session_id=session_id,
        tailoring_run_id=run_id,
        document=TailoredDocumentKind.CV,
        format=ExportFormat.PDF,
        run_version=run_version,
        requested_at=clock.now(),
    )


# --- Ready --------------------------------------------------------------------------------------


async def test_ready_job_returns_the_job_and_its_bytes(clock: FixedClock) -> None:
    sessions = FakeGuestSessionRepository()
    session = await create_active_session(sessions, clock)
    runs = FakeTailoringRunRepository()
    run = succeeded_run(session_id=session.id)
    await runs.add(run)
    job = _a_job(session_id=session.id, run_id=run.id, run_version=run.version, clock=clock)
    job.mark_started(clock.now())
    data = b"the rendered pdf bytes"
    job.mark_ready(byte_size=len(data), render_duration_ms=250, at=clock.now())
    jobs = FakeExportJobRepository()
    await jobs.add(job)
    files = InMemoryFileStore()
    files.data[job.storage_ref.key] = data
    use_case = _use_case(jobs, runs, sessions, files, clock)

    result_job, result_data = await use_case(job.id, session.id)

    assert result_job.id == job.id
    assert result_data == data


# --- X-44 / X-45: every non-ready status raises ExportNotReady, with the right fields -----------


async def test_queued_job_raises_export_not_ready(clock: FixedClock) -> None:
    sessions = FakeGuestSessionRepository()
    session = await create_active_session(sessions, clock)
    runs = FakeTailoringRunRepository()
    run = succeeded_run(session_id=session.id)
    await runs.add(run)
    job = _a_job(session_id=session.id, run_id=run.id, run_version=run.version, clock=clock)
    jobs = FakeExportJobRepository()
    await jobs.add(job)
    files = InMemoryFileStore()
    use_case = _use_case(jobs, runs, sessions, files, clock)

    with pytest.raises(ExportNotReady) as exc_info:
        await use_case(job.id, session.id)

    assert exc_info.value.status is job.status
    assert exc_info.value.failure_reason is None


async def test_rendering_job_raises_export_not_ready(clock: FixedClock) -> None:
    sessions = FakeGuestSessionRepository()
    session = await create_active_session(sessions, clock)
    runs = FakeTailoringRunRepository()
    run = succeeded_run(session_id=session.id)
    await runs.add(run)
    job = _a_job(session_id=session.id, run_id=run.id, run_version=run.version, clock=clock)
    job.mark_started(clock.now())
    jobs = FakeExportJobRepository()
    await jobs.add(job)
    files = InMemoryFileStore()
    use_case = _use_case(jobs, runs, sessions, files, clock)

    with pytest.raises(ExportNotReady) as exc_info:
        await use_case(job.id, session.id)

    assert exc_info.value.status is job.status
    assert exc_info.value.failure_reason is None


async def test_failed_job_raises_export_not_ready_carrying_the_failure_reason(
    clock: FixedClock,
) -> None:
    sessions = FakeGuestSessionRepository()
    session = await create_active_session(sessions, clock)
    runs = FakeTailoringRunRepository()
    run = succeeded_run(session_id=session.id)
    await runs.add(run)
    job = _a_job(session_id=session.id, run_id=run.id, run_version=run.version, clock=clock)
    job.mark_started(clock.now())
    job.mark_failed(ExportFailureReason.RENDER_FAILED, clock.now())
    jobs = FakeExportJobRepository()
    await jobs.add(job)
    files = InMemoryFileStore()
    use_case = _use_case(jobs, runs, sessions, files, clock)

    with pytest.raises(ExportNotReady) as exc_info:
        await use_case(job.id, session.id)

    assert exc_info.value.status is job.status
    assert exc_info.value.failure_reason is ExportFailureReason.RENDER_FAILED


# --- X-47 / X-48: the store fails a `ready` job's read -------------------------------------------


async def test_missing_file_raises_stored_file_missing(clock: FixedClock) -> None:
    sessions = FakeGuestSessionRepository()
    session = await create_active_session(sessions, clock)
    runs = FakeTailoringRunRepository()
    run = succeeded_run(session_id=session.id)
    await runs.add(run)
    job = _a_job(session_id=session.id, run_id=run.id, run_version=run.version, clock=clock)
    job.mark_started(clock.now())
    job.mark_ready(byte_size=10, render_duration_ms=10, at=clock.now())
    jobs = FakeExportJobRepository()
    await jobs.add(job)
    files = MissingFileStore()
    use_case = _use_case(jobs, runs, sessions, files, clock)

    with pytest.raises(StoredFileMissing):
        await use_case(job.id, session.id)


async def test_unreadable_store_raises_file_store_unavailable(clock: FixedClock) -> None:
    sessions = FakeGuestSessionRepository()
    session = await create_active_session(sessions, clock)
    runs = FakeTailoringRunRepository()
    run = succeeded_run(session_id=session.id)
    await runs.add(run)
    job = _a_job(session_id=session.id, run_id=run.id, run_version=run.version, clock=clock)
    job.mark_started(clock.now())
    job.mark_ready(byte_size=10, render_duration_ms=10, at=clock.now())
    jobs = FakeExportJobRepository()
    await jobs.add(job)
    files = AlwaysFailingFileStore()
    use_case = _use_case(jobs, runs, sessions, files, clock)

    with pytest.raises(FileStoreUnavailable):
        await use_case(job.id, session.id)
