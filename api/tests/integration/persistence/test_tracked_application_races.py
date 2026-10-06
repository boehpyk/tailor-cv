"""AC-18 and AC-19's persistence halves: a card racing a history-entry deletion, an account erasure and
another track of the same run, **on real, independent connections** (slice 3.1, T19, test-after).

`tracking_application.tailoring_run_id` has no foreign key (ADR-0014/0016/0023), so nothing in the
schema stops a card landing on a run a deletion is removing. Two locks close it (plan §0.7): `add`
INSERTs and **then** takes the run's row; the deletion's run `DELETE` conflicts with that lock, and
its card `DELETE` is a **separate statement** so a fresh READ COMMITTED snapshot, taken after the
wait, sees the card the track committed meanwhile. Each test stages the interleaving the spec names:

- **(a) track first** — the card's transaction holds the run; the deletion's run `DELETE` is
  observed **waiting** (`pg_stat_activity`); the card commits; the deletion finishes **and reports
  it took a card**: no card survives its run.
- **(b) delete first** — the deletion holds the run's deleted row; the track's run read is observed
  **waiting**; the deletion commits; the track finds nothing and raises `TailoringRunNotFound`, with
  no row. Observed from a separate connection, not from the aggregate.
- **AC-19** — a card `INSERT` racing an erasure waits on the user row and is refused on
  `fk_tracking_application_user_id_identity_user` → `UserNotFound`; two tracks of one run make the
  second wait on the unique index and then raise `ApplicationAlreadyTracked` naming the winner.
  (The HTTP half of AC-19 — 401 `not_signed_in`, 201 / 409 — needs T20/T23's route and belongs to
  T21.)

**Every overlap is proven**, not assumed from a sleep: `wait_for_lock_waiter` reads `pg_stat_activity`
from a third connection until a backend with `wait_event_type = 'Lock'` runs the statement the spec
names. **Every wait is bounded**: both connections are pinned with `lock_timeout` and
`statement_timeout` set and the `SET` committed (a bare `SET` on a pooled connection does not survive
a commit — 1.6's twenty-minute hang), each task is awaited under `asyncio.wait_for`, and teardown's
deletes carry their own `SET LOCAL lock_timeout`.

**Observed red, 2026-10-05, and restored byte-exact** (`git diff --stat -- api/src` empty): with the
run's `.with_for_update(…)` deleted from `SqlAlchemyTrackedApplicationRepository.add`, (a) and (b) both
fail on `no backend was ever waiting on a lock in a statement containing …` — the race is never
staged because the track holds nothing the deletion needs. Green again after the restore.

Rows are real and committed; every assertion is scoped to ids this test created, and teardown deletes
the user (the cascades take the runs, postings and cards).
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from tailorcraft.domain.identity.errors import UserNotFound
from tailorcraft.domain.identity.ownership import UserOwner
from tailorcraft.domain.identity.value_objects import UserId
from tailorcraft.domain.tailoring.errors import TailoringRunNotFound
from tailorcraft.domain.tailoring.value_objects import TailoringRunId
from tailorcraft.domain.tracking.errors import ApplicationAlreadyTracked
from tailorcraft.domain.tracking.tracked_application import TrackedApplication
from tailorcraft.infrastructure.clock import FixedClock
from tailorcraft.infrastructure.persistence.repositories.tracking.tracked_application import (
    SqlAlchemyTrackedApplicationRepository,
)
from tailorcraft.infrastructure.persistence.retention.account_data import SqlAlchemyAccountData
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
from tests.integration.tracking.support import a_card, seed_run

_STEP_TIMEOUT = 15.0


async def _seed_run(engine: AsyncEngine, clock: FixedClock, user_id: UserId) -> TailoringRunId:
    async with async_sessionmaker(engine, expire_on_commit=False)() as seeding:
        run = await seed_run(seeding, UserOwner(user_id), clock.now(), "succeeded")
        return run.id


def _card(
    repo: SqlAlchemyTrackedApplicationRepository,
    clock: FixedClock,
    user_id: UserId,
    run_id: TailoringRunId,
) -> TrackedApplication:
    return a_card(user_id, clock.now(), run_id=run_id, card_id=repo.next_identity())


async def _fresh_count(engine: AsyncEngine, sql: str, **params: Any) -> int:
    """A count read on a brand-new connection: what a different request would see."""
    async with engine.connect() as probe:
        return int((await probe.execute(text(sql), params)).scalar_one())


async def _cards_for_run(engine: AsyncEngine, run_id: TailoringRunId) -> int:
    return await _fresh_count(
        engine,
        "SELECT count(*) FROM tracking_application WHERE tailoring_run_id = :r",
        r=run_id.value,
    )


async def _runs(engine: AsyncEngine, run_id: TailoringRunId) -> int:
    return await _fresh_count(
        engine, "SELECT count(*) FROM tailoring_run WHERE id = :r", r=run_id.value
    )


async def _cleanup(engine: AsyncEngine, *users: UserId) -> None:
    async with engine.begin() as conn:
        await bound_cleanup_locks(conn)
    await drop_rows(engine, users=list(users))


# --- AC-18 (a): the track holds the run first ----------------------------------------------------


async def test_ac18a_a_deletion_that_waited_for_a_track_takes_the_card_it_committed(
    settings: Settings, engine: AsyncEngine, clock: FixedClock
) -> None:
    """The card's transaction holds the run → the deletion's run `DELETE` waits → the card commits →
    the deletion's **separate** card `DELETE` sees it. Folding the card into the run's statement
    would read the pre-wait snapshot, miss the card, and leave one pointing at a deleted run — the
    `tracked_application_deleted` assertion and the fresh count are what go red."""
    assert_test_database(settings)
    user_id = await new_user(engine, clock)
    try:
        run_id = await _seed_run(engine, clock, user_id)
        async with pinned_session(engine) as tracker, pinned_session(engine) as deleter:
            repo = SqlAlchemyTrackedApplicationRepository(tracker)
            card = _card(repo, clock, user_id, run_id)
            await repo.add(card)  # INSERT, then the run's lock; not committed
            assert await _cards_for_run(engine, run_id) == 0, "the track must still be uncommitted"

            deletion = asyncio.create_task(
                SqlAlchemyHistoryEntryData(deleter).delete_history_entry(user_id, run_id.value)
            )
            await wait_for_lock_waiter(engine, "delete from tailoring_run")
            assert not deletion.done(), "the run DELETE must be waiting for the track's lock"
            await tracker.commit()

            deleted = await asyncio.wait_for(deletion, _STEP_TIMEOUT)
            assert deleted is not None
            assert deleted.tracked_application_deleted is True, (
                "the card committed while the deletion waited, and a fresh snapshot must see it"
            )
            await deleter.commit()

        assert await _runs(engine, run_id) == 0
        assert await _cards_for_run(engine, run_id) == 0, "no card may survive its run"
    finally:
        await _cleanup(engine, user_id)


# --- AC-18 (b): the deletion holds the run first -------------------------------------------------


async def test_ac18b_a_track_that_waited_for_a_deletion_finds_no_run_and_leaves_no_row(
    settings: Settings, engine: AsyncEngine, clock: FixedClock
) -> None:
    """The deletion deletes the run, uncommitted → the track's `INSERT` goes through (no FK on the
    run) and its run read **waits** → the deletion commits → the read finds nothing →
    `TailoringRunNotFound`, the SAVEPOINT rolled back, no row."""
    assert_test_database(settings)
    user_id = await new_user(engine, clock)
    try:
        run_id = await _seed_run(engine, clock, user_id)
        async with pinned_session(engine) as tracker, pinned_session(engine) as deleter:
            deleted_first = await SqlAlchemyHistoryEntryData(deleter).delete_history_entry(
                user_id, run_id.value
            )
            assert deleted_first is not None
            assert deleted_first.tracked_application_deleted is False

            repo = SqlAlchemyTrackedApplicationRepository(tracker)
            card = _card(repo, clock, user_id, run_id)
            tracking = asyncio.create_task(repo.add(card))
            await wait_for_lock_waiter(engine, "tailoring_run", "select")
            assert not tracking.done(), "the track's run read must be waiting for the deletion"
            await deleter.commit()

            with pytest.raises(TailoringRunNotFound):
                await asyncio.wait_for(tracking, _STEP_TIMEOUT)
            await tracker.commit()

        assert await _runs(engine, run_id) == 0
        assert await _cards_for_run(engine, run_id) == 0, "the refused card must leave no row"
    finally:
        await _cleanup(engine, user_id)


async def test_ac18_without_a_race_the_same_track_succeeds_and_the_deletion_takes_it(
    settings: Settings, engine: AsyncEngine, clock: FixedClock
) -> None:
    """The control for both orders: committed in sequence, the track lands and the deletion's report
    says it took the card — so a `False` above means "no card was there", never "the field is
    always False"."""
    assert_test_database(settings)
    user_id = await new_user(engine, clock)
    try:
        run_id = await _seed_run(engine, clock, user_id)
        async with pinned_session(engine) as tracker:
            repo = SqlAlchemyTrackedApplicationRepository(tracker)
            await repo.add(_card(repo, clock, user_id, run_id))
            await tracker.commit()
        assert await _cards_for_run(engine, run_id) == 1

        async with pinned_session(engine) as deleter:
            deleted = await SqlAlchemyHistoryEntryData(deleter).delete_history_entry(
                user_id, run_id.value
            )
            await deleter.commit()

        assert deleted is not None
        assert deleted.tracked_application_deleted is True
        assert await _cards_for_run(engine, run_id) == 0
    finally:
        await _cleanup(engine, user_id)


