"""Persistence tests for `JobPosting` — the imperative mapping, its `TypeDecorator`s and
`SqlAlchemyJobPostingRepository` against real PostgreSQL (T29, written **after**).

Mirrors `test_base_cv_repository.py`'s structure: mapping round-trip with `session.expunge_all()` to
force a genuine reload, whole-second timestamp fidelity, `NULL` round-trips, repository scoping. Two
things are specific to this aggregate and have no `BaseCv` analogue:

- **Two source shapes**, not one — `from_pasted_text` and `from_fetched_url` populate `source_url`
  and `title` differently (J-2, J-4), so the round-trip test runs both and checks the columns that
  differ between them.
- **AC-21's check constraint**, tested with a raw `INSERT` (`session.execute(text(...))`) rather than
  through the aggregate, because `JobPosting` has exactly two named constructors and neither one can
  build the bad pairing the constraint exists to catch (feature-spec.md AC-21). Raw SQL is the only
  way to reach the state the database itself has to refuse.

Every assertion states what technical-plan.md's persistence section and `domain/posting/job_posting
.py`'s invariants (J-1…J-4) say should happen, never what a first run of the code produced.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tailorcraft.domain.identity.guest_session import GuestSession
from tailorcraft.domain.identity.value_objects import GuestSessionId
from tailorcraft.domain.posting.job_posting import JobPosting
from tailorcraft.domain.posting.value_objects import (
    FetchedPosting,
    JobPostingId,
    JobPostingText,
    PostingSource,
    PostingTitle,
    SourceUrl,
)
from tailorcraft.infrastructure.clock import FixedClock
from tailorcraft.infrastructure.persistence.mapping.identity.guest_session import (
    guest_session_table,
)

# `job_posting_table` is used below (the cascade test, matching `test_schema.py`'s pattern) — and
# importing this module is also what runs `mapper_registry.map_imperatively(JobPosting, ...)` as an
# import side effect. Without it `JobPosting._id` etc. do not exist yet and every repository import
# below fails at collection time with `AttributeError: type object 'JobPosting' has no attribute
# '_id'` — found the hard way when ruff's `--fix` removed this import as "unused" the first time
# nothing in this module referenced the table object directly.
from tailorcraft.infrastructure.persistence.mapping.posting.job_posting import job_posting_table
from tailorcraft.infrastructure.persistence.repositories.identity.guest_session import (
    SqlAlchemyGuestSessionRepository,
)
from tailorcraft.infrastructure.persistence.repositories.posting.job_posting import (
    SqlAlchemyJobPostingRepository,
)

# --- Test helpers --------------------------------------------------------------------------------


async def _persist_owner(
    session: AsyncSession, clock: FixedClock, *, token_hash: str
) -> GuestSession:
    """A `JobPosting` needs a real, persisted `GuestSession` to satisfy the `NOT NULL` FK
    (`posting_job_posting.guest_session_id`) — every test below builds one first."""
    repo = SqlAlchemyGuestSessionRepository(session)
    owner = GuestSession.start(
        id=repo.next_identity(), token_hash=token_hash, at=clock.now(), ttl_hours=24
    )
    await repo.add(owner)
    return owner


def _long_enough_text(marker: str = "x") -> JobPostingText:
    """150 non-whitespace characters — comfortably past the 100-character floor and comfortably
    under the 30,000-character ceiling, matching `test_capture_job_posting.py`'s helper."""
    return JobPostingText(marker * 150)


def _pasted(
    postings: SqlAlchemyJobPostingRepository, owner_id: GuestSessionId, clock: FixedClock
) -> JobPosting:
    return JobPosting.from_pasted_text(
        id=postings.next_identity(),
        guest_session_id=owner_id,
        text=_long_enough_text("p"),
        created_at=clock.now(),
    )


def _fetched(
    postings: SqlAlchemyJobPostingRepository,
    owner_id: GuestSessionId,
    clock: FixedClock,
    *,
    url: str = "https://jobs.example.com/postings/1234",
    title: str | None = "Senior Python Engineer",
) -> JobPosting:
    return JobPosting.from_fetched_url(
        id=postings.next_identity(),
        guest_session_id=owner_id,
        url=SourceUrl(url),
        fetched=FetchedPosting(
            text=_long_enough_text("f"),
            title=PostingTitle(title) if title is not None else None,
        ),
        created_at=clock.now(),
    )


# --- Mapping round-trip: every value object comes back as its own type, for BOTH source shapes ----


