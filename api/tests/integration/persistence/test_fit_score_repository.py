"""`SqlAlchemyFitScoreRepository` and the `scoring_fit_score` table against a real Postgres (slice 5.1,
T20, written after the code: the mapping, the constraints and the statements are discovered against
the library). The two-tab race, the ledger and the erasure interleavings are on real, independent
connections in `test_fit_score_races.py` and `test_spend_ledger.py`.

Each test rolls back. A refusal inside `store` is a SAVEPOINT, so the outer transaction survives it,
and the tests that say "the transaction survives" prove it by writing again afterwards.

What is pinned, and from where (the spec's words, never the adapter's):

- **Round trip and whole seconds** (ADR-0007): every field comes back from a fresh load, and the two
  new timestamp columns are `TIMESTAMP(0)` in the catalogue.
- **Identity is per owner** (ADR-0040): two plain unique constraints, one per owner column, NULLs
  distinct; the same pair under another owner is another row; FS-1's CHECK refuses zero or two owners.
- **`find_for_pair`** is the stored-hit lookup; **`scored_at_since`** is exclusive and oldest first
  (the port's docstring, decided at T14).
- **`store`**: a second score for the same owner and pair returns the winner's row and stores nothing;
  an owner gone is `UserNotFound` / `GuestSessionExpired`; a CV or posting gone is its own 404; each
  refusal leaves no row and a usable transaction (S-25 to S-27).
- **`EXPLAIN`** with other users' rows seeded: a lone user's rows seq-scan correctly, so the claim is
  only meaningful against a spread.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tailorcraft.domain.identity.errors import GuestSessionExpired, UserNotFound
from tailorcraft.domain.identity.ownership import GuestOwner, Owner, UserOwner
from tailorcraft.domain.identity.value_objects import GuestSessionId, UserId
from tailorcraft.domain.intake.base_cv import BaseCv
from tailorcraft.domain.intake.errors import BaseCvNotFound
from tailorcraft.domain.posting.errors import JobPostingNotFound
from tailorcraft.domain.posting.job_posting import JobPosting
from tailorcraft.domain.scoring.fit_score import FitScore
from tailorcraft.domain.scoring.value_objects import (
    CvFingerprint,
    MicroUsd,
    PostingFingerprint,
    ScoredCvRef,
    ScoredPostingRef,
)
from tailorcraft.infrastructure.clock import FixedClock
from tailorcraft.infrastructure.persistence.database import violated_constraint
from tailorcraft.infrastructure.persistence.repositories.intake.base_cv import (
    SqlAlchemyBaseCvRepository,
)
from tailorcraft.infrastructure.persistence.repositories.posting.job_posting import (
    SqlAlchemyJobPostingRepository,
)
from tailorcraft.infrastructure.persistence.repositories.scoring.fit_score import (
    SqlAlchemyFitScoreRepository,
)
from tests.integration.owners import extracted_cv, pasted_posting
from tests.integration.persistence.owner_rows import persist_guest, persist_user
from tests.integration.scoring.support import assessed

_USER_PAIR = "uq_scoring_fit_score_user_pair"
_GUEST_PAIR = "uq_scoring_fit_score_guest_pair"
_USER_INDEX = _USER_PAIR
_GUEST_INDEX = _GUEST_PAIR


def _fp(n: int) -> str:
    return f"{n:064x}"


def _score(
    repo: SqlAlchemyFitScoreRepository,
    owner: Owner,
    at: datetime,
    *,
    cv_id: UUID | None = None,
    posting_id: UUID | None = None,
    n: int = 1,
    score: int = 62,
    gaps: tuple[str, ...] = ("Kubernetes experience", "AWS certification"),
) -> FitScore:
    """A fresh success for `owner` and fingerprint pair `n`, with every column a distinct value."""
    built = FitScore.from_assessment(
        id=repo.next_identity(),
        owner=owner,
        base_cv_id=ScoredCvRef(cv_id or uuid4()),
        job_posting_id=ScoredPostingRef(posting_id or uuid4()),
        cv_fingerprint=CvFingerprint(_fp(n)),
        posting_fingerprint=PostingFingerprint(_fp(n + 7)),
        assessed=assessed(score=score, gaps=gaps),
        cost=MicroUsd(230),
        at=at,
    )
    built.release_events()
    return built


async def _inputs(session: AsyncSession, owner: Owner, at: datetime) -> tuple[BaseCv, JobPosting]:
    """A real CV and a real posting for `owner`: what `store`'s two locks look for."""
    cv = extracted_cv(owner, at)
    posting = pasted_posting(owner, at)
    await SqlAlchemyBaseCvRepository(session).add(cv)
    await SqlAlchemyJobPostingRepository(session).add(posting)
    await session.flush()
    return cv, posting


