"""Application tests for `ListTailoringHistory` (slice 2.3, T10 RED, AC-13).

`ListTailoringHistory(user_id, after, size)` resolves the user (`UserNotFound`) and returns one
keyset page of **only** `UserOwner(user_id)`'s runs, newest first by `(requested_at DESC, id DESC)`;
`next_cursor` is the last entry's key when — and only when — a further entry exists; two runs in the
same whole second are ordered by id and are neither skipped nor repeated across a page boundary.

**What this file can and cannot prove.** The ordering and the keyset arithmetic are the query's, and
the query here is `InMemoryTailoringHistoryQuery` — a faithful re-statement of technical plan §0.5's
SQL, not the SQL. The SQL is T15's and is proven against Postgres by T17 (AC-55/AC-56). What these
tests do prove about the *use case* is that it resolves the user before reading, asks for that user
and nobody else, and passes `after` and `size` through untouched: walking pages would repeat or skip
entries if either were dropped or rewritten, which is why the walk tests are the discriminating ones.
They double as the executable contract T17 can replay against the adapter.

Every test here is red on the skeleton's `NotImplementedError` body; none is green on arrival.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID, uuid4

import pytest

from tailorcraft.application.tailoring.list_tailoring_history import ListTailoringHistory
from tailorcraft.domain.identity.errors import UserNotFound
from tailorcraft.domain.identity.ownership import GuestOwner, UserOwner
from tailorcraft.domain.identity.value_objects import GuestSessionId, UserId
from tailorcraft.domain.tailoring.history import HistoryCursor, HistoryPage, HistoryPageSize
from tailorcraft.domain.tailoring.value_objects import TailoringRunId
from tailorcraft.infrastructure.clock import FixedClock
from tests.integration.fakes import (
    FakeBaseCvRepository,
    FakeJobPostingRepository,
    FakeTailoringRunRepository,
    FakeUserRepository,
    InMemoryTailoringHistoryQuery,
)
from tests.integration.owners import extracted_cv, pasted_posting, seed_user, succeeded_run


class _World:
    def __init__(self, clock: FixedClock) -> None:
        self.clock = clock
        self.users = FakeUserRepository()
        self.runs = FakeTailoringRunRepository()
        self.cvs = FakeBaseCvRepository()
        self.postings = FakeJobPostingRepository()
        self.history = InMemoryTailoringHistoryQuery(self.runs, self.cvs, self.postings)

    def use_case(self) -> ListTailoringHistory:
        return ListTailoringHistory(self.history, self.users)

    async def user(self, email: str = "alex@example.com") -> UserOwner:
        return await seed_user(self.users, email, self.clock.now() - timedelta(days=1))

    async def run_at(
        self, owner: UserOwner | GuestOwner, at: datetime, run_id: UUID | None = None
    ) -> TailoringRunId:
        run = succeeded_run(
            owner, at, run_id=TailoringRunId(value=run_id) if run_id is not None else None
        )
        await self.runs.add(run)
        return run.id


def _ids(page: HistoryPage) -> list[TailoringRunId]:
    return [entry.tailoring_run_id for entry in page.entries]


async def _walk(w: _World, user: UserOwner, size: int) -> list[HistoryPage]:
    pages: list[HistoryPage] = []
    after: HistoryCursor | None = None
    while True:
        page = await w.use_case()(user.user_id, after, HistoryPageSize(size))
        pages.append(page)
        if page.next_cursor is None:
            return pages
        after = page.next_cursor
        assert len(pages) < 50, "the walk never ended — a cursor that does not advance"


async def test_an_erased_user_raises_user_not_found_before_the_query_runs(
    clock: FixedClock,
) -> None:
    w = _World(clock)
    erased = UserOwner(UserId(value=uuid4()))
    await w.run_at(erased, clock.now())  # its rows linger; only resolution can refuse

    with pytest.raises(UserNotFound) as exc_info:
        await w.use_case()(erased.user_id, None, HistoryPageSize(20))

    assert type(exc_info.value) is UserNotFound
    assert w.history.calls == []


async def test_a_user_with_no_runs_gets_an_empty_page_without_a_cursor(clock: FixedClock) -> None:
    w = _World(clock)
    user = await w.user()
    await w.run_at(await w.user("b@example.com"), clock.now())

    page = await w.use_case()(user.user_id, None, HistoryPageSize(20))

    assert page == HistoryPage(entries=(), next_cursor=None)
    assert w.history.calls == [(user.user_id, None, HistoryPageSize(20))]


async def test_only_the_users_own_runs_are_listed(clock: FixedClock) -> None:
    w = _World(clock)
    user = await w.user()
    mine = {await w.run_at(user, clock.now() - timedelta(minutes=m)) for m in (1, 2, 3)}
    await w.run_at(await w.user("b@example.com"), clock.now())
    await w.run_at(GuestOwner(GuestSessionId(value=uuid4())), clock.now())

    page = await w.use_case()(user.user_id, None, HistoryPageSize(50))

    assert set(_ids(page)) == mine
    assert len(page.entries) == 3


async def test_runs_are_listed_newest_first(clock: FixedClock) -> None:
    w = _World(clock)
    user = await w.user()
    oldest = await w.run_at(user, clock.now() - timedelta(hours=3))
    newest = await w.run_at(user, clock.now() - timedelta(hours=1))
    middle = await w.run_at(user, clock.now() - timedelta(hours=2))

    page = await w.use_case()(user.user_id, None, HistoryPageSize(20))

    assert _ids(page) == [newest, middle, oldest]


async def test_runs_in_the_same_second_are_ordered_by_id_descending(clock: FixedClock) -> None:
    w = _World(clock)
    user = await w.user()
    same_second = clock.now() - timedelta(minutes=5)
    low = UUID("00000000-0000-7000-8000-000000000001")
    high = UUID("ffffffff-ffff-7fff-bfff-ffffffffffff")
    await w.run_at(user, same_second, low)
    await w.run_at(user, same_second, high)

    page = await w.use_case()(user.user_id, None, HistoryPageSize(20))

    assert [entry_id.value for entry_id in _ids(page)] == [high, low]


async def test_a_full_page_with_more_behind_it_carries_the_last_entrys_key(
    clock: FixedClock,
) -> None:
    w = _World(clock)
    user = await w.user()
    for minutes in range(5):
        await w.run_at(user, clock.now() - timedelta(minutes=minutes))

    page = await w.use_case()(user.user_id, None, HistoryPageSize(3))

    assert len(page.entries) == 3
    last = page.entries[-1]
    assert page.next_cursor == HistoryCursor(
        requested_at=last.requested_at, tailoring_run_id=last.tailoring_run_id
    )


async def test_a_page_that_exactly_exhausts_the_history_has_no_cursor(clock: FixedClock) -> None:
    """ "When, and only when, a further entry exists": exactly `size` entries left is not a reason for
    a cursor that would lead to an empty page."""
    w = _World(clock)
    user = await w.user()
    for minutes in range(3):
        await w.run_at(user, clock.now() - timedelta(minutes=minutes))

    page = await w.use_case()(user.user_id, None, HistoryPageSize(3))

    assert len(page.entries) == 3
    assert page.next_cursor is None


async def test_walking_pages_visits_every_run_once_in_order(clock: FixedClock) -> None:
    w = _World(clock)
    user = await w.user()
    created = [await w.run_at(user, clock.now() - timedelta(minutes=m)) for m in range(7)]

    pages = await _walk(w, user, size=3)

    assert [len(page.entries) for page in pages] == [3, 3, 1]
    assert [run_id for page in pages for run_id in _ids(page)] == created  # m=0 is newest


async def test_a_page_boundary_between_same_second_runs_neither_skips_nor_repeats(
    clock: FixedClock,
) -> None:
    """The tie AC-13 names: five runs in one whole second, pages of two. Every boundary falls
    between two runs with equal `requested_at`, so only the id half of the key keeps the walk
    honest."""
    w = _World(clock)
    user = await w.user()
    same_second = clock.now() - timedelta(minutes=5)
    created = [await w.run_at(user, same_second) for _ in range(5)]

    pages = await _walk(w, user, size=2)

    walked = [run_id for page in pages for run_id in _ids(page)]
    assert walked == sorted(created, key=lambda run_id: run_id.value, reverse=True)
    assert len(set(walked)) == 5


async def test_the_cursor_and_size_reach_the_query_unchanged(clock: FixedClock) -> None:
    w = _World(clock)
    user = await w.user()
    cursor = HistoryCursor(requested_at=clock.now(), tailoring_run_id=TailoringRunId(uuid4()))

    await w.use_case()(user.user_id, cursor, HistoryPageSize(7))

    assert w.history.calls == [(user.user_id, cursor, HistoryPageSize(7))]


async def test_an_entry_whose_saved_cv_was_deleted_says_so(clock: FixedClock) -> None:
    """§0.4(a) as history shows it: the run keeps its `base_cv_id`, and `base_cv` is `None`
    ("CV deleted"). The sibling entry, whose CV still exists, is the discriminating positive."""
    w = _World(clock)
    user = await w.user()
    earlier = clock.now() - timedelta(hours=1)
    kept_cv, gone_cv = extracted_cv(user, earlier), extracted_cv(user, earlier)
    posting = pasted_posting(user, earlier)
    for cv in (kept_cv, gone_cv):
        await w.cvs.add(cv)
    await w.postings.add(posting)
    kept = succeeded_run(user, earlier, base_cv_id=kept_cv.id, job_posting_id=posting.id)
    orphaned = succeeded_run(
        user, earlier - timedelta(minutes=1), base_cv_id=gone_cv.id, job_posting_id=posting.id
    )
    await w.runs.add(kept)
    await w.runs.add(orphaned)
    await w.cvs.remove(gone_cv.id, user)

    page = await w.use_case()(user.user_id, None, HistoryPageSize(20))

    by_id = {entry.tailoring_run_id: entry for entry in page.entries}
    assert by_id[kept.id].base_cv is not None
    assert by_id[kept.id].base_cv.base_cv_id == kept_cv.id  # type: ignore[union-attr]
    assert by_id[orphaned.id].base_cv is None
    assert by_id[orphaned.id].base_cv_id == gone_cv.id
