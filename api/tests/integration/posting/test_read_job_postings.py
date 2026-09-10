"""Application tests for `GetJobPostingForSession` and `ListJobPostingsForSession` (T12, RED).

This is the slice's security test (P-30/AC-14, ADR-0008/ADR-0010): "owning a session id is not
authority over an object that references it." Both use cases check **the link** —
`posting.guest_session_id == the resolved session id` — on every read, and a caller must not be able
to tell "exists but belongs to someone else" from "does not exist at all", because a distinguishable
answer would confirm the id exists. See `application/posting/get_job_posting.py`'s docstring for the
exact contract this file tests against: `JobPostingNotFound` for both cases, chained from
`JobPostingNotOwnedBySession` (visible only on `__cause__`) in the "wrong owner" branch, and chained
from nothing at all (`__cause__ is None`) in the "never existed" branch — the two branches must look
identical to a caller and must still be distinguishable to this test, or nothing here would prove the
ownership check runs at all.

Same in-memory fakes as T9/T10 (`CaptureJobPosting`), imported from `tests/integration/fakes.py`
rather than redefined — see that module's docstring. Real Postgres is not used for the same reason
T9's module docstring gives: as of this commit there is no migration bringing `tailorcraft_test` to
head, so testing against the ports the use cases actually depend on is the honest red.

Every assertion below states what the use case **should** do per technical-plan.md's "Use cases"
paragraph and feature-spec.md's P-30/AC-14, never what the (currently `NotImplementedError`) code was
observed doing.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta
from uuid import uuid4

import pytest

from tailorcraft.application.posting.get_job_posting import GetJobPostingForSession
from tailorcraft.application.posting.list_job_postings import ListJobPostingsForSession
from tailorcraft.domain.identity.errors import GuestSessionExpired, GuestSessionNotFound
from tailorcraft.domain.identity.guest_session import GuestSession
from tailorcraft.domain.identity.value_objects import GuestSessionId
from tailorcraft.domain.posting.errors import JobPostingNotFound, JobPostingNotOwnedBySession
from tailorcraft.domain.posting.job_posting import JobPosting
from tailorcraft.domain.posting.value_objects import JobPostingId, JobPostingText
from tailorcraft.infrastructure.clock import FixedClock
from tests.integration.fakes import (
    FakeGuestSessionRepository,
    FakeJobPostingRepository,
    create_active_session,
)

# --- Test helpers --------------------------------------------------------------------------------


async def _add_job_posting(
    postings: FakeJobPostingRepository,
    session_id: GuestSessionId,
    clock: FixedClock,
    *,
    marker: str = "x",
) -> JobPosting:
    posting_id = postings.next_identity()
    posting = JobPosting.from_pasted_text(
        id=posting_id,
        guest_session_id=session_id,
        text=JobPostingText(marker * 150),
        created_at=clock.now(),
    )
    await postings.add(posting)
    return posting


# --- GetJobPostingForSession -------------------------------------------------------------------------


async def test_get_returns_the_posting_for_the_session_that_owns_it(clock: FixedClock) -> None:
    sessions = FakeGuestSessionRepository()
    session = await create_active_session(sessions, clock)
    postings = FakeJobPostingRepository()
    posting = await _add_job_posting(postings, session.id, clock)

    use_case = GetJobPostingForSession(postings, sessions, clock)
    result = await use_case(posting.id, session.id)

    assert result.id == posting.id
    assert result.guest_session_id == session.id


async def test_get_for_a_posting_owned_by_a_different_session_raises_job_posting_not_found(
    clock: FixedClock,
) -> None:
    """P-30/AC-14: the job posting exists, but belongs to a session other than the caller's. The use
    case must raise the same `JobPostingNotFound` a nonexistent id raises — see the sibling test
    below — never `JobPostingNotOwnedBySession` or a 403-shaped signal, which would confirm the id
    exists. The distinguishing fact survives only on `__cause__`, which only this test looks at."""
    sessions = FakeGuestSessionRepository()
    owner_session = await create_active_session(sessions, clock, token_hash="owner-session-hash-ab")
    other_session = await create_active_session(sessions, clock, token_hash="other-session-hash-cd")
    postings = FakeJobPostingRepository()
    posting = await _add_job_posting(postings, owner_session.id, clock)

    use_case = GetJobPostingForSession(postings, sessions, clock)

    with pytest.raises(JobPostingNotFound) as exc_info:
        await use_case(posting.id, other_session.id)

    assert isinstance(exc_info.value.__cause__, JobPostingNotOwnedBySession)


async def test_get_for_a_nonexistent_id_raises_job_posting_not_found_with_no_cause(
    clock: FixedClock,
) -> None:
    """The same exception type as the "wrong owner" case above — on purpose, a caller must not be
    able to distinguish "this id belongs to someone else" from "this id was never issued". But
    `__cause__` must be `None` here, in contrast with the previous test's
    `JobPostingNotOwnedBySession` — that contrast is what proves the two branches are internally
    distinguishable at all, rather than this test accidentally passing because nothing ever sets
    `__cause__` either way."""
    sessions = FakeGuestSessionRepository()
    session = await create_active_session(sessions, clock)
    postings = FakeJobPostingRepository()  # empty: no job posting was ever added

    use_case = GetJobPostingForSession(postings, sessions, clock)
    nonexistent_id = JobPostingId(value=uuid4())

    with pytest.raises(JobPostingNotFound) as exc_info:
        await use_case(nonexistent_id, session.id)

    assert exc_info.value.__cause__ is None


async def test_get_with_expired_session_raises_guest_session_expired(clock: FixedClock) -> None:
    sessions = FakeGuestSessionRepository()
    expired = GuestSession.start(
        id=sessions.next_identity(),
        token_hash="expired-session-hash-get".ljust(64, "0"),
        at=clock.now() - timedelta(hours=48),
        ttl_hours=24,
    )
    await sessions.add(expired)
    postings = FakeJobPostingRepository()
    posting = await _add_job_posting(postings, expired.id, clock)

    use_case = GetJobPostingForSession(postings, sessions, clock)

    with pytest.raises(GuestSessionExpired):
        await use_case(posting.id, expired.id)


async def test_get_with_unknown_session_raises_guest_session_not_found(clock: FixedClock) -> None:
    sessions = FakeGuestSessionRepository()  # empty: no session was ever added
    postings = FakeJobPostingRepository()

    use_case = GetJobPostingForSession(postings, sessions, clock)
    unknown_session_id = GuestSessionId(value=uuid4())
    some_posting_id = JobPostingId(value=uuid4())

    with pytest.raises(GuestSessionNotFound):
        await use_case(some_posting_id, unknown_session_id)


# --- ListJobPostingsForSession ------------------------------------------------------------------------


async def test_list_returns_only_this_sessions_postings(clock: FixedClock) -> None:
    sessions = FakeGuestSessionRepository()
    session_a = await create_active_session(
        sessions, clock, token_hash="session-a-hash".ljust(64, "0")
    )
    session_b = await create_active_session(
        sessions, clock, token_hash="session-b-hash".ljust(64, "0")
    )
    postings = FakeJobPostingRepository()
    posting_a1 = await _add_job_posting(postings, session_a.id, clock, marker="a")
    posting_a2 = await _add_job_posting(postings, session_a.id, clock, marker="b")
    posting_b1 = await _add_job_posting(postings, session_b.id, clock, marker="c")

    use_case = ListJobPostingsForSession(postings, sessions, clock)
    result = await use_case(session_a.id)

    result_ids = {posting.id for posting in result}
    assert result_ids == {posting_a1.id, posting_a2.id}
    # not merely a count check — session B's posting must be absent by name, not just outnumbered
    assert posting_b1.id not in result_ids


async def test_list_returns_empty_sequence_when_session_owns_none(clock: FixedClock) -> None:
    sessions = FakeGuestSessionRepository()
    session = await create_active_session(sessions, clock)
    postings = FakeJobPostingRepository()  # empty: this session owns nothing

    use_case = ListJobPostingsForSession(postings, sessions, clock)
    result = await use_case(session.id)

    assert isinstance(result, Sequence)
    assert result is not None
    assert len(result) == 0


async def test_list_with_expired_session_raises_guest_session_expired(clock: FixedClock) -> None:
    sessions = FakeGuestSessionRepository()
    expired = GuestSession.start(
        id=sessions.next_identity(),
        token_hash="expired-session-hash-list".ljust(64, "0"),
        at=clock.now() - timedelta(hours=48),
        ttl_hours=24,
    )
    await sessions.add(expired)
    postings = FakeJobPostingRepository()

    use_case = ListJobPostingsForSession(postings, sessions, clock)

    with pytest.raises(GuestSessionExpired):
        await use_case(expired.id)


async def test_list_with_unknown_session_raises_guest_session_not_found(clock: FixedClock) -> None:
    sessions = FakeGuestSessionRepository()  # empty: no session was ever added
    postings = FakeJobPostingRepository()

    use_case = ListJobPostingsForSession(postings, sessions, clock)
    unknown_session_id = GuestSessionId(value=uuid4())

    with pytest.raises(GuestSessionNotFound):
        await use_case(unknown_session_id)
