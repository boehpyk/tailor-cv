"""Application tests for `GetExportJobForSession` (T6, RED).

**Why fakes, not a real Postgres:** the same reason every file in this package gives. This use case
is tested against the ports it actually depends on — `ExportJobRepository`
(`FakeExportJobRepository`), `TailoringRunRepository` (`FakeTailoringRunRepository`) and
`GuestSessionRepository` (`FakeGuestSessionRepository`).

Every assertion below states what `GetExportJobForSession.__call__` **should** do per
technical-plan.md's "Application layer" §4 ("Flow") and feature-spec.md's failure contract row X-43,
never what the (currently `NotImplementedError`) code was observed doing. Because the skeleton's
`__call__` body is an unconditional `raise NotImplementedError`, every test below is expected to fail
on that line: a real red, not a vacuous pass, and not an `ImportError`.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest

from tailorcraft.application.export.get_export_job import ExportJobLookup, GetExportJobForSession
from tailorcraft.domain.export.errors import ExportJobNotFound, ExportJobNotOwnedBySession
from tailorcraft.domain.export.export_job import ExportJob
from tailorcraft.domain.export.value_objects import ExportFormat, ExportJobId
from tailorcraft.domain.identity.errors import GuestSessionExpired, GuestSessionNotFound
from tailorcraft.domain.identity.value_objects import GuestSessionId
from tailorcraft.domain.tailoring.value_objects import TailoredCv, TailoredDocumentKind
from tailorcraft.infrastructure.clock import FixedClock
from tests.integration.export.support import succeeded_run
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
) -> GetExportJobForSession:
    return GetExportJobForSession(jobs, runs, sessions, clock)


async def test_returns_the_job_and_the_runs_current_version_for_the_owning_session(
    clock: FixedClock,
) -> None:
    sessions = FakeGuestSessionRepository()
    session = await create_active_session(sessions, clock)
    runs = FakeTailoringRunRepository()
    run = succeeded_run(session_id=session.id)
    await runs.add(run)
    job = ExportJob.request(
        id=ExportJobId(value=uuid4()),
        guest_session_id=session.id,
        tailoring_run_id=run.id,
        document=TailoredDocumentKind.CV,
        format=ExportFormat.PDF,
        run_version=run.version,
        requested_at=clock.now(),
    )
    jobs = FakeExportJobRepository()
    await jobs.add(job)
    use_case = _use_case(jobs, runs, sessions, clock)

    result = await use_case(job.id, session.id)

    assert result == ExportJobLookup(job=job, run_version_now=run.version)


async def test_run_gone_reports_run_version_now_as_none(clock: FixedClock) -> None:
    sessions = FakeGuestSessionRepository()
    session = await create_active_session(sessions, clock)
    runs = FakeTailoringRunRepository()
    run = succeeded_run(session_id=session.id)
    # The run itself is never added — purged out from under a job that still exists.
    job = ExportJob.request(
        id=ExportJobId(value=uuid4()),
        guest_session_id=session.id,
        tailoring_run_id=run.id,
        document=TailoredDocumentKind.CV,
        format=ExportFormat.PDF,
        run_version=run.version,
        requested_at=clock.now(),
    )
    jobs = FakeExportJobRepository()
    await jobs.add(job)
    use_case = _use_case(jobs, runs, sessions, clock)

    result = await use_case(job.id, session.id)

    assert result.job == job
    assert result.run_version_now is None


async def test_run_that_moved_on_reports_the_runs_new_version(clock: FixedClock) -> None:
    sessions = FakeGuestSessionRepository()
    session = await create_active_session(sessions, clock)
    runs = FakeTailoringRunRepository()
    run = succeeded_run(session_id=session.id)
    job = ExportJob.request(
        id=ExportJobId(value=uuid4()),
        guest_session_id=session.id,
        tailoring_run_id=run.id,
        document=TailoredDocumentKind.CV,
        format=ExportFormat.PDF,
        run_version=run.version,
        requested_at=clock.now(),
    )
    requested_version = run.version
    run.revise_cv(TailoredCv("z" * 450), expected_version=run.version, at=clock.now())
    await runs.add(run)
    jobs = FakeExportJobRepository()
    await jobs.add(job)
    use_case = _use_case(jobs, runs, sessions, clock)

    result = await use_case(job.id, session.id)

    assert result.run_version_now == run.version
    assert result.run_version_now != requested_version


async def test_job_owned_by_a_different_session_raises_export_job_not_found_chained_from_not_owned(
    clock: FixedClock,
) -> None:
    """X-43's collapse: "not mine" and "does not exist" must be indistinguishable to the public
    exception type, and `__cause__` is asserted rather than only the type, so a GREEN
    implementation that stopped chaining the two would turn this assertion red rather than leaving
    it silently unable to prove the ownership check exists at all."""
    sessions = FakeGuestSessionRepository()
    owner = await create_active_session(sessions, clock, token_hash="a" * 64)
    stranger = await create_active_session(sessions, clock, token_hash="b" * 64)
    runs = FakeTailoringRunRepository()
    run = succeeded_run(session_id=owner.id)
    await runs.add(run)
    job = ExportJob.request(
        id=ExportJobId(value=uuid4()),
        guest_session_id=owner.id,
        tailoring_run_id=run.id,
        document=TailoredDocumentKind.CV,
        format=ExportFormat.PDF,
        run_version=run.version,
        requested_at=clock.now(),
    )
    jobs = FakeExportJobRepository()
    await jobs.add(job)
    use_case = _use_case(jobs, runs, sessions, clock)

    with pytest.raises(ExportJobNotFound) as exc_info:
        await use_case(job.id, stranger.id)

    assert isinstance(exc_info.value.__cause__, ExportJobNotOwnedBySession)


async def test_nonexistent_job_raises_export_job_not_found_without_an_ownership_cause(
    clock: FixedClock,
) -> None:
    """The contrast case for the test above: `__cause__` here must **not** be an
    `ExportJobNotOwnedBySession`, proving the distinction survives on the cause rather than this
    test accidentally passing because nothing ever sets it to that type at all."""
    sessions = FakeGuestSessionRepository()
    session = await create_active_session(sessions, clock)
    jobs = FakeExportJobRepository()
    runs = FakeTailoringRunRepository()
    use_case = _use_case(jobs, runs, sessions, clock)

    with pytest.raises(ExportJobNotFound) as exc_info:
        await use_case(ExportJobId(value=uuid4()), session.id)

    assert not isinstance(exc_info.value.__cause__, ExportJobNotOwnedBySession)


async def test_expired_guest_session_raises_guest_session_expired(clock: FixedClock) -> None:
    sessions = FakeGuestSessionRepository()
    session = await create_active_session(sessions, clock, ttl_hours=1)
    jobs = FakeExportJobRepository()
    runs = FakeTailoringRunRepository()
    stale_clock = FixedClock(clock.now() + timedelta(hours=2))
    use_case = _use_case(jobs, runs, sessions, stale_clock)

    with pytest.raises(GuestSessionExpired):
        await use_case(ExportJobId(value=uuid4()), session.id)


async def test_unknown_guest_session_raises_guest_session_not_found(clock: FixedClock) -> None:
    jobs = FakeExportJobRepository()
    runs = FakeTailoringRunRepository()
    sessions = FakeGuestSessionRepository()
    use_case = _use_case(jobs, runs, sessions, clock)

    with pytest.raises(GuestSessionNotFound):
        await use_case(ExportJobId(value=uuid4()), GuestSessionId(value=uuid4()))
