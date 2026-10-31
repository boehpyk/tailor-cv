"""Fit scores and the things that delete or count them, against a real Postgres (slice 5.1, T23;
AC-33, AC-36's adapter half, AC-37, AC-38, and the counts AC-39 shows).

Written from the spec. A score carries no foreign key to its CV or posting (ADR-0014), so these rows
are seeded as SQL (`fit_score_rows`) and aimed at ids alone.

- **Passed on arrival, by design (the plan says "no code"):** AC-33's purge and the account cascade.
  They are here because AC-33 names them; each carries its control (a user's rows, a live session's).
- **Red against the skeleton:** `CommittingBaseCvRemoval.remove` and `delete_history_entry` leave a
  score behind (AC-37, AC-38); `count_account` and the directory do not count scores (AC-39). The
  count fields are read with `getattr`, so a field T24 has yet to add is a wrong value, not an
  `AttributeError`.
- Each deletion scopes to **the user and the id**: another user's row over the same CV or posting id
  (raw SQL; the use cases never build one) and the same user's rows over other ids survive.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from tailorcraft.application.retention.purge_expired_guest_sessions import (
    PurgeExpiredGuestSessions,
)
from tailorcraft.domain.identity.ownership import UserOwner
from tailorcraft.domain.intake.errors import BaseCvNotFound
from tailorcraft.domain.intake.value_objects import BaseCvId
from tailorcraft.domain.posting.value_objects import JobPostingId
from tailorcraft.domain.retention.value_objects import RetentionWindow
from tailorcraft.infrastructure.clock import FixedClock
from tailorcraft.infrastructure.files.local_file_store import LocalFileStore
from tailorcraft.infrastructure.intake.committing import CommittingBaseCvRemoval
from tailorcraft.infrastructure.persistence.queries.guest_session_directory import (
    SqlGuestSessionDirectory,
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
from tailorcraft.infrastructure.persistence.retention.account_data import SqlAlchemyAccountData
from tailorcraft.infrastructure.persistence.retention.expired_guest_data import (
    SqlAlchemyExpiredGuestData,
)
from tailorcraft.infrastructure.persistence.retention.history_entry_data import (
    SqlAlchemyHistoryEntryData,
)
from tailorcraft.infrastructure.retention.data_access import CommittingExpiredGuestDataAdapter
from tests.integration.owners import extracted_cv, pasted_posting, succeeded_run
from tests.integration.persistence.fit_score_rows import insert_score_row, owner_of
from tests.integration.persistence.owner_rows import persist_user
from tests.integration.retention.test_purge_database import _Rig


@pytest.fixture
def rig(session: AsyncSession, clock: FixedClock, tmp_path: Path) -> _Rig:
    return _Rig(session=session, clock=clock, files=LocalFileStore(tmp_path))


async def _exists(session: AsyncSession, row_id: UUID) -> bool:
    return await owner_of(session, row_id) is not None


async def _saved_cv(session: AsyncSession, clock: FixedClock, owner: UserOwner) -> BaseCvId:
    cv = extracted_cv(owner, clock.now())
    await SqlAlchemyBaseCvRepository(session).add(cv)
    await session.flush()
    return cv.id


# --- AC-33: purge -------------------------------------------------------------------------------


async def test_ac33_a_purge_takes_an_expired_sessions_scores_and_spares_everyone_elses(
    session: AsyncSession, rig: _Rig, clock: FixedClock, tmp_path: Path
) -> None:
    """The purge has no new code: the rows go by the session's cascade. A live session's score and a
    user's score are untouched."""
    now = clock.now()
    expired = await rig.new_guest_session(expires_at=now - timedelta(hours=30))
    live = await rig.new_guest_session(expires_at=now + timedelta(hours=1))
    user = await persist_user(session, clock)
    gone = [
        await insert_score_row(session, guest_session_id=expired.value, pair=pair, scored_at=now)
        for pair in (1, 2)
    ]
    kept_live = await insert_score_row(session, guest_session_id=live.value, pair=3, scored_at=now)
    kept_user = await insert_score_row(session, user_id=user.user_id.value, pair=1, scored_at=now)
    await session.commit()

    purge = PurgeExpiredGuestSessions(
        data=CommittingExpiredGuestDataAdapter(SqlAlchemyExpiredGuestData(session), session),
        files=LocalFileStore(tmp_path),
        clock=clock,
        window=RetentionWindow(hours=24),
        batch_limit=10,
        dry_run=False,
    )
    await purge()

    assert [await _exists(session, row) for row in gone] == [False, False]
    assert await _exists(session, kept_live)
    assert await _exists(session, kept_user)


# --- AC-37: deleting a saved CV -----------------------------------------------------------------


async def test_ac37_deleting_a_saved_cv_deletes_that_users_scores_over_it(
    session: AsyncSession, clock: FixedClock
) -> None:
    user = await persist_user(session, clock)
    cv = await _saved_cv(session, clock, user)
    mine = [
        await insert_score_row(
            session,
            user_id=user.user_id.value,
            base_cv_id=cv.value,
            pair=pair,
            scored_at=clock.now(),
        )
        for pair in (1, 2)
    ]

    await CommittingBaseCvRemoval(SqlAlchemyBaseCvRepository(session), session).remove(cv, user)

    assert [await _exists(session, row) for row in mine] == [False, False]