async def test_round_trip_of_a_pasted_posting_preserves_value_object_types(
    session: AsyncSession, clock: FixedClock
) -> None:
    owner = await _persist_owner(session, clock, token_hash="1" * 64)
    postings = SqlAlchemyJobPostingRepository(session)
    posting = _pasted(postings, owner.id, clock)
    await postings.add(posting)

    session.expunge_all()
    reloaded = await postings.get(posting.id)

    assert isinstance(reloaded.id, JobPostingId)
    assert reloaded.id == posting.id
    assert isinstance(reloaded.guest_session_id, GuestSessionId)
    assert reloaded.guest_session_id == owner.id
    assert reloaded.source is PostingSource.PASTED
    assert reloaded.source_url is None
    assert reloaded.title is None
    assert isinstance(reloaded.text, JobPostingText)
    assert reloaded.text == posting.text


async def test_round_trip_of_a_fetched_posting_preserves_value_object_types(
    session: AsyncSession, clock: FixedClock
) -> None:
    owner = await _persist_owner(session, clock, token_hash="2" * 64)
    postings = SqlAlchemyJobPostingRepository(session)
    posting = _fetched(
        postings,
        owner.id,
        clock,
        url="https://Jobs.Example.com/postings/5678",
        title="Staff Backend Engineer",
    )
    await postings.add(posting)

    session.expunge_all()
    reloaded = await postings.get(posting.id)

    assert isinstance(reloaded.id, JobPostingId)
    assert reloaded.id == posting.id
    assert isinstance(reloaded.guest_session_id, GuestSessionId)
    assert reloaded.guest_session_id == owner.id
    assert reloaded.source is PostingSource.FETCHED
    assert isinstance(reloaded.source_url, SourceUrl)
    assert reloaded.source_url == posting.source_url
    assert isinstance(reloaded.title, PostingTitle)
    assert reloaded.title == posting.title
    assert isinstance(reloaded.text, JobPostingText)
    assert reloaded.text == posting.text


# --- `TypeDecorator` NULL round-trips on a pasted posting (invariant J-2/J-4, both directions) -----


async def test_a_pasted_posting_round_trips_null_source_url_and_title(
    session: AsyncSession, clock: FixedClock
) -> None:
    """`SourceUrlType` and `PostingTitleType` must both round-trip `NULL` cleanly — a missing `None`
    guard in `process_result_value` would call `SourceUrl(None)` / `PostingTitle(None)` on a
    perfectly ordinary pasted row and raise instead of loading it."""
    owner = await _persist_owner(session, clock, token_hash="3" * 64)
    postings = SqlAlchemyJobPostingRepository(session)
    posting = _pasted(postings, owner.id, clock)
    await postings.add(posting)

    session.expunge_all()
    reloaded = await postings.get(posting.id)

    assert reloaded.source_url is None
    assert reloaded.title is None


async def test_a_fetched_posting_with_no_page_title_round_trips_null_title(
    session: AsyncSession, clock: FixedClock
) -> None:
    """A fetched posting's `title` is independently nullable (a page with no usable metadata title)
    — distinct from the pasted-posting case above, where `title` is null because `source != fetched`
    at all."""
    owner = await _persist_owner(session, clock, token_hash="4" * 64)
    postings = SqlAlchemyJobPostingRepository(session)
    posting = _fetched(postings, owner.id, clock, title=None)
    await postings.add(posting)

    session.expunge_all()
    reloaded = await postings.get(posting.id)

    assert reloaded.source is PostingSource.FETCHED
    assert reloaded.source_url is not None
    assert reloaded.title is None


# --- Whole-second timestamp fidelity (ADR-0007) -----------------------------------------------------


async def test_round_trip_preserves_whole_second_created_at(
    session: AsyncSession, clock: FixedClock
) -> None:
    owner = await _persist_owner(session, clock, token_hash="5" * 64)
    postings = SqlAlchemyJobPostingRepository(session)
    posting = _pasted(postings, owner.id, clock)
    await postings.add(posting)

    session.expunge_all()
    reloaded = await postings.get(posting.id)

    assert reloaded.created_at.microsecond == 0
    assert reloaded.created_at == posting.created_at


# --- AC-21: the check constraint rejects BOTH bad pairings, by raw SQL -----------------------------


async def test_check_constraint_rejects_a_fetched_row_with_no_source_url(
    session: AsyncSession, clock: FixedClock
) -> None:
    """`JobPosting.from_fetched_url` cannot omit a URL and `from_pasted_text` cannot set one — the
    aggregate makes this pairing unconstructable. Raw SQL is the only way to reach the state
    `ck_posting_job_posting_source_url_matches_source` exists to refuse: a hand-written `UPDATE`, a
    bad backfill, or a migration that populates the table from somewhere else."""
    owner = await _persist_owner(session, clock, token_hash="6" * 64)
    postings = SqlAlchemyJobPostingRepository(session)
    posting_id = postings.next_identity()

    with pytest.raises(IntegrityError, match="ck_posting_job_posting_source_url_matches_source"):
        await session.execute(
            text(
                "INSERT INTO posting_job_posting "
                "(id, guest_session_id, source, source_url, title, text, created_at) "
                "VALUES (:id, :guest_session_id, 'fetched', NULL, NULL, :text, :created_at)"
            ),
            {
                "id": posting_id.value,
                "guest_session_id": owner.id.value,
                "text": "x" * 150,
                "created_at": clock.now(),
            },
        )


