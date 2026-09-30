"""Application tests for `CaptureJobPosting` with a `UserOwner`, and for
`ListRecentJobPostingsForUser` (slice 2.3, T10 RED, AC-10, H-9, H-10).

`CaptureJobPosting`: the user is **resolved first** (before the cap is read and before a fetch goes
out); the cap is `MAX_JOB_POSTINGS_PER_USER` = 500 (the constructor's default, used on purpose, and
hard-coded below so a changed default goes red); paste and fetch arms are otherwise 1.2's, and a
failed fetch stores **no row** (ADR-0013). Every "no row / no fetch" absence is paired with a
positive: a skeleton neither fetches nor writes, so an absence alone proves nothing.

`ListRecentJobPostingsForUser(user_id, limit)`: resolves the user; returns only that user's postings,
newest first, at most `limit` — the account workspace's "recent postings" (AC-28's use-case half).

Green on arrival: `test_a_users_postings_do_not_count_toward_a_guests_cap` — the guest arm counts
the session's rows, kept as a regression guard for the switch to `count_for_owner`.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest

from tailorcraft.application.posting.capture_job_posting import (
    CaptureJobPosting,
    FetchJobPostingCommand,
    PasteJobPostingCommand,
)
from tailorcraft.application.posting.list_recent_job_postings import (
    ListRecentJobPostingsForUser,
)
from tailorcraft.domain.identity.errors import UserNotFound
from tailorcraft.domain.identity.ownership import GuestOwner, Owner, UserOwner
from tailorcraft.domain.identity.value_objects import UserId
from tailorcraft.domain.posting.errors import SourceUnreachable, TooManyJobPostings
from tailorcraft.domain.posting.events import JobPostingCaptured
from tailorcraft.domain.posting.value_objects import (
    FetchedPosting,
    JobPostingText,
    PostingSource,
    PostingTitle,
    SourceUrl,
)
from tailorcraft.infrastructure.clock import FixedClock
from tests.integration.fakes import (
    FakeGuestSessionRepository,
    FakeJobPostingFetcher,
    FakeJobPostingRepository,
    FakeUserRepository,
    RecordingEventPublisher,
    create_active_session,
)
from tests.integration.owners import pasted_posting, seed_user

USER_POSTING_CAP = 500  # MAX_JOB_POSTINGS_PER_USER (OQ-6)
_URL = SourceUrl("https://jobs.example.com/postings/1234")
_TEXT = JobPostingText("x" * 150)


class _World:
    def __init__(self, clock: FixedClock, fetcher: FakeJobPostingFetcher | None = None) -> None:
        self.clock = clock
        self.sessions = FakeGuestSessionRepository()
        self.users = FakeUserRepository()
        self.postings = FakeJobPostingRepository()
        self.events = RecordingEventPublisher(self.postings)
        self.fetcher = fetcher or FakeJobPostingFetcher(
            FetchedPosting(text=JobPostingText("f" * 150), title=PostingTitle("Staff Engineer"))
        )
        self.earlier = clock.now() - timedelta(hours=1)

    def capture(self) -> CaptureJobPosting:
        return CaptureJobPosting(
            self.postings, self.sessions, self.users, self.fetcher, self.events, self.clock
        )

    async def user(self, email: str = "alex@example.com") -> UserOwner:
        return await seed_user(self.users, email, self.earlier)

    async def fill(self, owner: Owner, count: int) -> None:
        for _ in range(count):
            await self.postings.add(pasted_posting(owner, self.earlier))

    def postings_of(self, owner: Owner) -> int:
        return len([p for p in self.postings.all() if p.owner == owner])


# --- CaptureJobPosting, user arm -------------------------------------------------------------------


async def test_a_pasted_posting_is_stored_for_the_user_and_published(clock: FixedClock) -> None:
    w = _World(clock)
    user = await w.user()

    result = await w.capture()(PasteJobPostingCommand(owner=user, text=_TEXT))

    stored = await w.postings.get(result.job_posting_id)
    assert stored.owner == user
    assert stored.source is PostingSource.PASTED
    assert stored.created_at == clock.now()
    captured = [e for e in w.events.published if isinstance(e, JobPostingCaptured)]
    assert [e.owner for e in captured] == [user]
    assert w.fetcher.calls == 0


async def test_a_fetched_posting_is_stored_for_the_user(clock: FixedClock) -> None:
    w = _World(clock)
    user = await w.user()

    result = await w.capture()(FetchJobPostingCommand(owner=user, url=_URL))

    stored = await w.postings.get(result.job_posting_id)
    assert stored.owner == user
    assert stored.source is PostingSource.FETCHED
    assert stored.source_url == _URL
    assert w.fetcher.calls == 1


async def test_an_erased_user_is_refused_before_any_fetch(clock: FixedClock) -> None:
    """H-9, and AC-10's "resolved first": the fetch arm is the one with an outbound side effect, so
    it is the arm that proves the order."""
    w = _World(clock)
    await w.user()  # another account exists
    erased = UserOwner(UserId(value=uuid4()))

    with pytest.raises(UserNotFound) as exc_info:
        await w.capture()(FetchJobPostingCommand(owner=erased, url=_URL))

    assert type(exc_info.value) is UserNotFound
    assert w.fetcher.calls == 0
    assert w.postings.all() == []


async def test_a_user_below_the_cap_may_store_the_five_hundredth_posting(clock: FixedClock) -> None:
    w = _World(clock)
    user = await w.user()
    await w.fill(user, USER_POSTING_CAP - 1)

    await w.capture()(PasteJobPostingCommand(owner=user, text=_TEXT))

    assert w.postings_of(user) == USER_POSTING_CAP


async def test_a_user_at_the_cap_is_refused_with_no_fetch_and_no_row(clock: FixedClock) -> None:
    """H-10: 409 `too_many_job_postings` — no fetch, no row."""
    w = _World(clock)
    user = await w.user()
    await w.fill(user, USER_POSTING_CAP)

    with pytest.raises(TooManyJobPostings) as exc_info:
        await w.capture()(FetchJobPostingCommand(owner=user, url=_URL))

    assert type(exc_info.value) is TooManyJobPostings
    assert w.fetcher.calls == 0
    assert w.postings_of(user) == USER_POSTING_CAP


async def test_another_users_postings_do_not_count_toward_a_users_cap(clock: FixedClock) -> None:
    w = _World(clock)
    user = await w.user()
    await w.fill(await w.user("b@example.com"), USER_POSTING_CAP)

    await w.capture()(PasteJobPostingCommand(owner=user, text=_TEXT))

    assert w.postings_of(user) == 1


async def test_a_users_postings_do_not_count_toward_a_guests_cap(clock: FixedClock) -> None:
    w = _World(clock)
    session = await create_active_session(w.sessions, clock)
    guest = GuestOwner(session.id)
    await w.fill(await w.user(), 20)  # twice the guest cap of 10

    await w.capture()(PasteJobPostingCommand(owner=guest, text=_TEXT))

    assert w.postings_of(guest) == 1


async def test_a_failed_fetch_for_a_user_stores_no_row(clock: FixedClock) -> None:
    """ADR-0013 through the user arm: the fetcher was reached (the positive), the failure
    propagates unchanged, and nothing was stored."""
    w = _World(clock, FakeJobPostingFetcher(SourceUnreachable()))
    user = await w.user()

    with pytest.raises(SourceUnreachable) as exc_info:
        await w.capture()(FetchJobPostingCommand(owner=user, url=_URL))

    assert type(exc_info.value) is SourceUnreachable
    assert w.fetcher.calls == 1
    assert w.postings.all() == []


# --- ListRecentJobPostingsForUser ------------------------------------------------------------------


async def test_recent_postings_are_the_users_own_newest_first_at_most_limit(
    clock: FixedClock,
) -> None:
    w = _World(clock)
    user = await w.user()
    other = await w.user("b@example.com")
    session = await create_active_session(w.sessions, clock)
    mine = [pasted_posting(user, clock.now() - timedelta(minutes=m)) for m in (30, 10, 20)]
    for posting in [
        *mine,
        pasted_posting(other, clock.now()),
        pasted_posting(GuestOwner(session.id), clock.now()),
    ]:
        await w.postings.add(posting)

    result = await ListRecentJobPostingsForUser(w.postings, w.users)(user.user_id, 2)

    newest_two = sorted(mine, key=lambda p: p.created_at, reverse=True)[:2]
    assert [p.id for p in result] == [p.id for p in newest_two]


async def test_recent_postings_for_a_user_with_none_is_empty(clock: FixedClock) -> None:
    w = _World(clock)
    user = await w.user()
    await w.fill(await w.user("b@example.com"), 3)

    result = await ListRecentJobPostingsForUser(w.postings, w.users)(user.user_id, 20)

    assert list(result) == []


async def test_recent_postings_for_an_erased_user_raise_user_not_found(clock: FixedClock) -> None:
    w = _World(clock)
    erased = UserOwner(UserId(value=uuid4()))
    await w.fill(erased, 2)  # its rows linger in the fake; only resolution can refuse

    with pytest.raises(UserNotFound) as exc_info:
        await ListRecentJobPostingsForUser(w.postings, w.users)(erased.user_id, 20)

    assert type(exc_info.value) is UserNotFound