async def test_ac37_deleting_a_saved_cv_spares_other_cvs_and_other_users_scores(
    session: AsyncSession, clock: FixedClock
) -> None:
    """The controls: the same user's score over another CV, and a stranger's row that carries this
    CV's id (raw SQL: no use case builds one, and the delete must not reach it)."""
    user = await persist_user(session, clock)
    stranger = await persist_user(session, clock)
    cv = await _saved_cv(session, clock, user)
    other_cv = await _saved_cv(session, clock, user)
    over_other = await insert_score_row(
        session,
        user_id=user.user_id.value,
        base_cv_id=other_cv.value,
        pair=1,
        scored_at=clock.now(),
    )
    strangers = await insert_score_row(
        session, user_id=stranger.user_id.value, base_cv_id=cv.value, pair=2, scored_at=clock.now()
    )
    doomed = await insert_score_row(
        session, user_id=user.user_id.value, base_cv_id=cv.value, pair=3, scored_at=clock.now()
    )

    await CommittingBaseCvRemoval(SqlAlchemyBaseCvRepository(session), session).remove(cv, user)

    assert not await _exists(session, doomed), "premise: the delete reached this user's score"
    assert await _exists(session, over_other)
    assert await _exists(session, strangers)


async def test_ac37_a_refused_delete_leaves_the_scores(
    session: AsyncSession, clock: FixedClock
) -> None:
    """Someone else's CV id: `BaseCvNotFound`, and the owner's score over it stays."""
    owner = await persist_user(session, clock)
    intruder = await persist_user(session, clock)
    cv = await _saved_cv(session, clock, owner)
    score = await insert_score_row(
        session, user_id=owner.user_id.value, base_cv_id=cv.value, pair=1, scored_at=clock.now()
    )

    with pytest.raises(BaseCvNotFound):
        await CommittingBaseCvRemoval(SqlAlchemyBaseCvRepository(session), session).remove(
            cv, intruder
        )

    assert await _exists(session, score)


# --- AC-38: deleting a history entry ------------------------------------------------------------


async def _run_over_new_posting(
    session: AsyncSession, clock: FixedClock, owner: UserOwner, cv: BaseCvId
) -> tuple[UUID, UUID]:
    """`(run id, posting id)`: a posting and a succeeded run over it and `cv`."""
    posting = pasted_posting(owner, clock.now())
    await SqlAlchemyJobPostingRepository(session).add(posting)
    run = succeeded_run(owner, clock.now(), base_cv_id=cv, job_posting_id=posting.id)
    await SqlAlchemyTailoringRunRepository(session).add(run)
    await session.flush()
    return run.id.value, posting.id.value


async def test_ac38_deleting_an_entry_that_takes_its_posting_deletes_the_users_scores_over_it(
    session: AsyncSession, clock: FixedClock
) -> None:
    user = await persist_user(session, clock)
    cv = await _saved_cv(session, clock, user)
    run_id, posting_id = await _run_over_new_posting(session, clock, user, cv)
    mine = [
        await insert_score_row(
            session,
            user_id=user.user_id.value,
            job_posting_id=posting_id,
            pair=pair,
            scored_at=clock.now(),
        )
        for pair in (1, 2)
    ]

    deleted = await SqlAlchemyHistoryEntryData(session).delete_history_entry(user.user_id, run_id)

    assert deleted is not None
    assert deleted.posting_deleted is True, "premise: the posting went"
    assert [await _exists(session, row) for row in mine] == [False, False]


async def test_ac38_deleting_an_entry_spares_other_postings_and_other_users_scores(
    session: AsyncSession, clock: FixedClock
) -> None:
    user = await persist_user(session, clock)
    stranger = await persist_user(session, clock)
    cv = await _saved_cv(session, clock, user)
    run_id, posting_id = await _run_over_new_posting(session, clock, user, cv)
    _, other_posting = await _run_over_new_posting(session, clock, user, cv)
    doomed = await insert_score_row(
        session,
        user_id=user.user_id.value,
        job_posting_id=posting_id,
        pair=1,
        scored_at=clock.now(),
    )
    over_other = await insert_score_row(
        session,
        user_id=user.user_id.value,
        job_posting_id=other_posting,
        pair=2,
        scored_at=clock.now(),
    )
    strangers = await insert_score_row(
        session,
        user_id=stranger.user_id.value,
        job_posting_id=posting_id,
        pair=3,
        scored_at=clock.now(),
    )

    await SqlAlchemyHistoryEntryData(session).delete_history_entry(user.user_id, run_id)

    assert not await _exists(session, doomed), "premise: the delete reached this user's score"
    assert await _exists(session, over_other)
    assert await _exists(session, strangers)