# --- AC-19: erasure, and two tracks of one run ---------------------------------------------------


async def test_ac19_a_card_insert_racing_an_erasure_waits_then_is_refused_with_no_row(
    settings: Settings, engine: AsyncEngine, clock: FixedClock
) -> None:
    """`SqlAlchemyAccountData.files_of_account` takes the user row `FOR UPDATE`; the card `INSERT`'s
    FK check needs `FOR KEY SHARE` on it, so it **waits** (observed in `pg_stat_activity`); once the
    erasure commits, the row is gone and the FK refuses → `UserNotFound` (the 401 `not_signed_in`
    over HTTP, T21). No card is left behind."""
    assert_test_database(settings)
    user_id = await new_user(engine, clock)
    run_id = await _seed_run(engine, clock, user_id)
    try:
        async with pinned_session(engine) as eraser, pinned_session(engine) as tracker:
            accounts = SqlAlchemyAccountData(eraser)
            await accounts.files_of_account(user_id)  # holds the user row FOR UPDATE

            repo = SqlAlchemyTrackedApplicationRepository(tracker)
            tracking = asyncio.create_task(repo.add(_card(repo, clock, user_id, run_id)))
            await wait_for_lock_waiter(engine, "insert into tracking_application")
            assert not tracking.done(), "the INSERT must wait on the erasure's user-row lock"

            assert await accounts.delete_account(user_id) is True
            await eraser.commit()

            with pytest.raises(UserNotFound):
                await asyncio.wait_for(tracking, _STEP_TIMEOUT)
            await tracker.rollback()

        assert await _cards_for_run(engine, run_id) == 0
        assert (
            await _fresh_count(
                engine, "SELECT count(*) FROM identity_user WHERE id = :u", u=user_id.value
            )
            == 0
        ), "the erasure must have committed"
    finally:
        await _cleanup(engine, user_id)