async def _count(session: AsyncSession, **where: Any) -> int:
    """Rows matching column = value, as SQL on raw UUIDs/strings (the table's typed columns want
    domain wrappers; a test counting rows wants none)."""
    clause = " AND ".join(f"{column} = :{column}" for column in where) or "true"
    return int(
        (
            await session.execute(
                text(f"SELECT count(*) FROM scoring_fit_score WHERE {clause}"),  # noqa: S608 -- test-owned names
                where,
            )
        ).scalar_one()
    )


async def _raw_insert(
    session: AsyncSession,
    *,
    user_id: UUID | None = None,
    guest_session_id: UUID | None = None,
    cv_fp: str | None = None,
    posting_fp: str | None = None,
    score: int = 50,
    scored_at: datetime,
) -> UUID:
    """A row straight into the table, as SQL, so the constraints are met without the aggregate's
    own refusals in front of them. No CV or posting is needed: neither has a foreign key (ADR-0014)."""
    row_id = uuid4()
    await session.execute(
        text(
            "INSERT INTO scoring_fit_score (id, guest_session_id, user_id, base_cv_id, "
            "job_posting_id, cv_fingerprint, posting_fingerprint, score, gaps, model, "
            "prompt_version, input_tokens, output_tokens, unmetered_attempts, llm_duration_ms, "
            "cost_microusd, scored_at) VALUES (:id, :g, :u, :cv, :p, :cfp, :pfp, :score, "
            "CAST('[]' AS jsonb), 'm', '1', 1, 1, 0, 1, 1, :at)"
        ),
        {
            "id": row_id,
            "g": guest_session_id,
            "u": user_id,
            "cv": uuid4(),
            "p": uuid4(),
            "cfp": cv_fp or _fp(1),
            "pfp": posting_fp or _fp(2),
            "score": score,
            "at": scored_at,
        },
    )
    return row_id


async def _refused_by(session: AsyncSession, **kwargs: Any) -> str | None:
    """Run `_raw_insert` in a SAVEPOINT and return the name of the constraint that refused it."""
    with pytest.raises(IntegrityError) as caught:
        async with session.begin_nested():
            await _raw_insert(session, **kwargs)
    return violated_constraint(caught.value)


# --- round trip and whole seconds ------------------------------------------------------------------


@pytest.mark.parametrize("owner_kind", ["user", "guest"])
async def test_a_stored_score_comes_back_whole_from_a_fresh_load(
    session: AsyncSession, clock: FixedClock, owner_kind: str
) -> None:
    owner: Owner = (
        await persist_user(session, clock)
        if owner_kind == "user"
        else await persist_guest(session, clock)
    )
    cv, posting = await _inputs(session, owner, clock.now())
    repo = SqlAlchemyFitScoreRepository(session)
    stored = _score(repo, owner, clock.now(), cv_id=cv.id.value, posting_id=posting.id.value)

    await repo.store(stored)
    session.expunge_all()  # the next read must come from the row, not the identity map
    found = await repo.find_for_pair(owner, stored.cv_fingerprint, stored.posting_fingerprint)

    assert found is not None
    assert found is not stored
    assert found.id == stored.id
    assert found.owner == owner
    assert found.base_cv_id == ScoredCvRef(cv.id.value)
    assert found.job_posting_id == ScoredPostingRef(posting.id.value)
    assert found.cv_fingerprint == stored.cv_fingerprint
    assert found.posting_fingerprint == stored.posting_fingerprint
    assert found.assessment == stored.assessment
    assert found.assessment.gaps == stored.assessment.gaps
    assert found.usage == stored.usage
    assert found.cost == MicroUsd(230)
    assert found.scored_at == stored.scored_at


