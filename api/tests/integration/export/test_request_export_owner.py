"""Application tests for `RequestExport`'s owner and cap (slice 2.3, T10 RED, AC-11, H-37).

**The job's owner is taken from the run** (`owner=run.owner`), never from the command — *"a job's
owner is its run's owner"* is a cross-aggregate invariant, and `RequestExport` is where it is kept.
Through the real `GetTailoringRun` the resolved requester and `run.owner` are always equal values, so
an honest end-to-end test cannot tell the two sources apart. `_RunHandedOver` makes the difference
observable: a `GetTailoringRun` whose authorization has already happened (the composition is what
is being substituted, not the rule — the matrix file proves the rule) and which hands back a run
owned by a *different* principal than the command names. Only `owner=run.owner` passes.

**Cap per owner kind**: guest 40 per session (1.5, unchanged); user `MAX_EXPORT_JOBS_PER_USER_RUN`
= 20 **per run** (OQ-6) — never per user, so a long history can still export. Idempotency on
(run, document, format, version) is unchanged (ADR-0016 §2), including at the cap.

Green on arrival: the guest-cap regression guards (`…guest_at_forty…`, `…guest_below_forty…`).
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from tailorcraft.application.export.request_export import RequestExport, RequestExportCommand
from tailorcraft.application.tailoring.get_tailoring_run import GetTailoringRun
from tailorcraft.domain.export.errors import TooManyExportJobs
from tailorcraft.domain.export.events import ExportRequested
from tailorcraft.domain.export.value_objects import ExportFormat
from tailorcraft.domain.identity.ownership import GuestOwner, Owner, UserOwner
from tailorcraft.domain.tailoring.tailoring_run import TailoringRun
from tailorcraft.domain.tailoring.value_objects import TailoredDocumentKind, TailoringRunId
from tailorcraft.infrastructure.clock import FixedClock
from tests.integration.fakes import (
    FakeExportJobRepository,
    FakeGuestSessionRepository,
    FakeTailoringRunRepository,
    FakeUserRepository,
    RecordingEventPublisher,
    create_active_session,
)
from tests.integration.owners import queued_export, seed_user, succeeded_run

USER_EXPORTS_PER_RUN = 20  # MAX_EXPORT_JOBS_PER_USER_RUN (OQ-6)
GUEST_EXPORTS_PER_SESSION = 40


class _RunHandedOver(GetTailoringRun):
    """A composed `GetTailoringRun` that has already authorized and returns `run` — see the module
    docstring for why this substitution, and only this one, is made."""

    def __init__(self, run: TailoringRun) -> None:
        self._run = run

    async def __call__(self, run_id: TailoringRunId, requester: Owner) -> TailoringRun:
        return self._run


class _World:
    def __init__(self, clock: FixedClock) -> None:
        self.clock = clock
        self.sessions = FakeGuestSessionRepository()
        self.users = FakeUserRepository()
        self.runs = FakeTailoringRunRepository()
        self.jobs = FakeExportJobRepository()
        self.events = RecordingEventPublisher(self.jobs)
        self.earlier = clock.now() - timedelta(hours=1)

    def use_case(self, get_run: GetTailoringRun | None = None) -> RequestExport:
        return RequestExport(
            self.jobs,
            get_run or GetTailoringRun(self.runs, self.sessions, self.users, self.clock),
            self.events,
            self.clock,
        )

    async def user(self, email: str = "alex@example.com") -> UserOwner:
        return await seed_user(self.users, email, self.earlier)

    async def guest(self, label: str = "guest") -> GuestOwner:
        session = await create_active_session(
            self.sessions, self.clock, token_hash=label.ljust(64, "0")
        )
        return GuestOwner(session.id)

    async def run_of(self, owner: Owner) -> TailoringRun:
        run = succeeded_run(owner, self.earlier)
        await self.runs.add(run)
        return run

    async def fill(self, owner: Owner, run: TailoringRun, count: int) -> None:
        """`count` jobs on `run` under a key the tests never request (the cover letter as DOCX),
        so idempotency cannot short-circuit the cap."""
        for _ in range(count):
            await self.jobs.add(
                queued_export(
                    owner,
                    run,
                    self.earlier,
                    document=TailoredDocumentKind.COVER_LETTER,
                    format=ExportFormat.DOCX,
                )
            )

    def jobs_on(self, run: TailoringRun) -> int:
        return len([job for job in self.jobs.all() if job.tailoring_run_id == run.id])


def _cv_pdf(requester: Owner, run: TailoringRun) -> RequestExportCommand:
    return RequestExportCommand(
        requester=requester,
        tailoring_run_id=run.id,
        document=TailoredDocumentKind.CV,
        format=ExportFormat.PDF,
    )


# --- The owner comes from the run ------------------------------------------------------------------


async def test_a_users_export_job_is_owned_by_the_user(clock: FixedClock) -> None:
    w = _World(clock)
    user = await w.user()
    run = await w.run_of(user)

    result = await w.use_case()(_cv_pdf(UserOwner(user.user_id), run))

    assert result.created is True
    assert result.export_job.owner == run.owner == user
    published = [e for e in w.events.published if isinstance(e, ExportRequested)]
    assert [e.owner for e in published] == [user]


@pytest.mark.parametrize("kind", ["guest", "user"])
async def test_the_jobs_owner_is_read_from_the_run_not_from_the_command(
    kind: str, clock: FixedClock
) -> None:
    """AC-11: the command's requester and the run's owner are made to differ (see the module
    docstring); the job must carry the run's."""
    w = _World(clock)
    if kind == "guest":
        requester: Owner = await w.guest("asker")
        run_owner: Owner = await w.guest("run-owner")
    else:
        requester = await w.user("asker@example.com")
        run_owner = await w.user("run-owner@example.com")
    run = succeeded_run(run_owner, w.earlier)

    result = await w.use_case(_RunHandedOver(run))(_cv_pdf(requester, run))

    assert result.export_job.owner == run_owner
    assert result.export_job.owner != requester


