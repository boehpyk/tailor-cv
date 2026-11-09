"""Draft scores in the database (slice 5.2, T30, test-after): the run lock on `store`, and the three
ways a draft score goes (AC-22): its history entry, its saved CV, its account.

A draft score is a `scoring_fit_score` row with `tailoring_run_id` set. It names its run and CV by id
alone (ADR-0014: no foreign key), so these rows are SQL, aimed by id, and the lock is what
`SqlAlchemyFitScoreRepository.store` does instead of a constraint.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tailorcraft.domain.identity.ownership import UserOwner
from tailorcraft.domain.intake.value_objects import BaseCvId
from tailorcraft.domain.scoring.fit_score import FitScore
from tailorcraft.domain.scoring.value_objects import (
    CvFingerprint,
    MicroUsd,
    PostingFingerprint,
    ScoredCvRef,
    ScoredPostingRef,
    ScoredRunRef,
)
from tailorcraft.domain.tailoring.errors import TailoringRunNotFound
from tailorcraft.infrastructure.clock import FixedClock
from tailorcraft.infrastructure.intake.committing import CommittingBaseCvRemoval
from tailorcraft.infrastructure.persistence.repositories.intake.base_cv import (
    SqlAlchemyBaseCvRepository,
)
from tailorcraft.infrastructure.persistence.repositories.posting.job_posting import (
    SqlAlchemyJobPostingRepository,
)
from tailorcraft.infrastructure.persistence.repositories.scoring.fit_score import (
    SqlAlchemyFitScoreRepository,
)
from tailorcraft.infrastructure.persistence.repositories.tailoring.tailoring_run import (
    SqlAlchemyTailoringRunRepository,
)
from tailorcraft.infrastructure.persistence.retention.account_data import SqlAlchemyAccountData
from tailorcraft.infrastructure.persistence.retention.history_entry_data import (
    SqlAlchemyHistoryEntryData,
)
from tests.integration.owners import extracted_cv, pasted_posting, succeeded_run
from tests.integration.persistence.fit_score_rows import fingerprint, insert_score_row, owner_of
from tests.integration.persistence.owner_rows import persist_user
from tests.integration.scoring.support import assessed


async def _set_run(session: AsyncSession, row: UUID, run: UUID) -> UUID:
    await session.execute(
        text("UPDATE scoring_fit_score SET tailoring_run_id = :r WHERE id = :i"),
        {"r": run, "i": row},
    )
    return row


async def _draft_score(
    session: AsyncSession,
    clock: FixedClock,
    user: UserOwner,
    *,
    cv: UUID,
    posting: UUID,
    run: UUID,
    pair: int,
) -> UUID:
    row = await insert_score_row(
        session,
        user_id=user.user_id.value,
        base_cv_id=cv,
        job_posting_id=posting,
        pair=pair,
        scored_at=clock.now(),
    )
    return await _set_run(session, row, run)


async def _exists(session: AsyncSession, row: UUID) -> bool:
    return await owner_of(session, row) is not None


async def _world(
    session: AsyncSession, clock: FixedClock, user: UserOwner
) -> tuple[BaseCvId, UUID, UUID]:
    """A saved CV, a posting and one succeeded run over them: `(cv, posting, run)`."""
    cv = extracted_cv(user, clock.now())
    posting = pasted_posting(user, clock.now())
    await SqlAlchemyBaseCvRepository(session).add(cv)
    await SqlAlchemyJobPostingRepository(session).add(posting)
    run = succeeded_run(user, clock.now(), base_cv_id=cv.id, job_posting_id=posting.id)
    await SqlAlchemyTailoringRunRepository(session).add(run)
    await session.flush()
    return cv.id, posting.id.value, run.id.value


def _draft(
    repo: SqlAlchemyFitScoreRepository,
    user: UserOwner,
    at: datetime,
    *,
    cv: UUID,
    posting: UUID,
    run: UUID,
) -> FitScore:
    built = FitScore.from_assessment(
        id=repo.next_identity(),
        owner=user,
        base_cv_id=ScoredCvRef(cv),
        job_posting_id=ScoredPostingRef(posting),
        cv_fingerprint=CvFingerprint(fingerprint(90)),
        posting_fingerprint=PostingFingerprint(fingerprint(91)),
        assessed=assessed(score=71),
        cost=MicroUsd(230),
        at=at,
        draft_of=ScoredRunRef(run),
    )
    built.release_events()
    return built


# --- the run lock -------------------------------------------------------------------------------


async def test_a_draft_score_for_a_run_that_exists_is_stored_with_its_run(
    session: AsyncSession, clock: FixedClock
) -> None:
    user = await persist_user(session, clock)
    cv, posting, run = await _world(session, clock, user)
    repo = SqlAlchemyFitScoreRepository(session)
    draft = _draft(repo, user, clock.now(), cv=cv.value, posting=posting, run=run)

    stored = await repo.store(draft)
    session.expunge_all()

    found = await repo.find_for_pair(user, draft.cv_fingerprint, draft.posting_fingerprint)
    assert stored.id == draft.id
    assert found is not None
    assert found.draft_of == ScoredRunRef(run)


async def test_a_draft_score_for_a_run_that_is_gone_raises_tailoring_run_not_found_and_leaves_no_row(
    session: AsyncSession, clock: FixedClock
) -> None:
    """The run has no foreign key behind the score, so the insert succeeds and the `FOR KEY SHARE` on
    the run row finds nothing: the SAVEPOINT's rollback is what removes the row. The follow-up store
    proves the transaction is alive."""
    user = await persist_user(session, clock)
    cv, posting, run = await _world(session, clock, user)
    repo = SqlAlchemyFitScoreRepository(session)
    orphan = _draft(repo, user, clock.now(), cv=cv.value, posting=posting, run=uuid4())

    with pytest.raises(TailoringRunNotFound):
        await repo.store(orphan)

    assert not await _exists(session, orphan.id.value)
    retry = _draft(repo, user, clock.now(), cv=cv.value, posting=posting, run=run)
    assert (await repo.store(retry)).id == retry.id


# --- AC-22: a draft score goes with its history entry, its CV, its account -------------------------


async def test_ac22_deleting_a_history_entry_deletes_that_runs_draft_scores_and_only_those(
    session: AsyncSession, clock: FixedClock
) -> None:
    user = await persist_user(session, clock)
    stranger = await persist_user(session, clock)
    cv, posting, run = await _world(session, clock, user)
    # A second run over the same CV and posting: the posting stays, so the base score stays too.
    other_run = succeeded_run(
        user, clock.now(), base_cv_id=cv, job_posting_id=_posting_ref(posting)
    )
    await SqlAlchemyTailoringRunRepository(session).add(other_run)
    await session.flush()
    doomed = await _draft_score(session, clock, user, cv=cv.value, posting=posting, run=run, pair=1)
    other_runs = await _draft_score(
        session, clock, user, cv=cv.value, posting=posting, run=other_run.id.value, pair=2
    )
    base_score = await insert_score_row(
        session,
        user_id=user.user_id.value,
        base_cv_id=cv.value,
        job_posting_id=None,
        pair=3,
        scored_at=clock.now(),
    )
    strangers = await _draft_score(
        session, clock, stranger, cv=uuid4(), posting=uuid4(), run=run, pair=4
    )

    deleted = await SqlAlchemyHistoryEntryData(session).delete_history_entry(user.user_id, run)

    assert deleted is not None
    assert deleted.posting_deleted is False, "premise: only the run's own rows go"
    assert not await _exists(session, doomed), "premise: the delete reached the draft score"
    assert await _exists(session, other_runs)
    assert await _exists(session, base_score)
    assert await _exists(session, strangers), "scoped to the user as well as the run id"


async def test_ac22_deleting_the_saved_cv_deletes_a_draft_score_over_it(
    session: AsyncSession, clock: FixedClock
) -> None:
    user = await persist_user(session, clock)
    cv, posting, run = await _world(session, clock, user)
    draft = await _draft_score(session, clock, user, cv=cv.value, posting=posting, run=run, pair=1)

    await CommittingBaseCvRemoval(SqlAlchemyBaseCvRepository(session), session).remove(cv, user)

    assert not await _exists(session, draft)


async def test_ac22_erasing_the_account_deletes_its_draft_scores(
    session: AsyncSession, clock: FixedClock
) -> None:
    user = await persist_user(session, clock)
    cv, posting, run = await _world(session, clock, user)
    draft = await _draft_score(session, clock, user, cv=cv.value, posting=posting, run=run, pair=1)
    survivor_owner = await persist_user(session, clock)
    survivor = await _draft_score(
        session, clock, survivor_owner, cv=uuid4(), posting=uuid4(), run=uuid4(), pair=1
    )

    assert await SqlAlchemyAccountData(session).delete_account(user.user_id)

    assert not await _exists(session, draft)
    assert await _exists(session, survivor)


def _posting_ref(posting: UUID) -> object:
    from tailorcraft.domain.posting.value_objects import JobPostingId

    return JobPostingId(posting)