async def test_scored_at_survives_the_database_round_trip_exactly(
    session: AsyncSession, clock: FixedClock
) -> None:
    owner = await persist_user(session, clock)
    cv, posting = await _inputs(session, owner, clock.now())
    repo = SqlAlchemyFitScoreRepository(session)
    stored = _score(repo, owner, clock.now(), cv_id=cv.id.value, posting_id=posting.id.value)
    await repo.store(stored)
    session.expunge_all()

    found = await repo.find_for_pair(owner, stored.cv_fingerprint, stored.posting_fingerprint)

    assert found is not None
    assert found.scored_at == clock.now()
    assert found.scored_at.tzinfo is not None
    assert found.scored_at.microsecond == 0


async def test_the_two_new_timestamp_columns_are_whole_second_in_the_catalogue(
    session: AsyncSession,
) -> None:
    rows = (
        await session.execute(
            text(
                "SELECT table_name, column_name, datetime_precision FROM information_schema.columns "
                "WHERE table_schema = 'public' AND (table_name, column_name) IN "
                "(('scoring_fit_score', 'scored_at'), ('scoring_daily_spend', 'updated_at'))"
            )
        )
    ).all()

    assert sorted((t, c, p) for t, c, p in rows) == [
        ("scoring_daily_spend", "updated_at", 0),
        ("scoring_fit_score", "scored_at", 0),
    ]


# --- the unique constraints and the CHECKs ---------------------------------------------------------


async def test_a_second_row_for_one_users_pair_is_refused_by_the_user_pair_constraint(
    session: AsyncSession, clock: FixedClock
) -> None:
    user = await persist_user(session, clock)
    await _raw_insert(session, user_id=user.user_id.value, scored_at=clock.now())

    refused = await _refused_by(session, user_id=user.user_id.value, scored_at=clock.now())

    assert refused == _USER_PAIR


async def test_a_second_row_for_one_guests_pair_is_refused_by_the_guest_pair_constraint(
    session: AsyncSession, clock: FixedClock
) -> None:
    guest = await persist_guest(session, clock)
    await _raw_insert(session, guest_session_id=guest.guest_session_id.value, scored_at=clock.now())

    refused = await _refused_by(
        session, guest_session_id=guest.guest_session_id.value, scored_at=clock.now()
    )

    assert refused == _GUEST_PAIR


async def test_the_same_pair_under_another_owner_is_another_row(
    session: AsyncSession, clock: FixedClock
) -> None:
    """AC-4: no score crosses owners, so the unique constraints are per owner column. Two guests
    would also collide if NULLs compared equal; three owners of one pair prove they don't."""
    first = await persist_user(session, clock)
    second = await persist_user(session, clock)
    guest = await persist_guest(session, clock)
    other_guest = await persist_guest(session, clock)

    await _raw_insert(session, user_id=first.user_id.value, scored_at=clock.now())
    await _raw_insert(session, user_id=second.user_id.value, scored_at=clock.now())
    await _raw_insert(session, guest_session_id=guest.guest_session_id.value, scored_at=clock.now())
    await _raw_insert(
        session, guest_session_id=other_guest.guest_session_id.value, scored_at=clock.now()
    )

    assert await _count(session, user_id=first.user_id.value) == 1
    assert await _count(session, user_id=second.user_id.value) == 1
    assert await _count(session, guest_session_id=guest.guest_session_id.value) == 1
    assert await _count(session, guest_session_id=other_guest.guest_session_id.value) == 1