# --- The user cap: 20 per run ----------------------------------------------------------------------


async def test_a_user_run_below_twenty_jobs_accepts_the_twentieth(clock: FixedClock) -> None:
    w = _World(clock)
    user = await w.user()
    run = await w.run_of(user)
    await w.fill(user, run, USER_EXPORTS_PER_RUN - 1)

    result = await w.use_case()(_cv_pdf(user, run))

    assert result.created is True
    assert w.jobs_on(run) == USER_EXPORTS_PER_RUN


async def test_a_user_run_at_twenty_jobs_is_refused_and_records_no_row(clock: FixedClock) -> None:
    """H-37: 409 `too_many_export_jobs`, no row."""
    w = _World(clock)
    user = await w.user()
    run = await w.run_of(user)
    await w.fill(user, run, USER_EXPORTS_PER_RUN)

    with pytest.raises(TooManyExportJobs) as exc_info:
        await w.use_case()(_cv_pdf(user, run))

    assert type(exc_info.value) is TooManyExportJobs
    assert w.jobs_on(run) == USER_EXPORTS_PER_RUN


async def test_the_user_cap_is_per_run_not_per_user(clock: FixedClock) -> None:
    """A user with many jobs on *other* runs — more than a guest's whole-session cap — still exports
    a fresh run."""
    w = _World(clock)
    user = await w.user()
    for _ in range(3):
        await w.fill(user, await w.run_of(user), USER_EXPORTS_PER_RUN)
    fresh = await w.run_of(user)

    result = await w.use_case()(_cv_pdf(user, fresh))

    assert result.created is True
    assert w.jobs_on(fresh) == 1


async def test_an_idempotent_re_request_at_the_user_cap_returns_the_existing_job(
    clock: FixedClock,
) -> None:
    """ADR-0016 §2 unchanged: the same (run, document, format, version) returns the job already
    there, before any cap is read."""
    w = _World(clock)
    user = await w.user()
    run = await w.run_of(user)
    existing = queued_export(user, run, w.earlier)  # CV as PDF, at run.version
    await w.jobs.add(existing)
    await w.fill(user, run, USER_EXPORTS_PER_RUN - 1)

    result = await w.use_case()(_cv_pdf(user, run))

    assert result.created is False
    assert result.export_job.id == existing.id
    assert w.jobs_on(run) == USER_EXPORTS_PER_RUN


# --- The guest cap: 40 per session, unchanged ------------------------------------------------------


async def test_a_guest_below_forty_jobs_accepts_the_fortieth(clock: FixedClock) -> None:
    w = _World(clock)
    guest = await w.guest()
    run = await w.run_of(guest)
    await w.fill(guest, run, GUEST_EXPORTS_PER_SESSION - 1)

    result = await w.use_case()(_cv_pdf(guest, run))

    assert result.created is True


async def test_a_guest_at_forty_jobs_is_refused(clock: FixedClock) -> None:
    """The guest cap counts the session, not the run: forty on one run refuses a second run too."""
    w = _World(clock)
    guest = await w.guest()
    await w.fill(guest, await w.run_of(guest), GUEST_EXPORTS_PER_SESSION)
    other_run = await w.run_of(guest)

    with pytest.raises(TooManyExportJobs):
        await w.use_case()(_cv_pdf(guest, other_run))

    assert w.jobs_on(other_run) == 0
