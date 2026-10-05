"""The retention adapters' tracking changes (slice 3.1, T17's code, T19's tests, written after):
`SqlAlchemyHistoryEntryData.delete_history_entry`'s card `DELETE … RETURNING` (AC-24's adapter half,
plan §0.7) and `SqlAlchemyAccountData`'s `count_account` / `delete_account` (AC-28's adapter half).

2.3's own tests for both adapters are untouched and green — that is the "byte-identical for an
untracked entry" half. What is new here is every tracked case, each beside a control:

- the card goes **with its run, in the same call**, and the report says so
  (`tracked_application_deleted=True`); an **untracked** entry reports `False` and takes no card;
- the card `DELETE` is scoped to the user: another user's card over the same run id (raw SQL only —
  a run has one owner) and every card over **other** runs survive;
- a card instance the session had loaded is expunged, so a later flush cannot target a row that is
  gone (2.2's `delete_account` precedent);
- `count_account` counts the cards — **non-zero**, which 2.3's expected `AccountCounts` (the field
  defaulting to `0`) could never prove — and `delete_account` takes them by cascade while sparing
  other users'.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from tailorcraft.domain.identity.value_objects import UserId
from tailorcraft.domain.retention.value_objects import AccountCounts
from tailorcraft.domain.tailoring.tailoring_run import TailoringRun
from tailorcraft.domain.tracking.value_objects import TrackedApplicationId
from tailorcraft.infrastructure.clock import FixedClock
from tailorcraft.infrastructure.persistence.mapping.tracking.tracked_application import (
    tracked_application_table,
)
from tailorcraft.infrastructure.persistence.repositories.tracking.tracked_application import (
    SqlAlchemyTrackedApplicationRepository,
)
from tailorcraft.infrastructure.persistence.retention.account_data import SqlAlchemyAccountData
from tailorcraft.infrastructure.persistence.retention.history_entry_data import (
    SqlAlchemyHistoryEntryData,
)
from tests.integration.persistence.owner_rows import persist_user
from tests.integration.tracking.support import a_card, seed_run


async def _track(
    session: AsyncSession, clock: FixedClock, user_id: UserId, run: TailoringRun
) -> TrackedApplicationId:
    repo = SqlAlchemyTrackedApplicationRepository(session)
    card = a_card(user_id, clock.now(), run_id=run.id, card_id=repo.next_identity())
    card_id = card.id
    await repo.add(card)
    await session.flush()
    return card_id


async def _card_exists(session: AsyncSession, card_id: TrackedApplicationId) -> bool:
    found = await session.execute(
        select(func.count())
        .select_from(tracked_application_table)
        .where(tracked_application_table.c.id == card_id)
    )
    return found.scalar_one() == 1


# --- delete_history_entry ------------------------------------------------------------------------


async def test_deleting_a_tracked_entry_takes_its_card_and_reports_it(
    session: AsyncSession, clock: FixedClock
) -> None:
    user = await persist_user(session, clock)
    run = await seed_run(session, user, clock.now(), "succeeded")
    card_id = await _track(session, clock, user.user_id, run)

    deleted = await SqlAlchemyHistoryEntryData(session).delete_history_entry(
        user.user_id, run.id.value
    )

    assert deleted is not None
    assert deleted.tracked_application_deleted is True
    assert not await _card_exists(session, card_id)


async def test_deleting_an_untracked_entry_reports_no_card_and_touches_no_other_card(
    session: AsyncSession, clock: FixedClock
) -> None:
    """The control for the test above, and 2.3's behaviour for an untracked entry: nothing about the
    report changes but the new field, which reads `False`; a **tracked sibling** is untouched."""
    user = await persist_user(session, clock)
    untracked = await seed_run(session, user, clock.now(), "succeeded")
    sibling = await seed_run(session, user, clock.now(), "succeeded")
    sibling_card = await _track(session, clock, user.user_id, sibling)

    deleted = await SqlAlchemyHistoryEntryData(session).delete_history_entry(
        user.user_id, untracked.id.value
    )

    assert deleted is not None
    assert deleted.tracked_application_deleted is False
    assert deleted.posting_deleted is True
    assert deleted.export_jobs == 0
    assert await _card_exists(session, sibling_card)


async def test_another_users_card_survives_a_deletion_of_a_run_they_do_not_own(
    session: AsyncSession, clock: FixedClock
) -> None:
    """The card `DELETE` carries the owner. A card of a **stranger** filed (raw SQL — no use case
    builds it) over this user's run id is not this user's to delete: the run goes, the stranger's row
    stays, and the report says no card was taken."""
    user = await persist_user(session, clock)
    stranger = await persist_user(session, clock)
    run = await seed_run(session, user, clock.now(), "succeeded")
    strangers_card = TrackedApplicationId(UUID(int=0x7000 << 64 | 1))
    await session.execute(
        text(
            "INSERT INTO tracking_application "
            "(id, user_id, tailoring_run_id, stage, tracked_at, stage_changed_at, version) "
            "VALUES (:i, :u, :r, 'applied', :t, :t, 1)"
        ),
        {
            "i": strangers_card.value,
            "u": stranger.user_id.value,
            "r": run.id.value,
            "t": clock.now(),
        },
    )

    deleted = await SqlAlchemyHistoryEntryData(session).delete_history_entry(
        user.user_id, run.id.value
    )

    assert deleted is not None
    assert deleted.tracked_application_deleted is False
    assert await _card_exists(session, strangers_card)


async def test_a_foreign_deletion_attempt_takes_neither_the_run_nor_the_card(
    session: AsyncSession, clock: FixedClock
) -> None:
    owner = await persist_user(session, clock)
    intruder = await persist_user(session, clock)
    run = await seed_run(session, owner, clock.now(), "succeeded")
    card_id = await _track(session, clock, owner.user_id, run)

    deleted = await SqlAlchemyHistoryEntryData(session).delete_history_entry(
        intruder.user_id, run.id.value
    )

    assert deleted is None
    assert await _card_exists(session, card_id)


async def test_a_card_instance_the_session_loaded_is_expunged_so_a_later_flush_is_harmless(
    session: AsyncSession, clock: FixedClock
) -> None:
    user = await persist_user(session, clock)
    run = await seed_run(session, user, clock.now(), "succeeded")
    card_id = await _track(session, clock, user.user_id, run)
    session.expunge_all()
    repo = SqlAlchemyTrackedApplicationRepository(session)
    loaded = await repo.get(card_id)

    await SqlAlchemyHistoryEntryData(session).delete_history_entry(user.user_id, run.id.value)

    assert loaded not in session
    await session.flush()  # nothing stale left to write
    assert not await _card_exists(session, card_id)


# --- count_account / delete_account --------------------------------------------------------------


async def test_count_account_counts_the_users_cards_and_only_theirs(
    session: AsyncSession, clock: FixedClock
) -> None:
    user = await persist_user(session, clock)
    other = await persist_user(session, clock)
    for _ in range(3):
        await _track(
            session, clock, user.user_id, await seed_run(session, user, clock.now(), "succeeded")
        )
    await _track(
        session, clock, other.user_id, await seed_run(session, other, clock.now(), "succeeded")
    )

    counts = await SqlAlchemyAccountData(session).count_account(user.user_id)

    assert counts is not None
    assert counts.tracked_applications == 3
    assert counts == AccountCounts(
        base_cvs=0,
        files=0,
        logins=0,
        tailoring_runs=3,
        job_postings=3,
        export_jobs=0,
        tracked_applications=3,
    )


async def test_count_account_of_a_user_with_no_cards_counts_zero_cards(
    session: AsyncSession, clock: FixedClock
) -> None:
    user = await persist_user(session, clock)
    await seed_run(session, user, clock.now(), "succeeded")  # history, but nothing tracked

    counts = await SqlAlchemyAccountData(session).count_account(user.user_id)

    assert counts is not None
    assert counts.tracked_applications == 0
    assert counts.tailoring_runs == 1


async def test_delete_account_takes_the_users_cards_by_cascade_and_spares_others(
    session: AsyncSession, clock: FixedClock
) -> None:
    user = await persist_user(session, clock)
    other = await persist_user(session, clock)
    mine = await _track(
        session, clock, user.user_id, await seed_run(session, user, clock.now(), "succeeded")
    )
    theirs = await _track(
        session, clock, other.user_id, await seed_run(session, other, clock.now(), "succeeded")
    )

    assert await SqlAlchemyAccountData(session).delete_account(user.user_id) is True

    assert not await _card_exists(session, mine)
    assert await _card_exists(session, theirs)
