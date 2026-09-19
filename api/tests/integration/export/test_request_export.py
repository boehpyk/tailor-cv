"""Application tests for `RequestExport` (T6, RED).

**Why fakes, not a real Postgres:** the same reason every application test in this codebase gives —
`infrastructure/persistence/mapping/export/` has no mapping module yet and there is no migration
bringing `tailorcraft_test` to head (I4-I6 are later tasks), so a test importing `conftest.py`'s
`session`/`engine` fixtures would fail for a reason that has nothing to do with `RequestExport`. This
use case is tested against the ports it actually depends on — `ExportJobRepository`
(`FakeExportJobRepository`) and, through the *real* `GetTailoringRunForSession`, `TailoringRunRepository`
(`FakeTailoringRunRepository`) and `GuestSessionRepository` (`FakeGuestSessionRepository`) — composed
exactly as production does, because the whole point of that composition (per `RequestExport`'s own
docstring) is that the "not mine → 404, never 403" rule is exercised for real here, the same argument
`test_request_tailoring_run.py` and `test_revise_tailored_document.py` make for their own composed
reads.

Every assertion below states what `RequestExport.__call__` **should** do per technical-plan.md's
"Application layer" §1 ("Flow") and feature-spec.md's failure contract rows X-12 … X-18 and X-23,
never what the (currently `NotImplementedError`) code was observed doing. Because the skeleton's
`__call__` body is an unconditional `raise NotImplementedError`, every test below is expected to fail
on that line: a real red, not a vacuous pass, and not an `ImportError`.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest

from tailorcraft.application.export.request_export import (
    RequestExport,
    RequestExportCommand,
    RequestExportResult,
)
from tailorcraft.application.tailoring.get_tailoring_run import GetTailoringRunForSession
from tailorcraft.domain.export.errors import (
    ExportFormatNotQueued,
    TailoringRunNotExportable,
    TooManyExportJobs,
)
from tailorcraft.domain.export.events import ExportRequested
from tailorcraft.domain.export.export_job import ExportJob
from tailorcraft.domain.export.value_objects import (
    ExportFailureReason,
    ExportFormat,
    ExportJobStatus,
)
from tailorcraft.domain.identity.errors import GuestSessionExpired, GuestSessionNotFound
from tailorcraft.domain.identity.value_objects import GuestSessionId
from tailorcraft.domain.tailoring.errors import TailoringRunNotFound, TailoringRunNotOwnedBySession
from tailorcraft.domain.tailoring.value_objects import (
    TailoredCv,
    TailoredDocumentKind,
    TailoringRunId,
)
from tailorcraft.infrastructure.clock import FixedClock
from tests.integration.export.support import failed_run, queued_run, succeeded_run
from tests.integration.fakes import (
    FakeExportJobRepository,
    FakeGuestSessionRepository,
    FakeTailoringRunRepository,
    RecordingEventPublisher,
    create_active_session,
)


def _use_case(
    jobs: FakeExportJobRepository,
    runs: FakeTailoringRunRepository,
    sessions: FakeGuestSessionRepository,
    events: RecordingEventPublisher,
    clock: FixedClock,
    *,
    max_per_session: int = 40,
) -> RequestExport:
    get_tailoring_run = GetTailoringRunForSession(runs, sessions, clock)
    return RequestExport(jobs, get_tailoring_run, events, clock, max_per_session=max_per_session)


# --- Happy path -------------------------------------------------------------------------------


async def test_happy_path_creates_a_queued_job_and_publishes_export_requested(
    clock: FixedClock,
) -> None:
    jobs = FakeExportJobRepository()
    runs = FakeTailoringRunRepository()
    sessions = FakeGuestSessionRepository()
    session = await create_active_session(sessions, clock)
    run = succeeded_run(session_id=session.id)
    await runs.add(run)
    events = RecordingEventPublisher()
    use_case = _use_case(jobs, runs, sessions, events, clock)
    cmd = RequestExportCommand(
        guest_session_id=session.id,
        tailoring_run_id=run.id,
        document=TailoredDocumentKind.CV,
        format=ExportFormat.PDF,
    )

    result = await use_case(cmd)

    assert result.created is True
    assert result.export_job.status is ExportJobStatus.QUEUED
    assert result.export_job.guest_session_id == session.id
    assert result.export_job.tailoring_run_id == run.id
    assert result.export_job.document is TailoredDocumentKind.CV
    assert result.export_job.format is ExportFormat.PDF
    assert result.export_job.run_version == run.version

    assert jobs.all() == [result.export_job]

    published = [e for e in events.published if isinstance(e, ExportRequested)]
    assert len(published) == 1
    assert published[0].export_job_id == result.export_job.id
    assert published[0].guest_session_id == session.id
    assert published[0].tailoring_run_id == run.id
    assert published[0].document is TailoredDocumentKind.CV
    assert published[0].format is ExportFormat.PDF
    assert published[0].run_version == run.version


# --- X-12 / X-13: session and run resolution, inherited whole from GetTailoringRunForSession ---


async def test_expired_guest_session_raises_guest_session_expired(clock: FixedClock) -> None:
    jobs = FakeExportJobRepository()
    runs = FakeTailoringRunRepository()
    sessions = FakeGuestSessionRepository()
    session = await create_active_session(sessions, clock, ttl_hours=1)
    run = succeeded_run(session_id=session.id)
    await runs.add(run)
    events = RecordingEventPublisher()
    stale_clock = FixedClock(clock.now() + timedelta(hours=2))
    use_case = _use_case(jobs, runs, sessions, events, stale_clock)
    cmd = RequestExportCommand(
        guest_session_id=session.id,
        tailoring_run_id=run.id,
        document=TailoredDocumentKind.CV,
        format=ExportFormat.PDF,
    )

    with pytest.raises(GuestSessionExpired):
        await use_case(cmd)

    assert jobs.all() == []


async def test_unknown_guest_session_raises_guest_session_not_found(clock: FixedClock) -> None:
    jobs = FakeExportJobRepository()
    runs = FakeTailoringRunRepository()
    sessions = FakeGuestSessionRepository()
    events = RecordingEventPublisher()
    use_case = _use_case(jobs, runs, sessions, events, clock)
    cmd = RequestExportCommand(
        guest_session_id=GuestSessionId(value=uuid4()),
        tailoring_run_id=TailoringRunId(value=uuid4()),
        document=TailoredDocumentKind.CV,
        format=ExportFormat.PDF,
    )

    with pytest.raises(GuestSessionNotFound):
        await use_case(cmd)

    assert jobs.all() == []


async def test_run_owned_by_a_different_session_raises_tailoring_run_not_found_chained_from_not_owned(
    clock: FixedClock,
) -> None:
    """X-13's collapse: "not mine" and "does not exist" must be indistinguishable to the public
    exception type, and `__cause__` is asserted rather than only the type, so that a GREEN
    implementation which stopped chaining the two exceptions — losing the distinction this use
    case's own tests (and `GetTailoringRunForSession`'s) need to prove the ownership check fired at
    all — would turn this assertion red rather than leaving it silently unable to tell the two
    apart."""
    jobs = FakeExportJobRepository()
    runs = FakeTailoringRunRepository()
    sessions = FakeGuestSessionRepository()
    owner = await create_active_session(sessions, clock, token_hash="a" * 64)
    stranger = await create_active_session(sessions, clock, token_hash="b" * 64)
    run = succeeded_run(session_id=owner.id)
    await runs.add(run)
    events = RecordingEventPublisher()
    use_case = _use_case(jobs, runs, sessions, events, clock)
    cmd = RequestExportCommand(
        guest_session_id=stranger.id,
        tailoring_run_id=run.id,
        document=TailoredDocumentKind.CV,
        format=ExportFormat.PDF,
    )

    with pytest.raises(TailoringRunNotFound) as exc_info:
        await use_case(cmd)

    assert isinstance(exc_info.value.__cause__, TailoringRunNotOwnedBySession)
    assert jobs.all() == []


async def test_nonexistent_run_raises_tailoring_run_not_found(clock: FixedClock) -> None:
    jobs = FakeExportJobRepository()
    runs = FakeTailoringRunRepository()
    sessions = FakeGuestSessionRepository()
    session = await create_active_session(sessions, clock)
    events = RecordingEventPublisher()
    use_case = _use_case(jobs, runs, sessions, events, clock)
    cmd = RequestExportCommand(
        guest_session_id=session.id,
        tailoring_run_id=TailoringRunId(value=uuid4()),
        document=TailoredDocumentKind.CV,
        format=ExportFormat.PDF,
    )

    with pytest.raises(TailoringRunNotFound):
        await use_case(cmd)

    assert jobs.all() == []


# --- X-14: the run is not `succeeded` -----------------------------------------------------------


async def test_queued_run_raises_tailoring_run_not_exportable_carrying_its_status(
    clock: FixedClock,
) -> None:
    jobs = FakeExportJobRepository()
    runs = FakeTailoringRunRepository()
    sessions = FakeGuestSessionRepository()
    session = await create_active_session(sessions, clock)
    run = queued_run(session_id=session.id)
    await runs.add(run)
    events = RecordingEventPublisher()
    use_case = _use_case(jobs, runs, sessions, events, clock)
    cmd = RequestExportCommand(
        guest_session_id=session.id,
        tailoring_run_id=run.id,
        document=TailoredDocumentKind.CV,
        format=ExportFormat.PDF,
    )

    with pytest.raises(TailoringRunNotExportable) as exc_info:
        await use_case(cmd)

    assert exc_info.value.status is run.status
    assert jobs.all() == []


async def test_failed_run_raises_tailoring_run_not_exportable(clock: FixedClock) -> None:
    jobs = FakeExportJobRepository()
    runs = FakeTailoringRunRepository()
    sessions = FakeGuestSessionRepository()
    session = await create_active_session(sessions, clock)
    run = failed_run(session_id=session.id)
    await runs.add(run)
    events = RecordingEventPublisher()
    use_case = _use_case(jobs, runs, sessions, events, clock)
    cmd = RequestExportCommand(
        guest_session_id=session.id,
        tailoring_run_id=run.id,
        document=TailoredDocumentKind.CV,
        format=ExportFormat.PDF,
    )

    with pytest.raises(TailoringRunNotExportable) as exc_info:
        await use_case(cmd)

    assert exc_info.value.status is run.status
    assert jobs.all() == []


# --- X-15: an inline format reaches the use case ---------------------------------------------


async def test_inline_format_raises_export_format_not_queued(clock: FixedClock) -> None:
    """The command carries the whole `ExportFormat` enum (technical-plan.md's note on
    `RequestExportCommand.format`), so an inline member is a value this use case must refuse itself
    rather than one the type system already ruled out — `ExportJob.request` (XJ-2) is what refuses
    it, and nothing is ever added first."""
    jobs = FakeExportJobRepository()
    runs = FakeTailoringRunRepository()
    sessions = FakeGuestSessionRepository()
    session = await create_active_session(sessions, clock)
    run = succeeded_run(session_id=session.id)
    await runs.add(run)
    events = RecordingEventPublisher()
    use_case = _use_case(jobs, runs, sessions, events, clock)
    cmd = RequestExportCommand(
        guest_session_id=session.id,
        tailoring_run_id=run.id,
        document=TailoredDocumentKind.CV,
        format=ExportFormat.MD,
    )

    with pytest.raises(ExportFormatNotQueued) as exc_info:
        await use_case(cmd)

    assert exc_info.value.format is ExportFormat.MD
    assert jobs.all() == []


# --- X-16: an idempotent lookup returns the existing current job, twice over --------------------


@pytest.mark.parametrize("status", [ExportJobStatus.QUEUED, ExportJobStatus.RENDERING])
async def test_existing_non_terminal_job_at_the_current_version_is_returned_unchanged(
    clock: FixedClock, status: ExportJobStatus
) -> None:
    jobs = FakeExportJobRepository()
    runs = FakeTailoringRunRepository()
    sessions = FakeGuestSessionRepository()
    session = await create_active_session(sessions, clock)
    run = succeeded_run(session_id=session.id)
    await runs.add(run)
    existing = ExportJob.request(
        id=jobs.next_identity(),
        guest_session_id=session.id,
        tailoring_run_id=run.id,
        document=TailoredDocumentKind.CV,
        format=ExportFormat.PDF,
        run_version=run.version,
        requested_at=clock.now() - timedelta(minutes=1),
    )
    if status is ExportJobStatus.RENDERING:
        existing.mark_started(clock.now())
    await jobs.add(existing)
    events = RecordingEventPublisher()
    use_case = _use_case(jobs, runs, sessions, events, clock)
    cmd = RequestExportCommand(
        guest_session_id=session.id,
        tailoring_run_id=run.id,
        document=TailoredDocumentKind.CV,
        format=ExportFormat.PDF,
    )

    result = await use_case(cmd)

    assert result == RequestExportResult(existing, created=False)
    # No add: the repository holds exactly the one job this test seeded.
    assert jobs.all() == [existing]
    # No task published either — `RequestExport` never enqueues at all, but the stronger, positive
    # proof for *this* row is that nothing new was even written for a task to be published about.
    assert events.published == []


async def test_existing_ready_job_at_the_current_version_is_returned_unchanged(
    clock: FixedClock,
) -> None:
    jobs = FakeExportJobRepository()
    runs = FakeTailoringRunRepository()
    sessions = FakeGuestSessionRepository()
    session = await create_active_session(sessions, clock)
    run = succeeded_run(session_id=session.id)
    await runs.add(run)
    existing = ExportJob.request(
        id=jobs.next_identity(),
        guest_session_id=session.id,
        tailoring_run_id=run.id,
        document=TailoredDocumentKind.CV,
        format=ExportFormat.PDF,
        run_version=run.version,
        requested_at=clock.now() - timedelta(minutes=2),
    )
    existing.mark_started(clock.now() - timedelta(minutes=1))
    existing.mark_ready(byte_size=1_000, render_duration_ms=500, at=clock.now())
    await jobs.add(existing)
    events = RecordingEventPublisher()
    use_case = _use_case(jobs, runs, sessions, events, clock)
    cmd = RequestExportCommand(
        guest_session_id=session.id,
        tailoring_run_id=run.id,
        document=TailoredDocumentKind.CV,
        format=ExportFormat.PDF,
    )

    result = await use_case(cmd)

    assert result == RequestExportResult(existing, created=False)
    assert jobs.all() == [existing]
    assert events.published == []


# --- X-17: the latest job for the key is `failed`, or stale by version — both create a new job --


async def test_failed_latest_job_for_the_key_is_superseded_by_a_new_job(clock: FixedClock) -> None:
    jobs = FakeExportJobRepository()
    runs = FakeTailoringRunRepository()
    sessions = FakeGuestSessionRepository()
    session = await create_active_session(sessions, clock)
    run = succeeded_run(session_id=session.id)
    await runs.add(run)
    stale = ExportJob.request(
        id=jobs.next_identity(),
        guest_session_id=session.id,
        tailoring_run_id=run.id,
        document=TailoredDocumentKind.CV,
        format=ExportFormat.PDF,
        run_version=run.version,
        requested_at=clock.now() - timedelta(minutes=2),
    )
    stale.mark_started(clock.now() - timedelta(minutes=1))
    stale.mark_failed(ExportFailureReason.RENDER_FAILED, clock.now())
    await jobs.add(stale)
    events = RecordingEventPublisher()
    use_case = _use_case(jobs, runs, sessions, events, clock)
    cmd = RequestExportCommand(
        guest_session_id=session.id,
        tailoring_run_id=run.id,
        document=TailoredDocumentKind.CV,
        format=ExportFormat.PDF,
    )

    result = await use_case(cmd)

    assert result.created is True
    assert result.export_job.id != stale.id
    assert result.export_job.status is ExportJobStatus.QUEUED
    assert {job.id for job in jobs.all()} == {stale.id, result.export_job.id}


async def test_latest_job_requested_for_a_stale_run_version_is_superseded_by_a_new_job(
    clock: FixedClock,
) -> None:
    jobs = FakeExportJobRepository()
    runs = FakeTailoringRunRepository()
    sessions = FakeGuestSessionRepository()
    session = await create_active_session(sessions, clock)
    run = succeeded_run(session_id=session.id)
    await runs.add(run)
    stale = ExportJob.request(
        id=jobs.next_identity(),
        guest_session_id=session.id,
        tailoring_run_id=run.id,
        document=TailoredDocumentKind.CV,
        format=ExportFormat.PDF,
        run_version=run.version,
        requested_at=clock.now() - timedelta(minutes=2),
    )
    await jobs.add(stale)
    # The user edited the document after requesting the export: the run's version moves on.
    run.revise_cv(TailoredCv("z" * 450), expected_version=run.version, at=clock.now())
    events = RecordingEventPublisher()
    use_case = _use_case(jobs, runs, sessions, events, clock)
    cmd = RequestExportCommand(
        guest_session_id=session.id,
        tailoring_run_id=run.id,
        document=TailoredDocumentKind.CV,
        format=ExportFormat.PDF,
    )

    result = await use_case(cmd)

    assert result.created is True
    assert result.export_job.id != stale.id
    assert result.export_job.run_version == run.version
    assert result.export_job.run_version != stale.run_version
    assert {job.id for job in jobs.all()} == {stale.id, result.export_job.id}


# --- X-18: the per-session cap -------------------------------------------------------------------


async def test_session_at_the_cap_raises_too_many_export_jobs(clock: FixedClock) -> None:
    jobs = FakeExportJobRepository()
    runs = FakeTailoringRunRepository()
    sessions = FakeGuestSessionRepository()
    session = await create_active_session(sessions, clock)
    run = succeeded_run(session_id=session.id)
    await runs.add(run)
    # 40 jobs already owned by this session, for a *different* document so the idempotent lookup
    # (step 3) cannot short-circuit before the cap (step 4) is even reached.
    for _ in range(40):
        other = ExportJob.request(
            id=jobs.next_identity(),
            guest_session_id=session.id,
            tailoring_run_id=run.id,
            document=TailoredDocumentKind.COVER_LETTER,
            format=ExportFormat.DOCX,
            run_version=run.version,
            requested_at=clock.now() - timedelta(minutes=1),
        )
        await jobs.add(other)
    events = RecordingEventPublisher()
    use_case = _use_case(jobs, runs, sessions, events, clock, max_per_session=40)
    cmd = RequestExportCommand(
        guest_session_id=session.id,
        tailoring_run_id=run.id,
        document=TailoredDocumentKind.CV,
        format=ExportFormat.PDF,
    )

    with pytest.raises(TooManyExportJobs):
        await use_case(cmd)

    assert len(jobs.all()) == 40


async def test_a_returning_current_job_is_handed_back_even_when_the_session_is_at_the_cap(
    clock: FixedClock,
) -> None:
    """The order of steps 3 and 4 is load-bearing (technical-plan.md): the idempotent lookup runs
    *before* the cap, so a visitor already at the limit who re-requests an export that already
    exists is handed that job rather than a 409 that would make a working repeat request fail."""
    jobs = FakeExportJobRepository()
    runs = FakeTailoringRunRepository()
    sessions = FakeGuestSessionRepository()
    session = await create_active_session(sessions, clock)
    run = succeeded_run(session_id=session.id)
    await runs.add(run)
    existing = ExportJob.request(
        id=jobs.next_identity(),
        guest_session_id=session.id,
        tailoring_run_id=run.id,
        document=TailoredDocumentKind.CV,
        format=ExportFormat.PDF,
        run_version=run.version,
        requested_at=clock.now() - timedelta(minutes=1),
    )
    await jobs.add(existing)
    for _ in range(39):
        other = ExportJob.request(
            id=jobs.next_identity(),
            guest_session_id=session.id,
            tailoring_run_id=run.id,
            document=TailoredDocumentKind.COVER_LETTER,
            format=ExportFormat.DOCX,
            run_version=run.version,
            requested_at=clock.now() - timedelta(minutes=1),
        )
        await jobs.add(other)
    assert len(jobs.all()) == 40
    events = RecordingEventPublisher()
    use_case = _use_case(jobs, runs, sessions, events, clock, max_per_session=40)
    cmd = RequestExportCommand(
        guest_session_id=session.id,
        tailoring_run_id=run.id,
        document=TailoredDocumentKind.CV,
        format=ExportFormat.PDF,
    )

    result = await use_case(cmd)

    assert result == RequestExportResult(existing, created=False)
    assert len(jobs.all()) == 40