async def test_ac19_two_tracks_of_one_run_make_the_second_wait_then_name_the_winner(
    settings: Settings, engine: AsyncEngine, clock: FixedClock
) -> None:
    """The unique index serialises them: the second `INSERT` waits for the first's transaction and,
    once it commits, is refused on `uq_tracking_application_tailoring_run_id` →
    `ApplicationAlreadyTracked(winner's id)` — one row, no other error."""
    assert_test_database(settings)
    user_id = await new_user(engine, clock)
    run_id = await _seed_run(engine, clock, user_id)
    try:
        async with pinned_session(engine) as first, pinned_session(engine) as second:
            first_repo = SqlAlchemyTrackedApplicationRepository(first)
            winner = _card(first_repo, clock, user_id, run_id)
            winner_id = winner.id
            await first_repo.add(winner)  # uncommitted

            second_repo = SqlAlchemyTrackedApplicationRepository(second)
            loser = _card(second_repo, clock, user_id, run_id)
            racing = asyncio.create_task(second_repo.add(loser))
            await wait_for_lock_waiter(engine, "insert into tracking_application")
            assert not racing.done(), "the second INSERT must wait on the unique index"
            await first.commit()

            with pytest.raises(ApplicationAlreadyTracked) as raised:
                await asyncio.wait_for(racing, _STEP_TIMEOUT)
            assert raised.value.existing_id == winner_id
            await second.rollback()

        assert await _cards_for_run(engine, run_id) == 1
    finally:
        await _cleanup(engine, user_id)


async def test_ac19_tracks_of_two_different_runs_do_not_contend(
    settings: Settings, engine: AsyncEngine, clock: FixedClock
) -> None:
    """The control for the wait above: two tracks of **different** runs do not contend, so the
    second returns while the first is still uncommitted — a second `INSERT` that waited here would
    mean the first test proves nothing about the unique index."""
    assert_test_database(settings)
    user_id = await new_user(engine, clock)
    run_one = await _seed_run(engine, clock, user_id)
    run_two = await _seed_run(engine, clock, user_id)
    try:
        async with pinned_session(engine) as first, pinned_session(engine) as second:
            first_repo = SqlAlchemyTrackedApplicationRepository(first)
            await first_repo.add(_card(first_repo, clock, user_id, run_one))  # uncommitted

            second_repo = SqlAlchemyTrackedApplicationRepository(second)
            await asyncio.wait_for(
                second_repo.add(_card(second_repo, clock, user_id, run_two)), _STEP_TIMEOUT
            )
            await second.commit()
            await first.commit()

        assert await _cards_for_run(engine, run_one) == 1
        assert await _cards_for_run(engine, run_two) == 1
    finally:
        await _cleanup(engine, user_id)


# --- a card has no path to a stranger's run (the lock's owner predicate, on real rows) -----------


async def test_a_track_over_another_users_committed_run_is_refused_and_leaves_no_row(
    settings: Settings, engine: AsyncEngine, clock: FixedClock
) -> None:
    assert_test_database(settings)
    owner = await new_user(engine, clock)
    stranger = await new_user(engine, clock)
    their_run = await _seed_run(engine, clock, stranger)
    try:
        async with pinned_session(engine) as session:
            repo = SqlAlchemyTrackedApplicationRepository(session)
            with pytest.raises(TailoringRunNotFound):
                await repo.add(_card(repo, clock, owner, their_run))
            await session.commit()

        assert await _cards_for_run(engine, their_run) == 0
        assert await _runs(engine, their_run) == 1, "the stranger's run is untouched"
    finally:
        await _cleanup(engine, owner, stranger)