async def test_one_owner_may_hold_many_pairs(session: AsyncSession, clock: FixedClock) -> None:
    user = await persist_user(session, clock)

    await _raw_insert(session, user_id=user.user_id.value, cv_fp=_fp(1), scored_at=clock.now())
    await _raw_insert(session, user_id=user.user_id.value, cv_fp=_fp(3), scored_at=clock.now())
    await _raw_insert(session, user_id=user.user_id.value, posting_fp=_fp(4), scored_at=clock.now())

    assert await _count(session, user_id=user.user_id.value) == 3


async def test_a_row_with_no_owner_is_refused_by_the_exactly_one_owner_check(
    session: AsyncSession, clock: FixedClock
) -> None:
    assert await _refused_by(session, scored_at=clock.now()) == (
        "ck_scoring_fit_score_exactly_one_owner"
    )


async def test_a_row_with_two_owners_is_refused_by_the_exactly_one_owner_check(
    session: AsyncSession, clock: FixedClock
) -> None:
    user = await persist_user(session, clock)
    guest = await persist_guest(session, clock)

    refused = await _refused_by(
        session,
        user_id=user.user_id.value,
        guest_session_id=guest.guest_session_id.value,
        scored_at=clock.now(),
    )

    assert refused == "ck_scoring_fit_score_exactly_one_owner"


@pytest.mark.parametrize("score", [-1, 101])
async def test_a_score_outside_zero_to_one_hundred_is_refused_by_the_database(
    session: AsyncSession, clock: FixedClock, score: int
) -> None:
    user = await persist_user(session, clock)

    refused = await _refused_by(
        session, user_id=user.user_id.value, score=score, scored_at=clock.now()
    )

    assert refused == "ck_scoring_fit_score_score_in_range"


# --- find_for_pair ---------------------------------------------------------------------------------


async def test_find_for_pair_is_none_when_nothing_is_stored(
    session: AsyncSession, clock: FixedClock
) -> None:
    user = await persist_user(session, clock)

    found = await SqlAlchemyFitScoreRepository(session).find_for_pair(
        user, CvFingerprint(_fp(1)), PostingFingerprint(_fp(2))
    )

    assert found is None


async def test_find_for_pair_matches_both_fingerprints_and_the_owner(
    session: AsyncSession, clock: FixedClock
) -> None:
    """The row is for (user, 1, 8). Every one-field change misses; the same fingerprints under a
    guest, or another user, are a different owner's pair."""
    user = await persist_user(session, clock)
    other_user = await persist_user(session, clock)
    guest = await persist_guest(session, clock)
    cv, posting = await _inputs(session, user, clock.now())
    repo = SqlAlchemyFitScoreRepository(session)
    stored = _score(repo, user, clock.now(), cv_id=cv.id.value, posting_id=posting.id.value, n=1)
    await repo.store(stored)

    hit = await repo.find_for_pair(user, CvFingerprint(_fp(1)), PostingFingerprint(_fp(8)))
    wrong_cv = await repo.find_for_pair(user, CvFingerprint(_fp(2)), PostingFingerprint(_fp(8)))
    wrong_posting = await repo.find_for_pair(
        user, CvFingerprint(_fp(1)), PostingFingerprint(_fp(9))
    )
    swapped = await repo.find_for_pair(user, CvFingerprint(_fp(8)), PostingFingerprint(_fp(1)))
    as_other_user = await repo.find_for_pair(
        other_user, CvFingerprint(_fp(1)), PostingFingerprint(_fp(8))
    )
    as_guest = await repo.find_for_pair(guest, CvFingerprint(_fp(1)), PostingFingerprint(_fp(8)))

    assert hit is not None
    assert hit.id == stored.id
    assert (wrong_cv, wrong_posting, swapped, as_other_user, as_guest) == (
        None,
        None,
        None,
        None,
        None,
    )


