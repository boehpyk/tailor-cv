"""A fit score being stored while its CV or its posting is being deleted, **both orders**, on real,
independent connections (slice 5.1, T23; AC-37, AC-38). `test_fit_score_races.py` covers the store
against an account erasure; this is the same question for the two deletes that carry their own
`DELETE` of the scores.

A score has no foreign key to its CV or posting (ADR-0014), so nothing but the order of statements and
the two `FOR KEY SHARE` locks stops one being left behind. A score left behind is a stranger's CV
text's summary (its gaps) about a CV the user deleted. The two orders:

- **The store is first** (its `INSERT` and both locks are held, uncommitted). The delete's `DELETE` of
  the CV (or `SELECT … FOR UPDATE` of the posting) waits on the locks. The store commits; the delete
  proceeds; its `DELETE` of the scores is a *new statement* with a fresh snapshot, so it sees the row.
  Nothing is left. A delete that removed the scores **before** the CV (or posting) would pass the
  other order and fail this one.
- **The delete is first** (the CV or posting is deleted, uncommitted, the scores already deleted). The
  store's `INSERT` goes in and its lock waits on the delete. The delete commits; the store finds the
  CV (or posting) gone and is **refused** (`BaseCvNotFound` / `JobPostingNotFound`), rolling its
  `INSERT` back inside the savepoint. Nothing is left.

Every overlap is proven from `pg_stat_activity` (`wait_for_lock_waiter`), never a sleep; the held
state of the delete in the second order is staged by gating the removal's own `commit`, so the real
`remove` runs to the instant before it is durable. Rows are real and committed; teardown deletes the
user, whose cascades take the rest.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import cast
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from tailorcraft.domain.identity.ownership import UserOwner
from tailorcraft.domain.identity.value_objects import UserId
from tailorcraft.domain.intake.errors import BaseCvNotFound
from tailorcraft.domain.intake.value_objects import BaseCvId
from tailorcraft.domain.posting.errors import JobPostingNotFound
from tailorcraft.domain.scoring.fit_score import FitScore
from tailorcraft.domain.scoring.value_objects import (
    CvFingerprint,
    MicroUsd,
    PostingFingerprint,
    ScoredCvRef,
    ScoredPostingRef,
)
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
from tailorcraft.infrastructure.persistence.retention.history_entry_data import (
    SqlAlchemyHistoryEntryData,
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
from tests.integration.owners import extracted_cv, pasted_posting, succeeded_run
from tests.integration.scoring.support import assessed

_STEP_TIMEOUT = 15.0


@dataclass(frozen=True, slots=True)
class _World:
    user: UserId
    cv: UUID
    posting: UUID
    run: UUID

    @property
    def owner(self) -> UserOwner:
        return UserOwner(self.user)


async def _world(engine: AsyncEngine, clock: FixedClock) -> _World:
    user = await new_user(engine, clock)
    owner = UserOwner(user)
    cv = extracted_cv(owner, clock.now())
    posting = pasted_posting(owner, clock.now())
    run = succeeded_run(owner, clock.now(), base_cv_id=cv.id, job_posting_id=posting.id)
    async with async_sessionmaker(engine, expire_on_commit=False)() as seeding:
        await SqlAlchemyBaseCvRepository(seeding).add(cv)
        await SqlAlchemyJobPostingRepository(seeding).add(posting)
        await SqlAlchemyTailoringRunRepository(seeding).add(run)
        await seeding.commit()
    return _World(user=user, cv=cv.id.value, posting=posting.id.value, run=run.id.value)


def _score(repo: SqlAlchemyFitScoreRepository, world: _World, clock: FixedClock) -> FitScore:
    built = FitScore.from_assessment(
        id=repo.next_identity(),
        owner=world.owner,
        base_cv_id=ScoredCvRef(world.cv),
        job_posting_id=ScoredPostingRef(world.posting),
        cv_fingerprint=CvFingerprint(f"{1:064x}"),
        posting_fingerprint=PostingFingerprint(f"{2:064x}"),
        assessed=assessed(score=62),
        cost=MicroUsd(230),
        at=clock.now(),
    )
    built.release_events()
    return built


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


async def _scores_left(engine: AsyncEngine, world: _World) -> int:
    return await _count(engine, "scoring_fit_score", "user_id", world.user.value)


async def _cleanup(engine: AsyncEngine, user: UserId) -> None:
    async with engine.begin() as conn:
        await bound_cleanup_locks(conn)
    await drop_rows(engine, users=[user])


class _GatedCommit:
    """An `AsyncSession` whose `commit` first reports it has been reached and waits to be released:
    the real `CommittingBaseCvRemoval.remove` then stops after its `DELETE`s and before they are
    durable. Everything else is the wrapped session's."""

    def __init__(self, inner: AsyncSession) -> None:
        self._inner = inner
        self.reached = asyncio.Event()
        self.release = asyncio.Event()

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)

    async def commit(self) -> None:
        self.reached.set()
        await self.release.wait()
        await self._inner.commit()


# --- AC-37: a saved CV ---------------------------------------------------------------------------


