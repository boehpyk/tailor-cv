"""`ShowApplicationBoard` (slice 3.1, T11 RED, AC-12; ordering per AC-30).

The query double is `InMemoryApplicationBoardQuery`, which honours AC-30's order
(`stage_changed_at DESC, id DESC`, only the user's cards). The use case's own obligations: resolve
the user first, ask the port for exactly that user, and hand the port's board back **unchanged**.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from tailorcraft.application.tracking.show_application_board import ShowApplicationBoard
from tailorcraft.domain.identity.errors import UserNotFound
from tailorcraft.domain.identity.value_objects import UserId
from tailorcraft.domain.tracking.value_objects import TrackedApplicationId
from tailorcraft.infrastructure.clock import FixedClock
from tests.integration.fakes import FakeTrackedApplicationRepository, InMemoryApplicationBoardQuery
from tests.integration.tracking.support import a_card, real_users, seed_user


def _build(
    session: AsyncSession,
) -> tuple[ShowApplicationBoard, FakeTrackedApplicationRepository, InMemoryApplicationBoardQuery]:
    cards = FakeTrackedApplicationRepository()
    query = InMemoryApplicationBoardQuery(cards)
    return ShowApplicationBoard(real_users(session), query), cards, query


async def test_the_board_is_the_query_ports_board_unchanged(
    session: AsyncSession, clock: FixedClock
) -> None:
    user_id = await seed_user(session, clock, "ada@example.com")
    use_case, cards, query = _build(session)
    card = a_card(user_id, clock.now())
    cards.seed(card)

    board = await use_case(user_id)

    assert query.calls == [user_id]
    assert query.last_board is not None
    assert board is query.last_board  # not rebuilt, filtered or re-sorted
    assert [c.id for c in board.cards] == [card.id]


async def test_an_empty_board_is_an_empty_board_not_an_error(
    session: AsyncSession, clock: FixedClock
) -> None:
    user_id = await seed_user(session, clock, "ada@example.com")
    use_case, _cards, query = _build(session)

    board = await use_case(user_id)

    assert query.calls == [user_id]  # the port really was asked
    assert board.cards == ()


async def test_only_the_requesting_users_cards_are_on_the_board(
    session: AsyncSession, clock: FixedClock
) -> None:
    user_id = await seed_user(session, clock, "ada@example.com")
    other_id = await seed_user(session, clock, "grace@example.com")
    use_case, cards, _query = _build(session)
    mine = a_card(user_id, clock.now())
    theirs = a_card(other_id, clock.now())
    cards.seed(mine)
    cards.seed(theirs)

    board = await use_case(user_id)

    assert [c.id for c in board.cards] == [mine.id]


async def test_cards_come_most_recently_moved_first_with_ties_broken_by_id_descending(
    session: AsyncSession, clock: FixedClock
) -> None:
    """AC-30's order, through the use case: `stage_changed_at DESC, id DESC`. Two cards share an
    instant; ids are fixed so the tie-break direction is unambiguous."""
    user_id = await seed_user(session, clock, "ada@example.com")
    use_case, cards, _query = _build(session)
    now = clock.now()
    oldest = a_card(user_id, now - timedelta(hours=2))
    newest = a_card(user_id, now)
    low = a_card(user_id, now - timedelta(hours=1), card_id=TrackedApplicationId(UUID(int=1)))
    high = a_card(user_id, now - timedelta(hours=1), card_id=TrackedApplicationId(UUID(int=2)))
    for card in (oldest, low, newest, high):
        cards.seed(card)

    board = await use_case(user_id)

    assert [c.id for c in board.cards] == [newest.id, high.id, low.id, oldest.id]


async def test_an_erased_user_is_refused_before_the_query_is_asked(
    session: AsyncSession, clock: FixedClock
) -> None:
    owner_id = await seed_user(session, clock, "ada@example.com")
    use_case, cards, query = _build(session)
    cards.seed(a_card(owner_id, clock.now()))

    with pytest.raises(UserNotFound):
        await use_case(UserId(value=uuid4()))

    assert query.calls == []
    assert query.last_board is None
