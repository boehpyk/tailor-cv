"""Persistence tests for the user owner on `JobPosting`, `TailoringRun` and `ExportJob` (slice 2.3,
T17, written after; AC-16's mapping half, H-53).

- **Round-trip, both owners, three tables**: written through the repository, the identity map
  emptied (`expunge_all`), read back — `owner` rebuilt as the same variant, the raw row holding
  exactly one owner column and `NULL` in the other, timestamps equal to the whole-second instants
  they were written with.
- **`add` translates both owner FKs** (the guest half since 2.4's AC-12): a `UserOwner` whose
  account is gone raises `UserNotFound` (the erasure race's shape, H-53); a `GuestOwner` whose
  session is gone raises `GuestSessionNotFound` — on all four owned tables. The refusal is contained in a SAVEPOINT: an
  aggregate the session had already loaded is not expired by it (the 1.4 lesson).
- **The owner-keyed reads**: `count_for_owner` isolated per owner and per variant;
  `find_active_for_owner` newest-first and tolerant of two active runs; `list_recent_for_user`
  ordered with same-second ties broken by id and bounded by `limit`; `count_for_run`.

The erasure race itself (an insert waiting on the user row's `FOR UPDATE`) is AC-37, at T25.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import timedelta
from uuid import UUID

import pytest
from sqlalchemy import Table, inspect, select
from sqlalchemy.ext.asyncio import AsyncSession

from tailorcraft.domain.export.value_objects import ExportFormat
from tailorcraft.domain.identity.errors import GuestSessionNotFound, UserNotFound
from tailorcraft.domain.identity.ownership import GuestOwner, Owner, UserOwner
from tailorcraft.domain.identity.value_objects import GuestSessionId, UserId
from tailorcraft.domain.posting.job_posting import JobPosting
from tailorcraft.domain.posting.value_objects import JobPostingText
from tailorcraft.domain.tailoring.tailoring_run import TailoringRun
from tailorcraft.domain.tailoring.value_objects import TailoringRunId, TailoringRunStatus
from tailorcraft.infrastructure.clock import FixedClock
from tailorcraft.infrastructure.identifiers import uuid7
from tailorcraft.infrastructure.persistence.mapping.export.export_job import export_job_table
from tailorcraft.infrastructure.persistence.mapping.intake.base_cv import base_cv_table
from tailorcraft.infrastructure.persistence.mapping.posting.job_posting import job_posting_table
from tailorcraft.infrastructure.persistence.mapping.tailoring.tailoring_run import (
    tailoring_run_table,
)
from tailorcraft.infrastructure.persistence.repositories.export.export_job import (
    SqlAlchemyExportJobRepository,
)
from tailorcraft.infrastructure.persistence.repositories.intake.base_cv import (
    SqlAlchemyBaseCvRepository,
)
from tailorcraft.infrastructure.persistence.repositories.posting.job_posting import (
    SqlAlchemyJobPostingRepository,
)
from tailorcraft.infrastructure.persistence.repositories.tailoring.tailoring_run import (
    SqlAlchemyTailoringRunRepository,
)
from tests.integration.owners import (
    failed_run,
    pasted_posting,
    queued_export,
    queued_run,
    running_run,
    succeeded_run,
    uploaded_cv,
)
from tests.integration.persistence.owner_rows import persist_guest, persist_user

_OWNER_KINDS = ["guest", "user"]


async def _owner(session: AsyncSession, clock: FixedClock, kind: str) -> Owner:
    return await (persist_guest if kind == "guest" else persist_user)(session, clock)


async def _persist_posting_for(session: AsyncSession, run: TailoringRun) -> None:
    """A real posting for `run.job_posting_id` — since 2.3 /verify (reviewer MINOR #1),
    `SqlAlchemyTailoringRunRepository.add` takes it `FOR KEY SHARE` and, through the request
    route's composition root, refuses a run whose posting does not exist."""
    await SqlAlchemyJobPostingRepository(session).add(
        JobPosting.from_pasted_text(
            id=run.job_posting_id,
            owner=run.owner,
            text=JobPostingText("posting " * 40),
            created_at=run.requested_at,
        )
    )


async def _raw_owner_columns(
    session: AsyncSession, table: Table, row_id: object
) -> tuple[object, object]:
    row = (
        await session.execute(
            select(table.c.guest_session_id, table.c.user_id).where(table.c.id == row_id)
        )
    ).one()
    return row.guest_session_id, row.user_id


def _expected_columns(owner: Owner) -> tuple[object, object]:
    if isinstance(owner, GuestOwner):
        return owner.guest_session_id, None
    return None, owner.user_id


# --- Round-trip, both owners ---------------------------------------------------------------------


@pytest.mark.parametrize("kind", _OWNER_KINDS)
async def test_a_job_posting_round_trips_its_owner(
    session: AsyncSession, clock: FixedClock, kind: str
) -> None:
    owner = await _owner(session, clock, kind)
    posting = pasted_posting(owner, clock.now())
    repo = SqlAlchemyJobPostingRepository(session)
    await repo.add(posting)
    await session.flush()
    session.expunge_all()

    loaded = await repo.get(posting.id)

    assert loaded is not posting
    assert loaded.owner == owner
    assert loaded.created_at == clock.now()
    assert await _raw_owner_columns(session, job_posting_table, posting.id) == _expected_columns(
        owner
    )


@pytest.mark.parametrize("kind", _OWNER_KINDS)
async def test_a_tailoring_run_round_trips_its_owner(
    session: AsyncSession, clock: FixedClock, kind: str
) -> None:
    owner = await _owner(session, clock, kind)
    run = succeeded_run(owner, clock.now())
    repo = SqlAlchemyTailoringRunRepository(session)
    await _persist_posting_for(session, run)
    await repo.add(run)
    await session.flush()
    session.expunge_all()

    loaded = await repo.get(run.id)

    assert loaded is not run
    assert loaded.owner == owner
    assert loaded.requested_at == clock.now()
    assert loaded.completed_at == clock.now()
    assert await _raw_owner_columns(session, tailoring_run_table, run.id) == _expected_columns(
        owner
    )


@pytest.mark.parametrize("kind", _OWNER_KINDS)
async def test_an_export_job_round_trips_its_owner(
    session: AsyncSession, clock: FixedClock, kind: str
) -> None:
    owner = await _owner(session, clock, kind)
    job = queued_export(owner, succeeded_run(owner, clock.now()), clock.now())
    repo = SqlAlchemyExportJobRepository(session)
    await repo.add(job)
    await session.flush()
    session.expunge_all()

    loaded = await repo.get(job.id)

    assert loaded is not job
    assert loaded.owner == owner
    assert loaded.requested_at == clock.now()
    assert await _raw_owner_columns(session, export_job_table, job.id) == _expected_columns(owner)


# --- add: the user FK is translated, the guest FK is not -----------------------------------------


def _gone_user() -> UserOwner:
    return UserOwner(UserId(uuid7()))


def _gone_guest() -> GuestOwner:
    return GuestOwner(GuestSessionId(uuid7()))


async def _add_posting(session: AsyncSession, owner: Owner, clock: FixedClock) -> None:
    await SqlAlchemyJobPostingRepository(session).add(pasted_posting(owner, clock.now()))


async def _add_run(session: AsyncSession, owner: Owner, clock: FixedClock) -> None:
    await SqlAlchemyTailoringRunRepository(session).add(queued_run(owner, clock.now()))


async def _add_cv(session: AsyncSession, owner: Owner, clock: FixedClock) -> None:
    await SqlAlchemyBaseCvRepository(session).add(uploaded_cv(owner, clock.now()))


async def _add_job(session: AsyncSession, owner: Owner, clock: FixedClock) -> None:
    run = succeeded_run(owner, clock.now())
    await SqlAlchemyExportJobRepository(session).add(queued_export(owner, run, clock.now()))


_Adder = Callable[[AsyncSession, Owner, FixedClock], Awaitable[None]]
_MAPPED: dict[str, Table] = {
    "posting_job_posting": job_posting_table,
    "tailoring_run": tailoring_run_table,
    "export_job": export_job_table,
    "intake_base_cv": base_cv_table,
}
_ADDERS = [
    pytest.param(_add_posting, "posting_job_posting", id="posting"),
    pytest.param(_add_run, "tailoring_run", id="run"),
    pytest.param(_add_job, "export_job", id="export"),
]
# AC-12 names four guest adders; the user-FK test above stays on the three it was written for.
_GUEST_ADDERS = [
    pytest.param(_add_cv, "intake_base_cv", id="base_cv"),
    *_ADDERS,
]


@pytest.mark.parametrize(("add", "table"), _ADDERS)
async def test_add_for_an_erased_user_raises_user_not_found(
    session: AsyncSession, clock: FixedClock, add: _Adder, table: str
) -> None:
    gone = _gone_user()
    with pytest.raises(UserNotFound) as exc_info:
        await add(session, gone, clock)

    assert type(exc_info.value) is UserNotFound
    assert exc_info.value.__cause__ is None
    mapped = _MAPPED[table]
    landed = await session.execute(select(mapped.c.id).where(mapped.c.user_id == gone.user_id))
    assert landed.all() == []


async def _attempt(
    add: _Adder, session: AsyncSession, owner: Owner, clock: FixedClock
) -> BaseException | None:
    """Run the add and hand back what escaped it, so the assertion — not the harness — is what
    fails when the wrong thing (or nothing) is raised."""
    try:
        await add(session, owner, clock)
    except Exception as exc:  # inspect whichever one escaped
        return exc
    return None


@pytest.mark.parametrize(("add", "table"), _GUEST_ADDERS)
async def test_add_for_a_vanished_guest_session_raises_guest_session_not_found(
    session: AsyncSession, clock: FixedClock, add: _Adder, table: str
) -> None:
    """AC-12 (slice 2.4, ADR-0025's guest-write race, AC-17): the guest FK is now translated, as the
    user FK always was. **This deliberately reverses 2.3's
    `test_add_for_a_vanished_guest_session_is_not_translated`**, which asserted the untranslated
    `IntegrityError`; AC-12 changes that meaning on purpose, because a purge can now delete a
    session between a guest write's resolve and its insert, and the route must answer the same
    401 it gives a vanished session — not a 500.

    "Any other integrity error is unchanged" is carried by the sibling erased-user test above (the
    user FK still translates to `UserNotFound`, not to this) and by the existing untranslated
    refusals (`uq_intake_base_cv_file_key`, CHECKs) in `test_base_cv_repository.py`."""
    gone = _gone_guest()

    escaped = await _attempt(add, session, gone, clock)

    assert type(escaped) is GuestSessionNotFound, f"escaped: {escaped!r}"
    assert escaped.__cause__ is None
    mapped = _MAPPED[table]
    landed = await session.execute(
        select(mapped.c.id).where(mapped.c.guest_session_id == gone.guest_session_id)
    )
    assert landed.all() == []


async def test_a_refused_add_does_not_expire_what_the_session_already_loaded(
    session: AsyncSession, clock: FixedClock
) -> None:
    """`RequestExport`'s shape: the run is loaded, then the job's `add` is refused on the user FK.
    The SAVEPOINT keeps the rollback to the pending job; the loaded run is still readable without
    a lazy load (which, on an `AsyncSession`, would be `MissingGreenlet`).

    **Observed red, 2026-09-28**, with `SqlAlchemyExportJobRepository.add`'s `async with
    self._session.begin_nested():` replaced by `if True:` (restored byte-exact after):
    `AssertionError: assert {'_base_cv_id', …, '_status', '_tailored_cv', …} == set()` — every
    attribute of the loaded run expired by the refused flush."""
    user = await persist_user(session, clock)
    runs = SqlAlchemyTailoringRunRepository(session)
    run = succeeded_run(user, clock.now())
    await _persist_posting_for(session, run)
    await runs.add(run)
    await session.flush()
    session.expunge_all()
    loaded = await runs.get(run.id)

    with pytest.raises(UserNotFound):
        await SqlAlchemyExportJobRepository(session).add(
            queued_export(_gone_user(), loaded, clock.now())
        )

    state = inspect(loaded)
    assert state is not None
    assert state.expired_attributes == set()
    assert loaded.status is TailoringRunStatus.SUCCEEDED
    assert loaded.owner == user


# --- count_for_owner --------------------------------------------------------------------------------


async def test_run_counts_are_isolated_per_owner_and_per_variant(
    session: AsyncSession, clock: FixedClock
) -> None:
    guest = await persist_guest(session, clock)
    user_a = await persist_user(session, clock)
    user_b = await persist_user(session, clock)
    runs = SqlAlchemyTailoringRunRepository(session)
    for owner, count in ((guest, 2), (user_a, 3), (user_b, 1)):
        for _ in range(count):
            run = failed_run(owner, clock.now())
            await _persist_posting_for(session, run)
            await runs.add(run)
    await session.flush()

    assert await runs.count_for_owner(guest) == 2
    assert await runs.count_for_owner(user_a) == 3
    assert await runs.count_for_owner(user_b) == 1
    assert await runs.count_for_owner(await persist_user(session, clock)) == 0


async def test_posting_counts_are_isolated_per_owner_and_per_variant(
    session: AsyncSession, clock: FixedClock
) -> None:
    guest = await persist_guest(session, clock)
    user_a = await persist_user(session, clock)
    user_b = await persist_user(session, clock)
    postings = SqlAlchemyJobPostingRepository(session)
    for owner, count in ((guest, 1), (user_a, 3), (user_b, 2)):
        for _ in range(count):
            await postings.add(pasted_posting(owner, clock.now()))
    await session.flush()

    assert await postings.count_for_owner(guest) == 1
    assert await postings.count_for_owner(user_a) == 3
    assert await postings.count_for_owner(user_b) == 2


# --- find_active_for_owner ---------------------------------------------------------------------------


async def test_find_active_for_a_user_returns_the_newest_active_run_without_raising(
    session: AsyncSession, clock: FixedClock
) -> None:
    """Two active runs are an accepted race (ADR-0014 §4): the newest comes back, never an error.
    Terminal runs and another owner's active run are ignored."""
    user = await persist_user(session, clock)
    other = await persist_user(session, clock)
    runs = SqlAlchemyTailoringRunRepository(session)
    older = queued_run(user, clock.now() - timedelta(minutes=5))
    newer = running_run(user, clock.now() - timedelta(minutes=1))
    for run in (
        older,
        newer,
        succeeded_run(user, clock.now()),
        queued_run(other, clock.now()),
    ):
        await _persist_posting_for(session, run)
        await runs.add(run)
    await session.flush()

    found = await runs.find_active_for_owner(user)

    assert found is not None
    assert found.id == newer.id


async def test_find_active_breaks_a_same_second_tie_by_id(
    session: AsyncSession, clock: FixedClock
) -> None:
    user = await persist_user(session, clock)
    runs = SqlAlchemyTailoringRunRepository(session)
    low = queued_run(user, clock.now(), run_id=TailoringRunId(UUID(int=1)))
    high = queued_run(user, clock.now(), run_id=TailoringRunId(UUID(int=2**127)))
    await _persist_posting_for(session, low)
    await _persist_posting_for(session, high)
    await runs.add(low)
    await runs.add(high)
    await session.flush()

    found = await runs.find_active_for_owner(user)

    assert found is not None
    assert found.id == high.id


async def test_find_active_for_a_user_with_only_terminal_runs_is_none(
    session: AsyncSession, clock: FixedClock
) -> None:
    user = await persist_user(session, clock)
    runs = SqlAlchemyTailoringRunRepository(session)
    terminal = failed_run(user, clock.now())
    await _persist_posting_for(session, terminal)
    await runs.add(terminal)
    guest_active = queued_run(await persist_guest(session, clock), clock.now())
    await _persist_posting_for(session, guest_active)
    await runs.add(guest_active)
    await session.flush()

    assert await runs.find_active_for_owner(user) is None


# --- list_recent_for_user ------------------------------------------------------------------------------


async def test_list_recent_for_user_is_newest_first_ties_by_id_and_bounded(
    session: AsyncSession, clock: FixedClock
) -> None:
    user = await persist_user(session, clock)
    postings = SqlAlchemyJobPostingRepository(session)
    same_second = [pasted_posting(user, clock.now()) for _ in range(3)]
    older = pasted_posting(user, clock.now() - timedelta(minutes=1))
    for posting in [
        *same_second,
        older,
        pasted_posting(await persist_user(session, clock), clock.now()),
    ]:
        await postings.add(posting)
    await postings.add(pasted_posting(await persist_guest(session, clock), clock.now()))
    await session.flush()

    top_two = await postings.list_recent_for_user(user.user_id, 2)
    everything = await postings.list_recent_for_user(user.user_id, 20)

    by_id_desc = sorted(same_second, key=lambda p: p.id.value, reverse=True)
    assert [p.id for p in top_two] == [p.id for p in by_id_desc[:2]]
    assert [p.id for p in everything] == [*(p.id for p in by_id_desc), older.id]


async def test_list_recent_for_a_user_without_postings_is_empty(
    session: AsyncSession, clock: FixedClock
) -> None:
    user = await persist_user(session, clock)
    await SqlAlchemyJobPostingRepository(session).add(
        pasted_posting(await persist_user(session, clock), clock.now())
    )
    await session.flush()

    assert (
        list(await SqlAlchemyJobPostingRepository(session).list_recent_for_user(user.user_id, 20))
        == []
    )


# --- count_for_run -------------------------------------------------------------------------------------


async def test_count_for_run_counts_that_runs_jobs_only(
    session: AsyncSession, clock: FixedClock
) -> None:
    user = await persist_user(session, clock)
    run_a = succeeded_run(user, clock.now())
    run_b = succeeded_run(user, clock.now())
    jobs = SqlAlchemyExportJobRepository(session)
    for run, formats in (
        (run_a, (ExportFormat.PDF, ExportFormat.DOCX, ExportFormat.PDF)),
        (run_b, (ExportFormat.PDF,)),
    ):
        for export_format in formats:
            await jobs.add(queued_export(user, run, clock.now(), format=export_format))
    await session.flush()

    assert await jobs.count_for_run(run_a.id) == 3
    assert await jobs.count_for_run(run_b.id) == 1
    assert await jobs.count_for_run(TailoringRunId(uuid7())) == 0