async def test_check_constraint_rejects_a_pasted_row_carrying_a_source_url(
    session: AsyncSession, clock: FixedClock
) -> None:
    owner = await _persist_owner(session, clock, token_hash="7" * 64)
    postings = SqlAlchemyJobPostingRepository(session)
    posting_id = postings.next_identity()

    with pytest.raises(IntegrityError, match="ck_posting_job_posting_source_url_matches_source"):
        await session.execute(
            text(
                "INSERT INTO posting_job_posting "
                "(id, guest_session_id, source, source_url, title, text, created_at) "
                "VALUES (:id, :guest_session_id, 'pasted', :source_url, NULL, :text, :created_at)"
            ),
            {
                "id": posting_id.value,
                "guest_session_id": owner.id.value,
                "source_url": "https://jobs.example.com/postings/1",
                "text": "x" * 150,
                "created_at": clock.now(),
            },
        )


# --- AC-20: deleting a guest session cascades to its job postings ----------------------------------


async def test_deleting_a_guest_session_cascades_to_its_job_postings(
    session: AsyncSession, clock: FixedClock
) -> None:
    """This is what lets the 1.6 purge reach `posting_job_posting` with **no new predicate**: it
    stays `identity_guest_session.expires_at < now()` and nothing else. Two postings, one session,
    one delete — both rows must be gone in the same statement."""
    owner = await _persist_owner(session, clock, token_hash="8" * 64)
    postings = SqlAlchemyJobPostingRepository(session)
    first = _pasted(postings, owner.id, clock)
    await postings.add(first)
    second = _fetched(postings, owner.id, clock, url="https://jobs.example.com/postings/2")
    await postings.add(second)

    await session.execute(guest_session_table.delete().where(guest_session_table.c.id == owner.id))
    await session.flush()

    # Columns are `TypeDecorator`-backed (`JobPostingIdType`), so the bound parameter is the domain
    # value object the decorator's `process_bind_param` expects (`first.id`) — not the bare `UUID`
    # underneath it, the same lesson `test_schema.py`'s cascade test needed.
    remaining = await session.execute(
        select(job_posting_table.c.id).where(job_posting_table.c.id.in_([first.id, second.id]))
    )
    assert remaining.scalars().all() == []


# --- The index exists ------------------------------------------------------------------------------


async def test_guest_session_id_index_exists_on_job_posting(session: AsyncSession) -> None:
    """Serves `GET /api/job-postings`, `count_for_session`, and the cascade above."""
    result = await session.execute(
        text(
            "SELECT indexname FROM pg_indexes "
            "WHERE tablename = 'posting_job_posting' "
            "AND indexname = 'ix_posting_job_posting_guest_session_id'"
        )
    )
    assert result.scalar_one_or_none() == "ix_posting_job_posting_guest_session_id"


# --- Repository scoping ------------------------------------------------------------------------------


async def test_count_for_session_counts_only_that_sessions_rows(
    session: AsyncSession, clock: FixedClock
) -> None:
    owner_a = await _persist_owner(session, clock, token_hash="9" * 64)
    owner_b = await _persist_owner(session, clock, token_hash="0" * 64)
    postings = SqlAlchemyJobPostingRepository(session)

    await postings.add(_pasted(postings, owner_a.id, clock))
    await postings.add(_pasted(postings, owner_a.id, clock))
    await postings.add(_pasted(postings, owner_b.id, clock))

    assert await postings.count_for_session(owner_a.id) == 2
    assert await postings.count_for_session(owner_b.id) == 1


async def test_list_for_session_is_scoped_and_ordered_newest_first(
    session: AsyncSession, clock: FixedClock
) -> None:
    owner_a = await _persist_owner(session, clock, token_hash="a" * 64)
    owner_b = await _persist_owner(session, clock, token_hash="b" * 64)
    postings = SqlAlchemyJobPostingRepository(session)

    first = _pasted(postings, owner_a.id, clock)
    await postings.add(first)

    clock.advance(60)
    second = _fetched(postings, owner_a.id, clock, url="https://jobs.example.com/postings/second")
    await postings.add(second)

    # a different session's posting must never appear in owner_a's list, regardless of ordering
    await postings.add(_pasted(postings, owner_b.id, clock))

    result = await postings.list_for_session(owner_a.id)

    assert [item.id for item in result] == [second.id, first.id]


async def test_get_raises_not_found_for_an_unknown_id(session: AsyncSession) -> None:
    from tailorcraft.domain.posting.errors import JobPostingNotFound

    postings = SqlAlchemyJobPostingRepository(session)

    with pytest.raises(JobPostingNotFound):
        await postings.get(postings.next_identity())