async def test_find_for_pair_returns_a_guests_row_to_that_guest_only(
    session: AsyncSession, clock: FixedClock
) -> None:
    guest = await persist_guest(session, clock)
    other_guest = await persist_guest(session, clock)
    cv, posting = await _inputs(session, guest, clock.now())
    repo = SqlAlchemyFitScoreRepository(session)
    stored = _score(repo, guest, clock.now(), cv_id=cv.id.value, posting_id=posting.id.value)
    await repo.store(stored)

    mine = await repo.find_for_pair(guest, stored.cv_fingerprint, stored.posting_fingerprint)
    theirs = await repo.find_for_pair(
        other_guest, stored.cv_fingerprint, stored.posting_fingerprint
    )

    assert mine is not None
    assert mine.owner == guest
    assert theirs is None


# --- scored_at_since -------------------------------------------------------------------------------


async def test_scored_at_since_is_exclusive_and_oldest_first(
    session: AsyncSession, clock: FixedClock
) -> None:
    """Seeded newest first so the order is the query's, not the insert's. A row exactly at `since`
    is **not** counted: a user returning at the second `retry_not_before` names is admitted."""
    user = await persist_user(session, clock)
    now = clock.now()
    for n, age in enumerate(
        [timedelta(hours=1), timedelta(hours=23), timedelta(hours=24), timedelta(hours=30)]
    ):
        await _raw_insert(
            session, user_id=user.user_id.value, cv_fp=_fp(10 + n), scored_at=now - age
        )
    since = now - timedelta(hours=24)

    scored = await SqlAlchemyFitScoreRepository(session).scored_at_since(user, since)

    assert list(scored) == [now - timedelta(hours=23), now - timedelta(hours=1)]


async def test_scored_at_since_none_counts_every_score_oldest_first(
    session: AsyncSession, clock: FixedClock
) -> None:
    user = await persist_user(session, clock)
    now = clock.now()
    for n, age in enumerate([timedelta(hours=1), timedelta(days=9), timedelta(hours=30)]):
        await _raw_insert(
            session, user_id=user.user_id.value, cv_fp=_fp(10 + n), scored_at=now - age
        )

    scored = await SqlAlchemyFitScoreRepository(session).scored_at_since(user, None)

    assert list(scored) == [
        now - timedelta(days=9),
        now - timedelta(hours=30),
        now - timedelta(hours=1),
    ]


async def test_scored_at_since_counts_one_owner_only(
    session: AsyncSession, clock: FixedClock
) -> None:
    user = await persist_user(session, clock)
    other_user = await persist_user(session, clock)
    guest = await persist_guest(session, clock)
    now = clock.now()
    await _raw_insert(session, user_id=user.user_id.value, scored_at=now - timedelta(hours=2))
    await _raw_insert(session, user_id=other_user.user_id.value, scored_at=now - timedelta(hours=3))
    await _raw_insert(
        session, guest_session_id=guest.guest_session_id.value, scored_at=now - timedelta(hours=4)
    )
    repo = SqlAlchemyFitScoreRepository(session)

    assert list(await repo.scored_at_since(user, None)) == [now - timedelta(hours=2)]
    assert list(await repo.scored_at_since(guest, None)) == [now - timedelta(hours=4)]


async def test_scored_at_since_is_empty_for_an_owner_with_no_scores(
    session: AsyncSession, clock: FixedClock
) -> None:
    user = await persist_user(session, clock)

    assert list(await SqlAlchemyFitScoreRepository(session).scored_at_since(user, None)) == []


# --- store: the second write for a pair ------------------------------------------------------------


