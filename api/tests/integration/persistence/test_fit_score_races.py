"""`SqlAlchemyFitScoreRepository.store` against the transactions it races, **on real, independent
connections** (slice 5.1, T20, test-after; AC-5, S-20, and the lock-order question the plan's §0.3 leaves
open). The deletion races (a CV or posting deleted while `store` runs, both orders) are T23's.

Each test stages the interleaving it names and **proves from `pg_stat_activity` that the overlap
happened** (`wait_for_lock_waiter`): a task that is "not done yet" cannot tell a lock wait from a task
that has not been scheduled. Both connections are pinned with `lock_timeout` and `statement_timeout`, and
every wait is bounded, so a cycle fails in seconds with a deadlock error instead of hanging.

**AC-5, the two-tab race.** The winner's `INSERT` is uncommitted; the loser's `INSERT … ON CONFLICT DO
NOTHING` is observed waiting on it; the winner commits; the loser inserts nothing, takes the CV and posting
locks, and re-reads: it returns the winner's row, never an error. If the winner rolls back instead, the
loser's own insert proceeds.

**The lock-order question.** The plan says the owner FK fires first so erasure meets it "before any CV
lock: no cycle". That sentence is true of a *winner* and not of a *loser*: a loser inserts nothing, so its
FK check never runs and it holds no owner lock; it takes the CV and then the posting. Erasure's cascade
deletes the CV and the posting in one statement. A cycle needs the cascade to take the posting first (or the
loser to take it first). It does neither, **because the cascade fires `intake_base_cv` before
`posting_job_posting`** (trigger name order, which is FK creation order), and `store` takes the CV first.
So the order is consistent and two interleavings, each of which would deadlock under the opposite order,
finish cleanly:

- the loser holds the CV and waits for the posting when the erasure arrives (the erasure waits for the
  CV, the loser then gets the posting and commits);
- the erasure holds the CV and waits for the posting when the loser arrives (the loser waits for the
  CV, then finds it gone and answers the CV's 404).

The premise is the cascade order, which no test of `store` would notice moving, so it is pinned
directly from the catalogue.

Rows are real and committed; every assertion is scoped to ids this test created, and teardown deletes
the user (the cascades take the rest).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from tailorcraft.domain.identity.ownership import UserOwner
from tailorcraft.domain.identity.value_objects import UserId
from tailorcraft.domain.intake.errors import BaseCvNotFound
from tailorcraft.domain.scoring.fit_score import FitScore
from tailorcraft.domain.scoring.value_objects import (
    CvFingerprint,
    MicroUsd,
    PostingFingerprint,
    ScoredCvRef,
    ScoredPostingRef,
)
from tailorcraft.infrastructure.clock import FixedClock
from tailorcraft.infrastructure.persistence.repositories.intake.base_cv import (
    SqlAlchemyBaseCvRepository,
)
from tailorcraft.infrastructure.persistence.repositories.posting.job_posting import (
    SqlAlchemyJobPostingRepository,
)
from tailorcraft.infrastructure.persistence.repositories.scoring.fit_score import (
    SqlAlchemyFitScoreRepository,
)
from tailorcraft.infrastructure.settings import Settings
from tests.integration.claim_race_support import (
    assert_test_database,
    bound_cleanup_locks,
    drop_rows,
    new_user,
    pinned_session,
    wait_for_lock_waiter,
)
from tests.integration.owners import extracted_cv, pasted_posting
from tests.integration.scoring.support import assessed

_STEP_TIMEOUT = 15.0


@dataclass(frozen=True, slots=True)
class _World:
    user: UserId
    cv: UUID
    posting: UUID

    @property
    def owner(self) -> UserOwner:
        return UserOwner(self.user)


async def _world(engine: AsyncEngine, clock: FixedClock) -> _World:
    user = await new_user(engine, clock)
    owner = UserOwner(user)
    cv = extracted_cv(owner, clock.now())
    posting = pasted_posting(owner, clock.now())
    async with async_sessionmaker(engine, expire_on_commit=False)() as seeding:
        await SqlAlchemyBaseCvRepository(seeding).add(cv)
        await SqlAlchemyJobPostingRepository(seeding).add(posting)
        await seeding.commit()
    return _World(user=user, cv=cv.id.value, posting=posting.id.value)


def _score(
    repo: SqlAlchemyFitScoreRepository, world: _World, clock: FixedClock, *, score: int
) -> FitScore:
    """The same owner and pair every time, so two of these are the two tabs."""
    built = FitScore.from_assessment(
        id=repo.next_identity(),
        owner=world.owner,
        base_cv_id=ScoredCvRef(world.cv),
        job_posting_id=ScoredPostingRef(world.posting),
        cv_fingerprint=CvFingerprint(f"{1:064x}"),
        posting_fingerprint=PostingFingerprint(f"{2:064x}"),
        assessed=assessed(score=score),
        cost=MicroUsd(230),
        at=clock.now(),
    )
    built.release_events()
    return built


async def _stored_rows(engine: AsyncEngine, user: UserId) -> list[tuple[UUID, int]]:
    async with engine.connect() as probe:
        rows = await probe.execute(
            text("SELECT id, score FROM scoring_fit_score WHERE user_id = :u"), {"u": user.value}
        )
        return [(row[0], int(row[1])) for row in rows]


async def _count(engine: AsyncEngine, table: str, column: str, value: UUID) -> int:
    async with engine.connect() as probe:
        return int(
            (
                await probe.execute(
                    text(f"SELECT count(*) FROM {table} WHERE {column} = :v"),  # noqa: S608 -- test-owned names
                    {"v": value},
                )
            ).scalar_one()
        )


async def _cleanup(engine: AsyncEngine, user: UserId) -> None:
    async with engine.begin() as conn:
        await bound_cleanup_locks(conn)
    await drop_rows(engine, users=[user])


# --- AC-5: the two-tab race -----------------------------------------------------------------------


async def test_ac5_the_loser_of_a_two_tab_race_returns_the_winners_row_and_stores_nothing(
    settings: Settings, engine: AsyncEngine, clock: FixedClock
) -> None:
    """Winner uncommitted → the loser's insert is observed waiting on it → the winner commits → the
    loser answers with the winner's id and score. One row, the winner's. (Without `ON CONFLICT DO
    NOTHING` the loser would raise the unique violation instead of returning.)"""
    assert_test_database(settings)
    world = await _world(engine, clock)
    try:
        async with pinned_session(engine) as tab_one, pinned_session(engine) as tab_two:
            first_repo = SqlAlchemyFitScoreRepository(tab_one)
            second_repo = SqlAlchemyFitScoreRepository(tab_two)
            winner = _score(first_repo, world, clock, score=62)
            loser = _score(second_repo, world, clock, score=17)
            assert (await first_repo.store(winner)).id == winner.id  # uncommitted

            pending = asyncio.create_task(second_repo.store(loser))
            await wait_for_lock_waiter(engine, "insert into scoring_fit_score")
            assert not pending.done(), "the second tab's INSERT must wait for the first's commit"
            await tab_one.commit()

            returned = await asyncio.wait_for(pending, _STEP_TIMEOUT)
            await tab_two.commit()

        assert returned.id == winner.id
        assert returned.assessment.score.value == 62
        assert await _stored_rows(engine, world.user) == [(winner.id.value, 62)]
    finally:
        await _cleanup(engine, world.user)


async def test_ac5_a_loser_whose_winner_rolls_back_inserts_its_own_row(
    settings: Settings, engine: AsyncEngine, clock: FixedClock
) -> None:
    """The control for the test above: the loser is only a loser if the winner commits."""
    assert_test_database(settings)
    world = await _world(engine, clock)
    try:
        async with pinned_session(engine) as tab_one, pinned_session(engine) as tab_two:
            first_repo = SqlAlchemyFitScoreRepository(tab_one)
            second_repo = SqlAlchemyFitScoreRepository(tab_two)
            first = _score(first_repo, world, clock, score=62)
            second = _score(second_repo, world, clock, score=17)
            await first_repo.store(first)

            pending = asyncio.create_task(second_repo.store(second))
            await wait_for_lock_waiter(engine, "insert into scoring_fit_score")
            await tab_one.rollback()

            returned = await asyncio.wait_for(pending, _STEP_TIMEOUT)
            await tab_two.commit()

        assert returned.id == second.id
        assert await _stored_rows(engine, world.user) == [(second.id.value, 17)]
    finally:
        await _cleanup(engine, world.user)


# --- the lock-order question ---------------------------------------------------------------------


async def test_a_loser_holding_the_cv_when_an_erasure_arrives_neither_deadlocks_nor_leaves_rows(
    settings: Settings, engine: AsyncEngine, clock: FixedClock
) -> None:
    """A winner is committed. A holder keeps the posting `FOR UPDATE`, so the loser (which inserts
    nothing and so holds no owner lock) takes the CV and then waits at the posting: the state the
    plan's sentence does not cover. The erasure's cascade then needs that CV and waits behind the loser.
    The holder lets go: the loser gets the posting, re-reads the winner, commits; the erasure then
    takes everything. A cycle would surface as `DeadlockDetectedError` from one of the tasks."""
    assert_test_database(settings)
    world = await _world(engine, clock)
    try:
        async with pinned_session(engine) as seeding:
            repo = SqlAlchemyFitScoreRepository(seeding)
            winner = _score(repo, world, clock, score=62)
            await repo.store(winner)
            await seeding.commit()

        async with (
            pinned_session(engine) as holder,
            pinned_session(engine) as loser_session,
            pinned_session(engine) as erasure,
        ):
            await holder.execute(
                text("SELECT id FROM posting_job_posting WHERE id = :p FOR UPDATE"),
                {"p": world.posting},
            )
            loser_repo = SqlAlchemyFitScoreRepository(loser_session)
            loser = asyncio.create_task(
                loser_repo.store(_score(loser_repo, world, clock, score=17))
            )
            await wait_for_lock_waiter(engine, "posting_job_posting", "for key share")

            erasing = asyncio.create_task(
                erasure.execute(
                    text("DELETE FROM identity_user WHERE id = :u"), {"u": world.user.value}
                )
            )
            await wait_for_lock_waiter(engine, "delete from identity_user")
            assert not loser.done()
            assert not erasing.done()

            await holder.rollback()
            returned = await asyncio.wait_for(loser, _STEP_TIMEOUT)
            await loser_session.commit()
            await asyncio.wait_for(erasing, _STEP_TIMEOUT)
            await erasure.commit()

        assert returned.id == winner.id
        assert await _count(engine, "identity_user", "id", world.user.value) == 0
        assert await _count(engine, "scoring_fit_score", "user_id", world.user.value) == 0
        assert await _count(engine, "intake_base_cv", "id", world.cv) == 0
        assert await _count(engine, "posting_job_posting", "id", world.posting) == 0
    finally:
        await _cleanup(engine, world.user)


async def test_a_loser_arriving_while_an_erasure_holds_the_cv_answers_the_cvs_404(
    settings: Settings, engine: AsyncEngine, clock: FixedClock
) -> None:
    """The opposite arrival. A holder keeps the posting `FOR KEY SHARE`, so the erasure (which deletes
    the CV first) holds the CV and waits at the posting. The loser then needs the CV and waits behind the
    erasure. The holder lets go; the erasure commits; the loser finds the CV gone and raises
    `BaseCvNotFound` rather than handing back a row about a deleted CV."""
    assert_test_database(settings)
    world = await _world(engine, clock)
    try:
        async with pinned_session(engine) as seeding:
            repo = SqlAlchemyFitScoreRepository(seeding)
            await repo.store(_score(repo, world, clock, score=62))
            await seeding.commit()

        async with (
            pinned_session(engine) as holder,
            pinned_session(engine) as erasure,
            pinned_session(engine) as loser_session,
        ):
            await holder.execute(
                text("SELECT id FROM posting_job_posting WHERE id = :p FOR KEY SHARE"),
                {"p": world.posting},
            )
            erasing = asyncio.create_task(
                erasure.execute(
                    text("DELETE FROM identity_user WHERE id = :u"), {"u": world.user.value}
                )
            )
            await wait_for_lock_waiter(engine, "delete from identity_user")

            loser_repo = SqlAlchemyFitScoreRepository(loser_session)
            loser = asyncio.create_task(
                loser_repo.store(_score(loser_repo, world, clock, score=17))
            )
            await wait_for_lock_waiter(engine, "intake_base_cv", "for key share")
            assert not erasing.done()
            assert not loser.done()

            await holder.rollback()
            await asyncio.wait_for(erasing, _STEP_TIMEOUT)
            await erasure.commit()
            with pytest.raises(BaseCvNotFound):
                await asyncio.wait_for(loser, _STEP_TIMEOUT)
            await loser_session.rollback()

        assert await _count(engine, "scoring_fit_score", "user_id", world.user.value) == 0
    finally:
        await _cleanup(engine, world.user)


@pytest.mark.parametrize("owner_table", ["identity_user", "identity_guest_session"])
async def test_an_owners_cascade_deletes_the_cv_before_the_posting(
    session: AsyncSession, owner_table: str
) -> None:
    """The premise of the two tests above, read from the catalogue: `store` takes the CV and then the
    posting, so the cascade must too. Triggers fire in name order (`RI_ConstraintTrigger_a_<oid>`, i.e.
    FK creation order, in the byte order Postgres sorts them by). A migration that created the posting's
    FK first would invert this, and nothing else would notice until two requests met in production."""
    tables = (
        (
            await session.execute(
                text(
                    "SELECT t.tgconstrrelid::regclass::text FROM pg_trigger t "
                    "WHERE t.tgrelid = CAST(:owner AS regclass) AND t.tgisinternal "
                    'AND (t.tgtype & 8) = 8 ORDER BY t.tgname COLLATE "C"'
                ),
                {"owner": owner_table},
            )
        )
        .scalars()
        .all()
    )

    assert "intake_base_cv" in tables
    assert "posting_job_posting" in tables
    assert tables.index("intake_base_cv") < tables.index("posting_job_posting"), tables
