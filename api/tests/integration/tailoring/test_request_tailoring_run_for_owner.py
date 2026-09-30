"""Application tests for `RequestTailoringRun` with a `UserOwner` (slice 2.3, T10 RED, AC-9, H-14,
H-15, H-16, H-17).

The command's `owner` is the requester. The CV and the posting are authorized against **the same**
requester (the real `GetBaseCv` and `GetJobPosting` are composed, never faked, so every test here
exercises the actual equality). A **saved CV is used directly** — no working copy, no new CV row,
the run names the saved CV's own id. `EXTRACTED` is required. One active run **per owner**, and the
cap **per owner**: guest 20 per session (1.3, unchanged), user `MAX_TAILORING_RUNS_PER_USER` = 500
(OQ-6) — the constructor's default is the setting's default, and the cap tests use it on purpose.
**No row on any rejection** (ADR-0014 §2): every refusal is paired with the run count staying put
*and* with a discriminating positive elsewhere in the file, since a skeleton writes no row either.

"The file store is not touched" (AC-9) holds **by construction**: `RequestTailoringRun` has no
`FileStorePort` parameter, so there is nothing a double could record. What the test can observe is
the thing a copy would leave behind — a second CV row, or a run naming a different CV id — and that
is what `test_a_saved_cv_is_used_directly_without_a_copy` asserts.

Green on arrival: the two tests whose requester is a guest (per-owner isolation seen from the guest
side) — regression guards for the guest arm, which already keys on the session.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from uuid import uuid4

import pytest

from tailorcraft.application.intake.get_base_cv import GetBaseCv
from tailorcraft.application.posting.get_job_posting import GetJobPosting
from tailorcraft.application.tailoring.request_tailoring_run import (
    RequestTailoringRun,
    RequestTailoringRunCommand,
)
from tailorcraft.domain.identity.errors import UserNotFound
from tailorcraft.domain.identity.ownership import GuestOwner, Owner, UserOwner
from tailorcraft.domain.identity.value_objects import UserId
from tailorcraft.domain.intake.base_cv import BaseCv
from tailorcraft.domain.intake.errors import BaseCvNotFound, BaseCvNotOwnedByUser
from tailorcraft.domain.intake.value_objects import BaseCvId, BaseCvStatus
from tailorcraft.domain.posting.errors import JobPostingNotFound, JobPostingNotOwnedByUser
from tailorcraft.domain.posting.value_objects import JobPostingId
from tailorcraft.domain.tailoring.errors import (
    BaseCvNotReadyForTailoring,
    TailoringAlreadyRunning,
    TooManyTailoringRuns,
)
from tailorcraft.domain.tailoring.events import TailoringRunRequested
from tailorcraft.domain.tailoring.tailoring_run import TailoringRun
from tailorcraft.domain.tailoring.value_objects import TailoringRunStatus
from tailorcraft.infrastructure.clock import FixedClock
from tests.integration.fakes import (
    FakeBaseCvRepository,
    FakeGuestSessionRepository,
    FakeJobPostingRepository,
    FakeTailoringRunRepository,
    FakeUserRepository,
    RecordingEventPublisher,
    create_active_session,
)
from tests.integration.owners import (
    extracted_cv,
    extraction_failed_cv,
    failed_run,
    pasted_posting,
    queued_run,
    running_run,
    seed_user,
    uploaded_cv,
)

USER_RUN_CAP = 500  # MAX_TAILORING_RUNS_PER_USER (OQ-6) — hard-coded so a changed default goes red


class _World:
    def __init__(self, clock: FixedClock) -> None:
        self.clock = clock
        self.sessions = FakeGuestSessionRepository()
        self.users = FakeUserRepository()
        self.cvs = FakeBaseCvRepository()
        self.postings = FakeJobPostingRepository()
        self.runs = FakeTailoringRunRepository()
        self.events = RecordingEventPublisher(self.runs)
        self.earlier = clock.now() - timedelta(hours=1)

    def use_case(self) -> RequestTailoringRun:
        return RequestTailoringRun(
            self.runs,
            GetBaseCv(self.cvs, self.sessions, self.users, self.clock),
            GetJobPosting(self.postings, self.sessions, self.users, self.clock),
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

    async def inputs(self, owner: Owner) -> tuple[BaseCvId, JobPostingId]:
        cv = extracted_cv(owner, self.earlier)
        posting = pasted_posting(owner, self.earlier)
        await self.cvs.add(cv)
        await self.postings.add(posting)
        return cv.id, posting.id

    async def fill_with_terminal_runs(self, owner: Owner, count: int) -> None:
        for _ in range(count):
            await self.runs.add(failed_run(owner, self.earlier))

    def runs_of(self, owner: Owner) -> int:
        return len([run for run in self.runs.all() if run.owner == owner])


def _command(owner: Owner, cv_id: BaseCvId, posting_id: JobPostingId) -> RequestTailoringRunCommand:
    return RequestTailoringRunCommand(owner=owner, base_cv_id=cv_id, job_posting_id=posting_id)


# --- The happy path --------------------------------------------------------------------------------


async def test_a_user_request_records_a_queued_run_owned_by_the_user(clock: FixedClock) -> None:
    w = _World(clock)
    user = await w.user()
    cv_id, posting_id = await w.inputs(user)

    result = await w.use_case()(_command(user, cv_id, posting_id))

    assert result.status is TailoringRunStatus.QUEUED
    run = await w.runs.get(result.tailoring_run_id)
    assert run.owner == user
    assert run.base_cv_id == cv_id
    assert run.job_posting_id == posting_id
    assert run.requested_at == clock.now()
    published = [e for e in w.events.published if isinstance(e, TailoringRunRequested)]
    assert len(published) == 1
    assert published[0].owner == user
    assert w.events.repo_size_at_first_publish == 1  # saved before published


async def test_a_saved_cv_is_used_directly_without_a_copy(clock: FixedClock) -> None:
    """AC-9: no working copy. The run names the saved CV's own id and the CV table holds exactly the
    one row it held before — a copy would leave a second, guest- or user-owned, row behind."""
    w = _World(clock)
    user = await w.user()
    cv_id, posting_id = await w.inputs(user)

    result = await w.use_case()(_command(user, cv_id, posting_id))

    run = await w.runs.get(result.tailoring_run_id)
    assert run.base_cv_id == cv_id
    assert [cv.id for cv in w.cvs.all()] == [cv_id]
    assert (await w.cvs.get(cv_id)).owner == user


# --- H-14: inputs not the user's -------------------------------------------------------------------


@pytest.mark.parametrize("cv_owner_kind", ["guest", "other_user", "nonexistent"])
async def test_a_cv_that_is_not_the_users_is_not_found_and_records_no_run(
    cv_owner_kind: str, clock: FixedClock
) -> None:
    w = _World(clock)
    user = await w.user()
    _, posting_id = await w.inputs(user)
    if cv_owner_kind == "guest":
        foreign = extracted_cv(await w.guest(), w.earlier)
        await w.cvs.add(foreign)
        cv_id = foreign.id
    elif cv_owner_kind == "other_user":
        foreign = extracted_cv(await w.user("other@example.com"), w.earlier)
        await w.cvs.add(foreign)
        cv_id = foreign.id
    else:
        cv_id = extracted_cv(user, w.earlier).id  # built, never added

    with pytest.raises(BaseCvNotFound) as exc_info:
        await w.use_case()(_command(user, cv_id, posting_id))

    assert type(exc_info.value) is BaseCvNotFound
    if cv_owner_kind != "nonexistent":
        assert type(exc_info.value.__cause__) is BaseCvNotOwnedByUser
    assert w.runs.all() == []


@pytest.mark.parametrize("posting_owner_kind", ["guest", "other_user", "nonexistent"])
async def test_a_posting_that_is_not_the_users_is_not_found_and_records_no_run(
    posting_owner_kind: str, clock: FixedClock
) -> None:
    w = _World(clock)
    user = await w.user()
    cv_id, _ = await w.inputs(user)
    if posting_owner_kind == "guest":
        foreign = pasted_posting(await w.guest(), w.earlier)
        await w.postings.add(foreign)
        posting_id = foreign.id
    elif posting_owner_kind == "other_user":
        foreign = pasted_posting(await w.user("other@example.com"), w.earlier)
        await w.postings.add(foreign)
        posting_id = foreign.id
    else:
        posting_id = pasted_posting(user, w.earlier).id  # built, never added

    with pytest.raises(JobPostingNotFound) as exc_info:
        await w.use_case()(_command(user, cv_id, posting_id))

    assert type(exc_info.value) is JobPostingNotFound
    if posting_owner_kind != "nonexistent":
        assert type(exc_info.value.__cause__) is JobPostingNotOwnedByUser
    assert w.runs.all() == []


async def test_an_erased_user_is_refused_and_records_no_run(clock: FixedClock) -> None:
    """H-9 through this use case: the inputs are the (now erased) user's own, so only the
    resolution can refuse."""
    w = _World(clock)
    await w.user()  # someone else exists; the erased account does not
    erased = UserOwner(UserId(value=uuid4()))
    cv_id, posting_id = await w.inputs(erased)  # its rows linger (the fakes have no FK)

    with pytest.raises(UserNotFound) as exc_info:
        await w.use_case()(_command(erased, cv_id, posting_id))

    assert type(exc_info.value) is UserNotFound
    assert w.runs.all() == []


# --- H-15: the saved CV is not extracted -----------------------------------------------------------


@pytest.mark.parametrize(
    ("build", "status"),
    [
        pytest.param(uploaded_cv, BaseCvStatus.UPLOADED, id="uploaded"),
        pytest.param(extraction_failed_cv, BaseCvStatus.EXTRACTION_FAILED, id="extraction_failed"),
    ],
)
async def test_a_saved_cv_that_is_not_extracted_is_refused_and_records_no_run(
    build: Callable[[Owner, datetime], BaseCv], status: BaseCvStatus, clock: FixedClock
) -> None:
    w = _World(clock)
    user = await w.user()
    _, posting_id = await w.inputs(user)
    cv = build(user, w.earlier)
    await w.cvs.add(cv)

    with pytest.raises(BaseCvNotReadyForTailoring) as exc_info:
        await w.use_case()(_command(user, cv.id, posting_id))

    assert type(exc_info.value) is BaseCvNotReadyForTailoring
    assert w.runs_of(user) == 0
    assert cv.status is status


# --- H-16: one active run per owner ----------------------------------------------------------------


@pytest.mark.parametrize("active", [queued_run, running_run], ids=["queued", "running"])
async def test_a_users_active_run_refuses_a_second_carrying_its_id(
    active: Callable[[Owner, datetime], TailoringRun], clock: FixedClock
) -> None:
    w = _World(clock)
    user = await w.user()
    cv_id, posting_id = await w.inputs(user)
    in_flight = active(user, w.earlier)
    await w.runs.add(in_flight)

    with pytest.raises(TailoringAlreadyRunning) as exc_info:
        await w.use_case()(_command(user, cv_id, posting_id))

    assert type(exc_info.value) is TailoringAlreadyRunning
    assert exc_info.value.active_run_id == in_flight.id
    assert w.runs_of(user) == 1


@pytest.mark.parametrize("other_kind", ["guest", "other_user"])
async def test_someone_elses_active_run_does_not_block_a_user(
    other_kind: str, clock: FixedClock
) -> None:
    """Per owner: an active run belonging to a guest or to another user is not this user's."""
    w = _World(clock)
    user = await w.user()
    cv_id, posting_id = await w.inputs(user)
    other: Owner = await w.guest() if other_kind == "guest" else await w.user("b@example.com")
    await w.runs.add(running_run(other, w.earlier))

    result = await w.use_case()(_command(user, cv_id, posting_id))

    assert (await w.runs.get(result.tailoring_run_id)).owner == user