async def test_a_second_store_for_the_same_pair_returns_the_winner_and_stores_nothing(
    session: AsyncSession, clock: FixedClock
) -> None:
    """ADR-0040's two-tab rule, sequentially: the second score is a different aggregate with a
    different id and a different answer, and the caller gets the first one's. The concurrent half is
    in `test_fit_score_races.py`."""
    user = await persist_user(session, clock)
    cv, posting = await _inputs(session, user, clock.now())
    repo = SqlAlchemyFitScoreRepository(session)
    winner = _score(
        repo, user, clock.now(), cv_id=cv.id.value, posting_id=posting.id.value, score=62
    )
    loser = _score(
        repo, user, clock.now(), cv_id=cv.id.value, posting_id=posting.id.value, score=17
    )
    await repo.store(winner)

    returned = await repo.store(loser)

    assert returned.id == winner.id
    assert returned.assessment.score.value == 62
    assert returned.id != loser.id
    assert await _count(session, user_id=user.user_id.value) == 1


async def test_a_losing_store_leaves_the_transaction_usable(
    session: AsyncSession, clock: FixedClock
) -> None:
    user = await persist_user(session, clock)
    cv, posting = await _inputs(session, user, clock.now())
    repo = SqlAlchemyFitScoreRepository(session)
    await repo.store(
        _score(repo, user, clock.now(), cv_id=cv.id.value, posting_id=posting.id.value)
    )
    await repo.store(
        _score(repo, user, clock.now(), cv_id=cv.id.value, posting_id=posting.id.value)
    )

    next_pair = _score(
        repo, user, clock.now(), cv_id=cv.id.value, posting_id=posting.id.value, n=40
    )
    stored = await repo.store(next_pair)

    assert stored.id == next_pair.id
    assert await _count(session, user_id=user.user_id.value) == 2


# --- store: what is gone ---------------------------------------------------------------------------


async def test_storing_for_a_user_who_is_gone_raises_user_not_found_and_keeps_the_transaction(
    session: AsyncSession, clock: FixedClock
) -> None:
    present = await persist_user(session, clock)
    cv, posting = await _inputs(session, present, clock.now())
    repo = SqlAlchemyFitScoreRepository(session)
    gone = UserOwner(UserId(uuid4()))

    with pytest.raises(UserNotFound):
        await repo.store(
            _score(repo, gone, clock.now(), cv_id=cv.id.value, posting_id=posting.id.value)
        )

    assert await _count(session, user_id=gone.user_id.value) == 0
    follow_up = _score(repo, present, clock.now(), cv_id=cv.id.value, posting_id=posting.id.value)
    assert (await repo.store(follow_up)).id == follow_up.id
    assert await _count(session, user_id=present.user_id.value) == 1


async def test_storing_for_a_guest_session_that_is_gone_raises_guest_session_expired(
    session: AsyncSession, clock: FixedClock
) -> None:
    present = await persist_user(session, clock)
    cv, posting = await _inputs(session, present, clock.now())
    repo = SqlAlchemyFitScoreRepository(session)
    gone = GuestOwner(GuestSessionId(uuid4()))

    with pytest.raises(GuestSessionExpired):
        await repo.store(
            _score(repo, gone, clock.now(), cv_id=cv.id.value, posting_id=posting.id.value)
        )

    assert await _count(session, guest_session_id=gone.guest_session_id.value) == 0
    follow_up = _score(repo, present, clock.now(), cv_id=cv.id.value, posting_id=posting.id.value)
    assert (await repo.store(follow_up)).id == follow_up.id


