"""`SqlAlchemyTrackedApplicationRepository` against a real PostgreSQL (slice 3.1, T19, test-after;
AC-15's round-trip, AC-17, AC-20, the port's `get` / `find_for_run` / `count_for_user` / `remove`).

Rows are real (`tailorcraft_test`, the rolled-back `session` fixture); seeds commit (2.3's lesson:
on the `create_savepoint` session a commit releases a savepoint and the outer rollback still
isolates the test). The two-connection races are in `test_tracked_application_races.py`.

**Statement capture** proves what reached the driver where the *order* is the claim: `add` is the
`INSERT` and **then** the run's `FOR KEY SHARE` (AC-17) — an order inverted, or a lock that silently
became a plain `SELECT`, would still answer the right exception in a single-connection test.

**Every refusal asserts "nothing inserted"** from the catalogue (`count(*)` scoped to ids this test
created), and the SAVEPOINT's promise — only the pending card is discarded, the session stays usable —
is asserted by flushing another write afterwards.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from tailorcraft.domain.identity.errors import UserNotFound
from tailorcraft.domain.identity.value_objects import UserId
from tailorcraft.domain.tailoring.errors import TailoringRunNotFound
from tailorcraft.domain.tailoring.value_objects import TailoringRunId
from tailorcraft.domain.tracking.errors import (
    ApplicationAlreadyTracked,
    TrackedApplicationConcurrentlyModified,
    TrackedApplicationNotFound,
)
from tailorcraft.domain.tracking.tracked_application import TrackedApplication
from tailorcraft.domain.tracking.value_objects import (
    ApplicationStage,
    ApplicationTitle,
    TrackedApplicationId,
    TrackedRunRef,
)
from tailorcraft.infrastructure.clock import FixedClock
from tailorcraft.infrastructure.persistence.repositories.tracking.tracked_application import (
    SqlAlchemyTrackedApplicationRepository,
)
from tests.integration.persistence.owner_rows import persist_user
from tests.integration.tracking.support import a_card, seed_run


@contextmanager
def _captured_statements(connection: AsyncConnection) -> Iterator[list[str]]:
    seen: list[str] = []

    def _record(_conn: Any, _cursor: Any, statement: str, *_rest: Any) -> None:
        seen.append(statement)

    event.listen(connection.sync_connection, "before_cursor_execute", _record)
    try:
        yield seen
    finally:
        event.remove(connection.sync_connection, "before_cursor_execute", _record)


async def _count(session: AsyncSession, **where: object) -> int:
    clause = " AND ".join(f"{column} = :{column}" for column in where)
    return int(
        (
            await session.execute(
                text(f"SELECT count(*) FROM tracking_application WHERE {clause}"),  # noqa: S608 -- test-owned names
                where,
            )
        ).scalar_one()
    )


async def _raw_version(session: AsyncSession, card_id: TrackedApplicationId) -> int:
    return int(
        (
            await session.execute(
                text("SELECT version FROM tracking_application WHERE id = :i"),
                {"i": card_id.value},
            )
        ).scalar_one()
    )


async def _tracked(
    session: AsyncSession,
    clock: FixedClock,
    *,
    stage: ApplicationStage = ApplicationStage.TO_APPLY,
    title: ApplicationTitle | None = None,
) -> tuple[SqlAlchemyTrackedApplicationRepository, TrackedApplication, UserId]:
    """A user, a succeeded run of theirs and a card over it, added and flushed."""
    owner = await persist_user(session, clock)
    run = await seed_run(session, owner, clock.now(), "succeeded")
    repo = SqlAlchemyTrackedApplicationRepository(session)
    card = a_card(
        owner.user_id,
        clock.now(),
        stage=stage,
        title=title,
        run_id=run.id,
        card_id=repo.next_identity(),
    )
    await repo.add(card)
    await session.flush()
    return repo, card, owner.user_id


# --- the mapping: round trip, whole-second fidelity, the domain's version (AC-15) ----------------


async def test_a_card_round_trips_every_field_through_a_fresh_load(
    session: AsyncSession, clock: FixedClock
) -> None:
    repo, card, user_id = await _tracked(
        session, clock, stage=ApplicationStage.INTERVIEWING, title=ApplicationTitle("Acme — Staff")
    )
    snapshot = (
        card.id,
        card.user_id,
        card.tailoring_run_id,
        card.stage,
        card.title,
        card.tracked_at,
        card.stage_changed_at,
        card.version,
    )
    session.expunge_all()

    loaded = await repo.get(snapshot[0])

    assert loaded is not card, "the test must read the row back, not the identity map"
    assert (
        loaded.id,
        loaded.user_id,
        loaded.tailoring_run_id,
        loaded.stage,
        loaded.title,
        loaded.tracked_at,
        loaded.stage_changed_at,
        loaded.version,
    ) == snapshot
    assert loaded.user_id == user_id
    assert type(loaded.stage) is ApplicationStage
    assert type(loaded.title) is ApplicationTitle
    assert type(loaded.tailoring_run_id) is TrackedRunRef
    assert type(loaded.id) is TrackedApplicationId
    assert not loaded.release_events(), "a loaded card holds no pending event"


async def test_a_card_without_a_title_round_trips_as_none(
    session: AsyncSession, clock: FixedClock
) -> None:
    repo, card, _ = await _tracked(session, clock, title=None)
    card_id = card.id
    session.expunge_all()

    assert (await repo.get(card_id)).title is None


async def test_instants_survive_the_database_round_trip_to_the_second(
    session: AsyncSession, clock: FixedClock
) -> None:
    """The `Clock` is whole-second by contract and the columns are `TIMESTAMPTZ(0)`: what the
    aggregate held is, equal and tz-aware, what the row hands back."""
    repo, card, _ = await _tracked(session, clock)
    at = clock.now()
    card_id = card.id
    session.expunge_all()

    loaded = await repo.get(card_id)

    assert loaded.tracked_at == at
    assert loaded.stage_changed_at == at
    assert loaded.tracked_at.tzinfo is not None
    assert loaded.tracked_at.microsecond == 0


async def test_a_moved_card_is_saved_with_the_domains_version_not_the_mappers(
    session: AsyncSession, clock: FixedClock
) -> None:
    """`version_id_generator=False`: the aggregate bumped 1 → 2 once, and the row says 2 — with the
    default generator SQLAlchemy would add its own bump and the two would drift apart by one."""
    repo, card, _ = await _tracked(session, clock)
    card_id = card.id
    assert await _raw_version(session, card_id) == 1

    card.move_to(
        ApplicationStage.APPLIED, expected_version=1, at=clock.now() + timedelta(seconds=30)
    )
    await repo.save(card)
    await session.flush()
    session.expunge_all()

    assert card.version == 2
    assert await _raw_version(session, card_id) == 2
    reloaded = await repo.get(card_id)
    assert (reloaded.version, reloaded.stage) == (2, ApplicationStage.APPLIED)
    assert reloaded.stage_changed_at == clock.now() + timedelta(seconds=30)
    assert reloaded.tracked_at == clock.now()


async def test_a_retitled_card_saves_its_new_title_and_a_cleared_one_saves_null(
    session: AsyncSession, clock: FixedClock
) -> None:
    repo, card, _ = await _tracked(session, clock, title=ApplicationTitle("Before"))
    card_id = card.id

    card.retitle(ApplicationTitle("After"), expected_version=1, at=clock.now())
    await repo.save(card)
    await session.flush()
    session.expunge_all()
    after = await repo.get(card_id)
    assert after.title == ApplicationTitle("After")

    after.retitle(None, expected_version=2, at=clock.now())
    await repo.save(after)
    await session.flush()
    session.expunge_all()
    cleared = await repo.get(card_id)
    assert (cleared.title, cleared.version) == (None, 3)


# --- add (AC-17) ---------------------------------------------------------------------------------


async def test_add_inserts_then_takes_the_runs_row_for_key_share_scoped_to_the_user(
    session: AsyncSession, connection: AsyncConnection, clock: FixedClock
) -> None:
    """AC-17's order, from the statements that reached the driver: the `INSERT` first (its FK check
    takes the user row — the erasure race's lock point), then `SELECT … FROM tailoring_run WHERE id
    AND user_id … FOR KEY SHARE`. Lock the run before the insert and erasure and a track make a
    cycle."""
    owner = await persist_user(session, clock)
    run = await seed_run(session, owner, clock.now(), "succeeded")
    repo = SqlAlchemyTrackedApplicationRepository(session)
    card = a_card(owner.user_id, clock.now(), run_id=run.id, card_id=repo.next_identity())

    with _captured_statements(connection) as statements:
        await repo.add(card)

    relevant = [s for s in statements if "tracking_application" in s or "FROM tailoring_run" in s]
    insert_at = next(
        i
        for i, s in enumerate(relevant)
        if s.lstrip().upper().startswith("INSERT INTO TRACKING_APPLICATION")
    )
    lock_at = next(
        i
        for i, s in enumerate(relevant)
        if "FROM tailoring_run" in s and "tracking_application" not in s
    )
    assert insert_at < lock_at, "the INSERT must come first, the run lock after it"
    lock = relevant[lock_at]
    assert " FOR " in lock.upper(), "the run read is a locking read"
    assert "user_id" in lock, "the run is the user's: an owner predicate is in the lock's WHERE"
    assert await _count(session, id=card.id.value) == 1


async def test_add_locks_the_run_in_the_mode_the_spec_names(
    session: AsyncSession, connection: AsyncConnection, clock: FixedClock
) -> None:
    owner = await persist_user(session, clock)
    run = await seed_run(session, owner, clock.now(), "succeeded")
    repo = SqlAlchemyTrackedApplicationRepository(session)
    card = a_card(owner.user_id, clock.now(), run_id=run.id, card_id=repo.next_identity())

    with _captured_statements(connection) as statements:
        await repo.add(card)

    (lock,) = [s for s in statements if "FROM tailoring_run" in s]
    assert lock.rstrip().upper().endswith("FOR KEY SHARE")


async def test_add_refuses_a_run_that_does_not_exist_and_leaves_no_row(
    session: AsyncSession, clock: FixedClock
) -> None:
    owner = await persist_user(session, clock)
    repo = SqlAlchemyTrackedApplicationRepository(session)
    card = a_card(
        owner.user_id, clock.now(), run_id=TailoringRunId(uuid4()), card_id=repo.next_identity()
    )
    card_id = card.id.value

    with pytest.raises(TailoringRunNotFound):
        await repo.add(card)

    assert await _count(session, id=card_id) == 0


async def test_add_refuses_another_users_run_and_leaves_no_row(
    session: AsyncSession, clock: FixedClock
) -> None:
    """The run exists, but not as this user's: the lock's `user_id` predicate finds nothing, so a
    card can never be filed over someone else's run, whatever the use case checked before."""
    owner = await persist_user(session, clock)
    stranger = await persist_user(session, clock)
    their_run = await seed_run(session, stranger, clock.now(), "succeeded")
    repo = SqlAlchemyTrackedApplicationRepository(session)
    card = a_card(owner.user_id, clock.now(), run_id=their_run.id, card_id=repo.next_identity())
    card_id = card.id.value

    with pytest.raises(TailoringRunNotFound):
        await repo.add(card)

    assert await _count(session, id=card_id) == 0
    assert await _count(session, tailoring_run_id=their_run.id.value) == 0


async def test_a_refused_add_rolls_back_only_its_own_savepoint(
    session: AsyncSession, clock: FixedClock
) -> None:
    """The SAVEPOINT's promise: the refusal discards the pending card and expires nothing else —
    an earlier card this session holds is still there, still loaded, and a later add still works."""
    repo, first, user_id = await _tracked(session, clock)
    first_id = first.id
    missing = a_card(
        user_id, clock.now(), run_id=TailoringRunId(uuid4()), card_id=repo.next_identity()
    )
    with pytest.raises(TailoringRunNotFound):
        await repo.add(missing)

    assert first.id == first_id, "the earlier card must not have been expired by the refusal"
    run_two = await seed_run(session, _owner_of(user_id), clock.now(), "succeeded")
    second = a_card(user_id, clock.now(), run_id=run_two.id, card_id=repo.next_identity())
    await repo.add(second)
    await session.flush()
    assert await _count(session, user_id=user_id.value) == 2


def _owner_of(user_id: UserId) -> Any:
    from tailorcraft.domain.identity.ownership import UserOwner

    return UserOwner(user_id)


async def test_add_of_an_already_tracked_run_names_the_winners_id_and_adds_nothing(
    session: AsyncSession, clock: FixedClock
) -> None:
    repo, winner, user_id = await _tracked(session, clock)
    loser = a_card(
        user_id,
        clock.now(),
        run_id=TailoringRunId(winner.tailoring_run_id.value),
        card_id=repo.next_identity(),
    )
    winner_id = winner.id
    loser_id = loser.id.value

    with pytest.raises(ApplicationAlreadyTracked) as raised:
        await repo.add(loser)

    assert raised.value.existing_id == winner_id
    assert raised.value.__cause__ is None, "raised `from None`: no frame holding the title"
    assert await _count(session, id=loser_id) == 0
    assert await _count(session, tailoring_run_id=winner.tailoring_run_id.value) == 1


async def test_add_for_a_user_that_does_not_exist_is_user_not_found_and_adds_nothing(
    session: AsyncSession, clock: FixedClock
) -> None:
    owner = await persist_user(session, clock)
    run = await seed_run(session, owner, clock.now(), "succeeded")
    repo = SqlAlchemyTrackedApplicationRepository(session)
    ghost = UserId(uuid4())
    card = a_card(ghost, clock.now(), run_id=run.id, card_id=repo.next_identity())
    card_id = card.id.value

    with pytest.raises(UserNotFound):
        await repo.add(card)

    assert await _count(session, id=card_id) == 0


# --- reads ---------------------------------------------------------------------------------------


async def test_get_of_an_unknown_id_is_not_found_with_no_cause(session: AsyncSession) -> None:
    repo = SqlAlchemyTrackedApplicationRepository(session)

    with pytest.raises(TrackedApplicationNotFound) as raised:
        await repo.get(TrackedApplicationId(uuid4()))

    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__ is True


async def test_get_does_not_check_ownership(session: AsyncSession, clock: FixedClock) -> None:
    """The port's rule: authorization is the use case's, so a card is handed out by id alone."""
    repo, card, _ = await _tracked(session, clock)
    card_id = card.id
    session.expunge_all()

    assert (await repo.get(card_id)).id == card_id


async def test_find_for_run_answers_the_users_card_and_none_for_another_user_or_run(
    session: AsyncSession, clock: FixedClock
) -> None:
    repo, card, user_id = await _tracked(session, clock)
    other = await persist_user(session, clock)

    found = await repo.find_for_run(user_id, card.tailoring_run_id)

    assert found is not None
    assert found.id == card.id
    assert await repo.find_for_run(other.user_id, card.tailoring_run_id) is None
    assert await repo.find_for_run(user_id, TrackedRunRef(uuid4())) is None


async def test_count_for_user_counts_only_that_users_cards(
    session: AsyncSession, clock: FixedClock
) -> None:
    repo, _, user_id = await _tracked(session, clock)
    owner = _owner_of(user_id)
    for _ in range(2):
        run = await seed_run(session, owner, clock.now(), "succeeded")
        await repo.add(a_card(user_id, clock.now(), run_id=run.id, card_id=repo.next_identity()))
    other_repo, _, other_user = await _tracked(session, clock)

    assert await repo.count_for_user(user_id) == 3
    assert await other_repo.count_for_user(other_user) == 1
    assert await repo.count_for_user(UserId(uuid4())) == 0


# --- save (AC-20) --------------------------------------------------------------------------------


async def test_save_with_a_stale_version_is_concurrently_modified_and_the_row_is_untouched(
    session: AsyncSession, clock: FixedClock
) -> None:
    """Another writer moved the row on to version 5 after this aggregate was loaded at 1. The flush's
    `UPDATE … WHERE id AND version = 1` matches nothing; `StaleDataError` never escapes."""
    repo, card, _ = await _tracked(session, clock)
    card_id = card.id
    await session.execute(
        text("UPDATE tracking_application SET version = 5, stage = 'offer' WHERE id = :i"),
        {"i": card_id.value},
    )
    card.move_to(
        ApplicationStage.APPLIED, expected_version=1, at=clock.now() + timedelta(seconds=5)
    )

    with pytest.raises(TrackedApplicationConcurrentlyModified) as raised:
        await repo.save(card)

    assert raised.value.__cause__ is None, "`from None`: SQLAlchemy's StaleDataError stays inside"
    assert type(raised.value).__module__.startswith("tailorcraft.domain")
    await session.rollback()  # the failed flush leaves the session to its caller (the docstring)


async def test_save_of_a_card_deleted_since_the_load_is_the_same_error(
    session: AsyncSession, clock: FixedClock
) -> None:
    repo, card, _ = await _tracked(session, clock)
    await session.execute(
        text("DELETE FROM tracking_application WHERE id = :i"), {"i": card.id.value}
    )
    card.retitle(ApplicationTitle("Too late"), expected_version=1, at=clock.now())

    with pytest.raises(TrackedApplicationConcurrentlyModified):
        await repo.save(card)

    await session.rollback()


async def test_save_with_the_current_version_succeeds_where_the_stale_one_fails(
    session: AsyncSession, clock: FixedClock
) -> None:
    """The positive control for the two refusals above: the same call shape, a current version."""
    repo, card, _ = await _tracked(session, clock)
    card.move_to(
        ApplicationStage.APPLIED, expected_version=1, at=clock.now() + timedelta(seconds=5)
    )

    await repo.save(card)

    assert await _raw_version(session, card.id) == 2


# --- remove --------------------------------------------------------------------------------------


async def test_remove_of_an_existing_card_is_true_and_the_row_is_gone(
    session: AsyncSession, clock: FixedClock
) -> None:
    repo, card, _ = await _tracked(session, clock)
    card_id = card.id

    assert await repo.remove(card_id) is True

    assert await _count(session, id=card_id.value) == 0


async def test_remove_of_a_card_that_is_not_there_is_false(session: AsyncSession) -> None:
    repo = SqlAlchemyTrackedApplicationRepository(session)

    assert await repo.remove(TrackedApplicationId(uuid4())) is False


async def test_a_second_remove_of_the_same_card_is_false(
    session: AsyncSession, clock: FixedClock
) -> None:
    repo, card, _ = await _tracked(session, clock)

    assert await repo.remove(card.id) is True
    assert await repo.remove(card.id) is False


async def test_remove_expunges_the_loaded_instance_so_a_later_flush_is_harmless(
    session: AsyncSession, clock: FixedClock
) -> None:
    """The use case loaded the card and recorded `untrack` on it; a Core `DELETE` does not tell the
    ORM, and a flush of that instance would `UPDATE` a row that is gone."""
    repo, card, _ = await _tracked(session, clock)
    card_id = card.id
    session.expunge_all()
    loaded = await repo.get(card_id)
    loaded.untrack(clock.now())

    assert await repo.remove(card_id) is True

    assert loaded not in session
    await session.flush()  # nothing stale left to write
    with pytest.raises(TrackedApplicationNotFound):
        await repo.get(card_id)


async def test_remove_touches_only_the_named_card(session: AsyncSession, clock: FixedClock) -> None:
    repo, first, user_id = await _tracked(session, clock)
    run = await seed_run(session, _owner_of(user_id), clock.now(), "succeeded")
    second = a_card(user_id, clock.now(), run_id=run.id, card_id=repo.next_identity())
    await repo.add(second)
    await session.flush()

    assert await repo.remove(first.id) is True

    assert await _count(session, id=second.id.value) == 1
    assert await _count(session, user_id=user_id.value) == 1


async def test_remove_leaves_the_history_entry_untouched(
    session: AsyncSession, clock: FixedClock
) -> None:
    """AC-24's repository half: untracking deletes the card and **nothing else** — the run (and so
    the entry's documents) and its posting are exactly there afterwards."""
    repo, card, _ = await _tracked(session, clock)
    run_id = card.tailoring_run_id.value

    assert await repo.remove(card.id) is True

    runs = await session.execute(
        text("SELECT count(*), max(job_posting_id::text) FROM tailoring_run WHERE id = :r"),
        {"r": run_id},
    )
    run_count, posting_id = runs.one()
    assert run_count == 1
    postings = await session.execute(
        text("SELECT count(*) FROM posting_job_posting WHERE id = :p"), {"p": UUID(posting_id)}
    )
    assert postings.scalar_one() == 1