async def test_a_users_active_run_does_not_block_a_guest(clock: FixedClock) -> None:
    """The same isolation seen from the guest side — green on arrival (the guest arm keys on the
    session), kept as a regression guard for the switch to `find_active_for_owner`."""
    w = _World(clock)
    guest = await w.guest()
    cv_id, posting_id = await w.inputs(guest)
    await w.runs.add(running_run(await w.user(), w.earlier))

    result = await w.use_case()(_command(guest, cv_id, posting_id))

    assert (await w.runs.get(result.tailoring_run_id)).owner == guest


# --- H-17: the per-owner cap -----------------------------------------------------------------------


async def test_a_user_below_the_cap_may_request_the_five_hundredth_run(clock: FixedClock) -> None:
    w = _World(clock)
    user = await w.user()
    cv_id, posting_id = await w.inputs(user)
    await w.fill_with_terminal_runs(user, USER_RUN_CAP - 1)

    await w.use_case()(_command(user, cv_id, posting_id))

    assert w.runs_of(user) == USER_RUN_CAP


async def test_a_user_at_the_cap_is_refused_and_records_no_run(clock: FixedClock) -> None:
    w = _World(clock)
    user = await w.user()
    cv_id, posting_id = await w.inputs(user)
    await w.fill_with_terminal_runs(user, USER_RUN_CAP)

    with pytest.raises(TooManyTailoringRuns) as exc_info:
        await w.use_case()(_command(user, cv_id, posting_id))

    assert type(exc_info.value) is TooManyTailoringRuns
    assert exc_info.value.count == USER_RUN_CAP
    assert exc_info.value.limit == USER_RUN_CAP
    assert w.runs_of(user) == USER_RUN_CAP


async def test_another_users_runs_do_not_count_toward_a_users_cap(clock: FixedClock) -> None:
    w = _World(clock)
    user = await w.user()
    cv_id, posting_id = await w.inputs(user)
    await w.fill_with_terminal_runs(await w.user("b@example.com"), USER_RUN_CAP)

    await w.use_case()(_command(user, cv_id, posting_id))

    assert w.runs_of(user) == 1


async def test_a_users_runs_do_not_count_toward_a_guests_cap(clock: FixedClock) -> None:
    """Green on arrival (the guest arm counts the session's runs); a regression guard for the
    switch to `count_for_owner` — 500 user runs must not push a guest past 20."""
    w = _World(clock)
    guest = await w.guest()
    cv_id, posting_id = await w.inputs(guest)
    await w.fill_with_terminal_runs(await w.user(), 25)

    await w.use_case()(_command(guest, cv_id, posting_id))

    assert w.runs_of(guest) == 1