async def test_ac38_a_posting_another_run_still_references_keeps_its_scores(
    session: AsyncSession, clock: FixedClock
) -> None:
    """A posting kept because a sibling run references it keeps its scores: a score is about the
    pair, and the pair is still there."""
    user = await persist_user(session, clock)
    cv = await _saved_cv(session, clock, user)
    run_id, posting_id = await _run_over_new_posting(session, clock, user, cv)
    sibling = succeeded_run(
        user, clock.now(), base_cv_id=cv, job_posting_id=JobPostingId(posting_id)
    )
    await SqlAlchemyTailoringRunRepository(session).add(sibling)
    score = await insert_score_row(
        session,
        user_id=user.user_id.value,
        job_posting_id=posting_id,
        pair=1,
        scored_at=clock.now(),
    )

    deleted = await SqlAlchemyHistoryEntryData(session).delete_history_entry(user.user_id, run_id)

    assert deleted is not None
    assert deleted.posting_deleted is False, "premise: the posting stayed"
    assert await _exists(session, score)


async def test_ac38_a_foreign_deletion_attempt_leaves_the_scores(
    session: AsyncSession, clock: FixedClock
) -> None:
    owner = await persist_user(session, clock)
    intruder = await persist_user(session, clock)
    cv = await _saved_cv(session, clock, owner)
    run_id, posting_id = await _run_over_new_posting(session, clock, owner, cv)
    score = await insert_score_row(
        session,
        user_id=owner.user_id.value,
        job_posting_id=posting_id,
        pair=1,
        scored_at=clock.now(),
    )

    deleted = await SqlAlchemyHistoryEntryData(session).delete_history_entry(
        intruder.user_id, run_id
    )

    assert deleted is None
    assert await _exists(session, score)


# --- AC-36 / AC-39: the counts ------------------------------------------------------------------


def _count(value: object, name: str) -> int | None:
    found = getattr(value, name, None)
    return found if isinstance(found, int) else None


async def test_ac36_count_account_counts_the_users_scores_and_only_theirs(
    session: AsyncSession, clock: FixedClock
) -> None:
    user = await persist_user(session, clock)
    other = await persist_user(session, clock)
    for pair in (1, 2, 3):
        await insert_score_row(
            session, user_id=user.user_id.value, pair=pair, scored_at=clock.now()
        )
    await insert_score_row(session, user_id=other.user_id.value, pair=1, scored_at=clock.now())

    counts = await SqlAlchemyAccountData(session).count_account(user.user_id)

    assert counts is not None
    assert _count(counts, "fit_scores") == 3


async def test_ac36_count_account_of_a_user_with_no_scores_counts_zero(
    session: AsyncSession, clock: FixedClock
) -> None:
    user = await persist_user(session, clock)
    other = await persist_user(session, clock)
    await insert_score_row(session, user_id=other.user_id.value, pair=1, scored_at=clock.now())

    counts = await SqlAlchemyAccountData(session).count_account(user.user_id)

    assert counts is not None
    assert _count(counts, "fit_scores") == 0


async def test_ac36_delete_account_takes_the_users_scores_by_cascade_and_spares_others(
    session: AsyncSession, clock: FixedClock
) -> None:
    """Passes on arrival: the cascade is the FK's. Here because AC-36 names it."""
    user = await persist_user(session, clock)
    other = await persist_user(session, clock)
    mine = await insert_score_row(
        session, user_id=user.user_id.value, pair=1, scored_at=clock.now()
    )
    theirs = await insert_score_row(
        session, user_id=other.user_id.value, pair=1, scored_at=clock.now()
    )

    assert await SqlAlchemyAccountData(session).delete_account(user.user_id) is True

    assert not await _exists(session, mine)
    assert await _exists(session, theirs)


async def test_ac39_the_guest_directory_counts_a_sessions_scores_and_only_its_own(
    session: AsyncSession, rig: _Rig, clock: FixedClock
) -> None:
    now = clock.now()
    mine = await rig.new_guest_session(expires_at=now + timedelta(hours=1))
    other = await rig.new_guest_session(expires_at=now + timedelta(hours=1))
    for pair in (1, 2):
        await insert_score_row(session, guest_session_id=mine.value, pair=pair, scored_at=now)
    await insert_score_row(session, guest_session_id=other.value, pair=1, scored_at=now)
    directory = SqlGuestSessionDirectory(session)

    summary = await directory.get(mine, now)

    assert summary is not None
    assert _count(summary, "fit_scores") == 2


async def test_ac39_a_session_with_no_scores_counts_zero(
    session: AsyncSession, rig: _Rig, clock: FixedClock
) -> None:
    now = clock.now()
    empty = await rig.new_guest_session(expires_at=now + timedelta(hours=1))
    await insert_score_row(
        session,
        guest_session_id=(await rig.new_guest_session(expires_at=now + timedelta(hours=1))).value,
        pair=1,
        scored_at=now,
    )

    summary = await SqlGuestSessionDirectory(session).get(empty, now)

    assert summary is not None
    assert _count(summary, "fit_scores") == 0