async def test_storing_for_a_cv_that_is_gone_raises_base_cv_not_found_and_leaves_no_row(
    session: AsyncSession, clock: FixedClock
) -> None:
    """S-25. The CV has no foreign key, so the row goes in and the lock finds nothing: the SAVEPOINT's
    rollback is what removes it. The follow-up proves both halves: the row is gone (a surviving one
    would be the 'winner' and the follow-up would come back as someone else) and the transaction is
    alive."""
    user = await persist_user(session, clock)
    _, posting = await _inputs(session, user, clock.now())
    repo = SqlAlchemyFitScoreRepository(session)
    refused = _score(repo, user, clock.now(), cv_id=uuid4(), posting_id=posting.id.value)

    with pytest.raises(BaseCvNotFound):
        await repo.store(refused)

    assert await _count(session, user_id=user.user_id.value) == 0
    cv, _ = await _inputs(session, user, clock.now())
    retry = _score(repo, user, clock.now(), cv_id=cv.id.value, posting_id=posting.id.value)
    assert (await repo.store(retry)).id == retry.id


async def test_storing_for_a_posting_that_is_gone_raises_job_posting_not_found_and_leaves_no_row(
    session: AsyncSession, clock: FixedClock
) -> None:
    user = await persist_user(session, clock)
    cv, _ = await _inputs(session, user, clock.now())
    repo = SqlAlchemyFitScoreRepository(session)
    refused = _score(repo, user, clock.now(), cv_id=cv.id.value, posting_id=uuid4())

    with pytest.raises(JobPostingNotFound):
        await repo.store(refused)

    assert await _count(session, user_id=user.user_id.value) == 0
    _, posting = await _inputs(session, user, clock.now())
    retry = _score(repo, user, clock.now(), cv_id=cv.id.value, posting_id=posting.id.value)
    assert (await repo.store(retry)).id == retry.id


async def test_a_conflicting_store_is_still_refused_when_the_cv_is_gone(
    session: AsyncSession, clock: FixedClock
) -> None:
    """A loser takes the locks before it re-reads (the module docstring's step 3), so a CV deleted
    since the winner stored answers the CV's 404 rather than handing back a row about a deleted CV."""
    user = await persist_user(session, clock)
    cv, posting = await _inputs(session, user, clock.now())
    repo = SqlAlchemyFitScoreRepository(session)
    await repo.store(
        _score(repo, user, clock.now(), cv_id=cv.id.value, posting_id=posting.id.value)
    )
    await session.execute(text("DELETE FROM intake_base_cv WHERE id = :i"), {"i": cv.id.value})

    with pytest.raises(BaseCvNotFound):
        await repo.store(
            _score(repo, user, clock.now(), cv_id=cv.id.value, posting_id=posting.id.value)
        )


# --- EXPLAIN, with other owners' rows seeded -------------------------------------------------------


async def _seed_spread(session: AsyncSession, clock: FixedClock) -> None:
    """60 other users and 60 other guest sessions with 40 scores each, then `ANALYZE`: a lone owner's
    rows would correctly seq-scan, so the index is only the planner's choice against a spread."""
    await session.execute(
        text(
            "INSERT INTO identity_user (id, email, password_hash, created_at, password_updated_at) "
            "SELECT gen_random_uuid(), 'fit-spread-' || g || '@example.com', 'x', CAST(:t AS timestamptz), CAST(:t AS timestamptz) "
            "FROM generate_series(1, 60) AS g"
        ),
        {"t": clock.now()},
    )
    await session.execute(
        text(
            "INSERT INTO identity_guest_session (id, token_hash, created_at, expires_at) "
            "SELECT gen_random_uuid(), md5(g::text) || md5(g::text || 'x'), CAST(:t AS timestamptz), "
            "CAST(:t AS timestamptz) + interval '25 hours' "
            "FROM generate_series(1, 60) AS g"
        ),
        {"t": clock.now()},
    )
    insert = (
        "INSERT INTO scoring_fit_score (id, guest_session_id, user_id, base_cv_id, job_posting_id, "
        "cv_fingerprint, posting_fingerprint, score, gaps, model, prompt_version, input_tokens, "
        "output_tokens, unmetered_attempts, llm_duration_ms, cost_microusd, scored_at) "
        "SELECT gen_random_uuid(), {guest}, {user}, gen_random_uuid(), gen_random_uuid(), "
        "md5('c' || o.id::text || g::text) || md5('x' || o.id::text || g::text), "
        "md5('p' || o.id::text || g::text) || md5('y' || o.id::text || g::text), "
        "50, CAST('[]' AS jsonb), 'm', '1', 1, 1, 0, 1, 1, CAST(:t AS timestamptz) + g * interval '1 second' "
        "FROM {source} o, generate_series(1, 40) AS g WHERE {filter}"
    )
    await session.execute(
        text(
            insert.format(
                guest="NULL",
                user="o.id",
                source="identity_user",
                filter="o.email LIKE 'fit-spread-%'",
            )
        ),
        {"t": clock.now()},
    )
    await session.execute(
        text(
            insert.format(
                guest="o.id",
                user="NULL",
                source="identity_guest_session",
                filter="o.expires_at = CAST(:t AS timestamptz) + interval '25 hours'",
            )
        ),
        {"t": clock.now()},
    )
    await session.execute(text("ANALYZE scoring_fit_score"))


