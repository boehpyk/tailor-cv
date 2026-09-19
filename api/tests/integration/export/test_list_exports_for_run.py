"""Application tests for `ListExportsForRun` (T6, RED).

**Why fakes, not a real Postgres:** the same reason every file in this package gives. This use case
is tested against the ports it actually depends on — `ExportJobRepository`
(`FakeExportJobRepository`) and, through the *real* `GetTailoringRunForSession`,
`TailoringRunRepository` (`FakeTailoringRunRepository`) and `GuestSessionRepository`
(`FakeGuestSessionRepository`).

Every assertion below states what `ListExportsForRun.__call__` **should** do per
technical-plan.md's "Application layer" §5 ("Flow"), never what the (currently
`NotImplementedError`) code was observed doing. Because the skeleton's `__call__` body is an
unconditional `raise NotImplementedError`, every test below is expected to fail on that line: a real
red, not a vacuous pass, and not an `ImportError`.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import uuid4

import pytest

from tailorcraft.application.export.list_exports_for_run import (
    ExportListing,
    ListExportsForRun,
)
from tailorcraft.application.tailoring.get_tailoring_run import GetTailoringRunForSession
from tailorcraft.domain.export.export_job import ExportJob
from tailorcraft.domain.export.value_objects import ExportFormat, ExportJobId
from tailorcraft.domain.identity.errors import GuestSessionExpired, GuestSessionNotFound
from tailorcraft.domain.identity.value_objects import GuestSessionId
from tailorcraft.domain.tailoring.errors import TailoringRunNotFound, TailoringRunNotOwnedBySession
from tailorcraft.domain.tailoring.value_objects import TailoredDocumentKind, TailoringRunId
from tailorcraft.infrastructure.clock import FixedClock
from tests.integration.export.support import queued_run, succeeded_run
from tests.integration.fakes import (
    FakeExportJobRepository,
    FakeGuestSessionRepository,
    FakeTailoringRunRepository,
    create_active_session,
)


def _use_case(
    jobs: FakeExportJobRepository,
    runs: FakeTailoringRunRepository,
    sessions: FakeGuestSessionRepository,
    clock: FixedClock,
) -> ListExportsForRun:
    get_tailoring_run = GetTailoringRunForSession(runs, sessions, clock)
    return ListExportsForRun(jobs, get_tailoring_run)


def _a_job(
    *,
    session_id: GuestSessionId,
    run_id: TailoringRunId,
    run_version: int,
    document: TailoredDocumentKind,
    format: ExportFormat,
    requested_at: datetime,
) -> ExportJob:
    return ExportJob.request(
        id=ExportJobId(value=uuid4()),
        guest_session_id=session_id,
        tailoring_run_id=run_id,
        document=document,
        format=format,
        run_version=run_version,
        requested_at=requested_at,
    )


async def test_returns_every_job_for_the_run_newest_first_with_the_runs_version(
    clock: FixedClock,
) -> None:
    sessions = FakeGuestSessionRepository()
    session = await create_active_session(sessions, clock)
    runs = FakeTailoringRunRepository()
    run = succeeded_run(session_id=session.id)
    await runs.add(run)
    jobs = FakeExportJobRepository()
    older = _a_job(
        session_id=session.id,
        run_id=run.id,
        run_version=run.version,
        document=TailoredDocumentKind.CV,
        format=ExportFormat.PDF,
        requested_at=clock.now() - timedelta(minutes=2),
    )
    newer = _a_job(
        session_id=session.id,
        run_id=run.id,
        run_version=run.version,
        document=TailoredDocumentKind.COVER_LETTER,
        format=ExportFormat.DOCX,
        requested_at=clock.now() - timedelta(minutes=1),
    )
    await jobs.add(older)
    await jobs.add(newer)
    use_case = _use_case(jobs, runs, sessions, clock)

    result = await use_case(run.id, session.id)

    assert result == ExportListing(jobs=[newer, older], run_version=run.version)


async def test_run_with_no_exports_returns_an_empty_list(clock: FixedClock) -> None:
    sessions = FakeGuestSessionRepository()
    session = await create_active_session(sessions, clock)
    runs = FakeTailoringRunRepository()
    run = succeeded_run(session_id=session.id)
    await runs.add(run)
    jobs = FakeExportJobRepository()
    use_case = _use_case(jobs, runs, sessions, clock)

    result = await use_case(run.id, session.id)

    assert result == ExportListing(jobs=[], run_version=run.version)


async def test_run_that_is_not_succeeded_still_returns_its_exports_not_an_error(
    clock: FixedClock,
) -> None:
    """Unlike `RequestExport` and `RenderDocumentInline`, listing raises no
    `TailoringRunNotExportable`: a run's exports are a valid question whatever its status."""
    sessions = FakeGuestSessionRepository()
    session = await create_active_session(sessions, clock)
    runs = FakeTailoringRunRepository()
    run = queued_run(session_id=session.id)
    await runs.add(run)
    jobs = FakeExportJobRepository()
    use_case = _use_case(jobs, runs, sessions, clock)

    result = await use_case(run.id, session.id)

    assert result == ExportListing(jobs=[], run_version=run.version)


async def test_run_owned_by_a_different_session_raises_tailoring_run_not_found_chained_from_not_owned(
    clock: FixedClock,
) -> None:
    sessions = FakeGuestSessionRepository()
    owner = await create_active_session(sessions, clock, token_hash="a" * 64)
    stranger = await create_active_session(sessions, clock, token_hash="b" * 64)
    runs = FakeTailoringRunRepository()
    run = succeeded_run(session_id=owner.id)
    await runs.add(run)
    jobs = FakeExportJobRepository()
    use_case = _use_case(jobs, runs, sessions, clock)

    with pytest.raises(TailoringRunNotFound) as exc_info:
        await use_case(run.id, stranger.id)

    assert isinstance(exc_info.value.__cause__, TailoringRunNotOwnedBySession)


async def test_nonexistent_run_raises_tailoring_run_not_found(clock: FixedClock) -> None:
    sessions = FakeGuestSessionRepository()
    session = await create_active_session(sessions, clock)
    runs = FakeTailoringRunRepository()
    jobs = FakeExportJobRepository()
    use_case = _use_case(jobs, runs, sessions, clock)

    with pytest.raises(TailoringRunNotFound):
        await use_case(TailoringRunId(value=uuid4()), session.id)


async def test_expired_guest_session_raises_guest_session_expired(clock: FixedClock) -> None:
    sessions = FakeGuestSessionRepository()
    session = await create_active_session(sessions, clock, ttl_hours=1)
    runs = FakeTailoringRunRepository()
    jobs = FakeExportJobRepository()
    stale_clock = FixedClock(clock.now() + timedelta(hours=2))
    use_case = _use_case(jobs, runs, sessions, stale_clock)

    with pytest.raises(GuestSessionExpired):
        await use_case(TailoringRunId(value=uuid4()), session.id)


async def test_unknown_guest_session_raises_guest_session_not_found(clock: FixedClock) -> None:
    sessions = FakeGuestSessionRepository()
    runs = FakeTailoringRunRepository()
    jobs = FakeExportJobRepository()
    use_case = _use_case(jobs, runs, sessions, clock)

    with pytest.raises(GuestSessionNotFound):
        await use_case(TailoringRunId(value=uuid4()), GuestSessionId(value=uuid4()))
