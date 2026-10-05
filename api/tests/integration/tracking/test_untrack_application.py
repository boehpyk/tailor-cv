"""`UntrackApplication` (slice 3.1, T11 RED, AC-10, failure-contract T-22, T-23).

A lost `remove` (a concurrent removal won) must answer `TrackedApplicationNotFound` and publish
**nothing**; the "no event" assertion is paired with the success test, where the same publisher
records `ApplicationUntracked`.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from tailorcraft.application.tracking.untrack_application import (
    UntrackApplication,
    UntrackApplicationCommand,
)
from tailorcraft.domain.identity.errors import UserNotFound
from tailorcraft.domain.identity.value_objects import UserId
from tailorcraft.domain.tracking.errors import (
    TrackedApplicationNotFound,
    TrackedApplicationNotOwnedByUser,
)
from tailorcraft.domain.tracking.events import ApplicationUntracked
from tailorcraft.domain.tracking.value_objects import TrackedApplicationId
from tailorcraft.infrastructure.clock import FixedClock
from tests.integration.fakes import FakeTrackedApplicationRepository, RecordingEventPublisher
from tests.integration.tracking.support import a_card, real_users, seed_user


def _build(
    session: AsyncSession, clock: FixedClock, cards: FakeTrackedApplicationRepository
) -> tuple[UntrackApplication, RecordingEventPublisher]:
    events = RecordingEventPublisher()
    return UntrackApplication(real_users(session), cards, events, clock), events


async def test_untracking_removes_the_card_and_publishes_the_event(
    session: AsyncSession, clock: FixedClock
) -> None:
    user_id = await seed_user(session, clock, "ada@example.com")
    cards = FakeTrackedApplicationRepository()
    card = a_card(user_id, clock.now())
    cards.seed(card)
    use_case, events = _build(session, clock, cards)

    await use_case(UntrackApplicationCommand(user_id=user_id, id=card.id))

    assert cards.removed == [card.id]
    assert cards.all() == ()
    assert cards.calls == ["get", "remove"]
    assert events.published == [
        ApplicationUntracked(
            occurred_at=clock.now(), tracked_application_id=card.id, user_id=user_id
        )
    ]


async def test_untracking_the_same_card_twice_is_not_found_the_second_time(
    session: AsyncSession, clock: FixedClock
) -> None:
    user_id = await seed_user(session, clock, "ada@example.com")
    cards = FakeTrackedApplicationRepository()
    card = a_card(user_id, clock.now())
    cards.seed(card)
    use_case, events = _build(session, clock, cards)
    await use_case(UntrackApplicationCommand(user_id=user_id, id=card.id))
    assert len(events.published) == 1  # the first call really untracked

    with pytest.raises(TrackedApplicationNotFound):
        await use_case(UntrackApplicationCommand(user_id=user_id, id=card.id))

    assert len(events.published) == 1  # and the second announced nothing more


async def test_a_lost_remove_is_not_found_and_publishes_no_event(
    session: AsyncSession, clock: FixedClock
) -> None:
    user_id = await seed_user(session, clock, "ada@example.com")
    cards = FakeTrackedApplicationRepository(lose_on_remove=True)
    card = a_card(user_id, clock.now())
    cards.seed(card)
    use_case, events = _build(session, clock, cards)

    with pytest.raises(TrackedApplicationNotFound):
        await use_case(UntrackApplicationCommand(user_id=user_id, id=card.id))

    assert cards.calls == ["get", "remove"]  # the removal really was attempted
    assert events.published == []


async def test_untracking_a_nonexistent_card_is_not_found_without_an_ownership_cause(
    session: AsyncSession, clock: FixedClock
) -> None:
    user_id = await seed_user(session, clock, "ada@example.com")
    cards = FakeTrackedApplicationRepository()
    use_case, events = _build(session, clock, cards)

    with pytest.raises(TrackedApplicationNotFound) as caught:
        await use_case(
            UntrackApplicationCommand(user_id=user_id, id=TrackedApplicationId(value=uuid4()))
        )

    assert not isinstance(caught.value.__cause__, TrackedApplicationNotOwnedByUser)
    assert cards.calls == ["get"]
    assert events.published == []


async def test_untracking_another_users_card_is_not_found_chained_and_leaves_the_card(
    session: AsyncSession, clock: FixedClock
) -> None:
    user_id = await seed_user(session, clock, "ada@example.com")
    other_id = await seed_user(session, clock, "grace@example.com")
    cards = FakeTrackedApplicationRepository()
    card = a_card(other_id, clock.now())
    cards.seed(card)
    use_case, events = _build(session, clock, cards)

    with pytest.raises(TrackedApplicationNotFound) as caught:
        await use_case(UntrackApplicationCommand(user_id=user_id, id=card.id))

    assert isinstance(caught.value.__cause__, TrackedApplicationNotOwnedByUser)
    assert cards.calls == ["get"]  # read, authorized against, never removed
    assert cards.all() == (card,)
    assert events.published == []


async def test_untracking_for_an_erased_user_is_refused_before_the_card_is_read(
    session: AsyncSession, clock: FixedClock
) -> None:
    user_id = await seed_user(session, clock, "ada@example.com")
    cards = FakeTrackedApplicationRepository()
    card = a_card(user_id, clock.now())
    cards.seed(card)
    use_case, events = _build(session, clock, cards)

    with pytest.raises(UserNotFound):
        await use_case(UntrackApplicationCommand(user_id=UserId(value=uuid4()), id=card.id))

    assert cards.calls == []
    assert cards.all() == (card,)
    assert events.published == []