async def test_ac37_a_store_in_flight_when_the_cv_is_deleted_leaves_no_score(
    settings: Settings, engine: AsyncEngine, clock: FixedClock
) -> None:
    """The store is first. The removal's `DELETE` of the CV waits on its `FOR KEY SHARE`; once the
    store commits, the removal's own `DELETE` of the scores finds the new row."""
    assert_test_database(settings)
    world = await _world(engine, clock)
    try:
        async with pinned_session(engine) as storing, pinned_session(engine) as removing:
            repo = SqlAlchemyFitScoreRepository(storing)
            await repo.store(_score(repo, world, clock))  # uncommitted, both locks held

            removal = CommittingBaseCvRemoval(SqlAlchemyBaseCvRepository(removing), removing)
            pending = asyncio.create_task(removal.remove(BaseCvId(world.cv), world.owner))
            await wait_for_lock_waiter(engine, "delete from intake_base_cv")
            assert not pending.done(), "the removal must wait for the store's lock on the CV"
            await storing.commit()

            await asyncio.wait_for(pending, _STEP_TIMEOUT)

        assert await _count(engine, "intake_base_cv", "id", world.cv) == 0
        assert await _scores_left(engine, world) == 0, "a score survived its CV"
    finally:
        await _cleanup(engine, world.user)


async def test_ac37_a_store_arriving_while_the_cv_is_being_deleted_is_refused(
    settings: Settings, engine: AsyncEngine, clock: FixedClock
) -> None:
    """The removal is first: it has deleted the CV and the scores and is stopped at its commit. The
    store's `INSERT` goes in; its lock on the CV waits; the removal commits; the store finds no CV and
    is refused, its `INSERT` rolled back with the savepoint."""
    assert_test_database(settings)
    world = await _world(engine, clock)
    try:
        async with pinned_session(engine) as removing, pinned_session(engine) as storing:
            gated = _GatedCommit(removing)
            removal = CommittingBaseCvRemoval(
                SqlAlchemyBaseCvRepository(removing), cast("AsyncSession", gated)
            )
            removing_task = asyncio.create_task(removal.remove(BaseCvId(world.cv), world.owner))
            await asyncio.wait_for(gated.reached.wait(), _STEP_TIMEOUT)

            repo = SqlAlchemyFitScoreRepository(storing)
            storing_task = asyncio.create_task(repo.store(_score(repo, world, clock)))
            await wait_for_lock_waiter(engine, "intake_base_cv", "for key share")
            assert not storing_task.done()
            gated.release.set()

            await asyncio.wait_for(removing_task, _STEP_TIMEOUT)
            with pytest.raises(BaseCvNotFound):
                await asyncio.wait_for(storing_task, _STEP_TIMEOUT)
            await storing.rollback()

        assert await _count(engine, "intake_base_cv", "id", world.cv) == 0
        assert await _scores_left(engine, world) == 0, "a score was stored for a deleted CV"
    finally:
        await _cleanup(engine, world.user)


# --- AC-38: a history entry that takes its posting -----------------------------------------------


async def test_ac38_a_store_in_flight_when_the_posting_is_deleted_leaves_no_score(
    settings: Settings, engine: AsyncEngine, clock: FixedClock
) -> None:
    """The store is first. The entry's deletion locks the posting `FOR UPDATE` and waits on the
    store's `FOR KEY SHARE`; after the commit its `DELETE` of the scores finds the new row."""
    assert_test_database(settings)
    world = await _world(engine, clock)
    try:
        async with pinned_session(engine) as storing, pinned_session(engine) as erasing:
            repo = SqlAlchemyFitScoreRepository(storing)
            await repo.store(_score(repo, world, clock))  # uncommitted, both locks held

            pending = asyncio.create_task(
                SqlAlchemyHistoryEntryData(erasing).delete_history_entry(world.user, world.run)
            )
            await wait_for_lock_waiter(engine, "posting_job_posting", "for update")
            assert not pending.done(), "the deletion must wait for the store's lock on the posting"
            await storing.commit()

            deleted = await asyncio.wait_for(pending, _STEP_TIMEOUT)
            await erasing.commit()

        assert deleted is not None
        assert deleted.posting_deleted is True
        assert await _count(engine, "posting_job_posting", "id", world.posting) == 0
        assert await _scores_left(engine, world) == 0, "a score survived its posting"
    finally:
        await _cleanup(engine, world.user)


async def test_ac38_a_store_arriving_while_the_posting_is_being_deleted_is_refused(
    settings: Settings, engine: AsyncEngine, clock: FixedClock
) -> None:
    """The deletion is first: the run, the posting and the scores are deleted and uncommitted. The
    store's lock on the posting waits; the deletion commits; the store finds no posting and is refused."""
    assert_test_database(settings)
    world = await _world(engine, clock)
    try:
        async with pinned_session(engine) as erasing, pinned_session(engine) as storing:
            deleted = await SqlAlchemyHistoryEntryData(erasing).delete_history_entry(
                world.user, world.run
            )
            assert deleted is not None
            assert deleted.posting_deleted is True

            repo = SqlAlchemyFitScoreRepository(storing)
            storing_task = asyncio.create_task(repo.store(_score(repo, world, clock)))
            await wait_for_lock_waiter(engine, "posting_job_posting", "for key share")
            assert not storing_task.done()
            await erasing.commit()

            with pytest.raises(JobPostingNotFound):
                await asyncio.wait_for(storing_task, _STEP_TIMEOUT)
            await storing.rollback()

        assert await _count(engine, "posting_job_posting", "id", world.posting) == 0
        assert await _scores_left(engine, world) == 0, "a score was stored for a deleted posting"
    finally:
        await _cleanup(engine, world.user)