async def _plan_of_the_scoring_select(session: AsyncSession, call: Any) -> str:
    connection = await session.connection()
    captured: list[tuple[str, object]] = []

    def capture(
        conn: Connection, cursor: object, statement: str, parameters: object, *_: object
    ) -> None:
        if "scoring_fit_score" in statement and statement.lstrip().upper().startswith("SELECT"):
            captured.append((statement, parameters))

    engine = session.get_bind()
    event.listen(engine, "before_cursor_execute", capture)
    try:
        await call()
    finally:
        event.remove(engine, "before_cursor_execute", capture)

    assert len(captured) == 1, captured
    statement, parameters = captured[0]
    plan = await connection.exec_driver_sql(f"EXPLAIN {statement}", parameters)  # type: ignore[arg-type]
    return "\n".join(row[0] for row in plan)


@pytest.mark.parametrize("owner_kind", ["user", "guest"])
async def test_the_stored_hit_lookup_is_planned_through_the_owners_pair_index(
    session: AsyncSession, clock: FixedClock, owner_kind: str
) -> None:
    owner: Owner = (
        await persist_user(session, clock)
        if owner_kind == "user"
        else await persist_guest(session, clock)
    )
    await _raw_insert(
        session,
        user_id=owner.user_id.value if isinstance(owner, UserOwner) else None,
        guest_session_id=owner.guest_session_id.value if isinstance(owner, GuestOwner) else None,
        scored_at=clock.now(),
    )
    await _seed_spread(session, clock)
    repo = SqlAlchemyFitScoreRepository(session)

    plan = await _plan_of_the_scoring_select(
        session,
        lambda: repo.find_for_pair(owner, CvFingerprint(_fp(1)), PostingFingerprint(_fp(2))),
    )

    assert (_USER_INDEX if owner_kind == "user" else _GUEST_INDEX) in plan, plan
    assert "Seq Scan on scoring_fit_score" not in plan, plan


@pytest.mark.parametrize("owner_kind", ["user", "guest"])
async def test_the_window_count_is_planned_through_the_owners_pair_index(
    session: AsyncSession, clock: FixedClock, owner_kind: str
) -> None:
    owner: Owner = (
        await persist_user(session, clock)
        if owner_kind == "user"
        else await persist_guest(session, clock)
    )
    await _raw_insert(
        session,
        user_id=owner.user_id.value if isinstance(owner, UserOwner) else None,
        guest_session_id=owner.guest_session_id.value if isinstance(owner, GuestOwner) else None,
        scored_at=clock.now(),
    )
    await _seed_spread(session, clock)
    repo = SqlAlchemyFitScoreRepository(session)

    plan = await _plan_of_the_scoring_select(
        session, lambda: repo.scored_at_since(owner, clock.now() - timedelta(hours=24))
    )

    assert (_USER_INDEX if owner_kind == "user" else _GUEST_INDEX) in plan, plan
    assert "Seq Scan on scoring_fit_score" not in plan, plan
