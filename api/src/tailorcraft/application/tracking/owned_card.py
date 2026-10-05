"""`load_owned_card`: the one authorization rule three tracking use cases share (slice 3.1, technical
plan §2).

Move, retitle and untrack all load a card by id and authorize it by **one equality**,
``card.user_id == user_id``. Written once here rather than three times, because this is shared
*behaviour* — the same rule, with the same 404 collapse — not a shared shape. A plain function, as
`resolve_existing_user` is: it holds no state, and every dependency already sits on the caller.
"""

from __future__ import annotations

from tailorcraft.domain.identity.value_objects import UserId
from tailorcraft.domain.tracking.errors import (
    TrackedApplicationNotFound,
    TrackedApplicationNotOwnedByUser,
)
from tailorcraft.domain.tracking.ports import TrackedApplicationRepository
from tailorcraft.domain.tracking.tracked_application import TrackedApplication
from tailorcraft.domain.tracking.value_objects import TrackedApplicationId


async def load_owned_card(
    cards: TrackedApplicationRepository, card_id: TrackedApplicationId, user_id: UserId
) -> TrackedApplication:
    """Return card `card_id` if it is `user_id`'s.

    A nonexistent card is `TrackedApplicationNotFound` from `cards.get`, with no ownership cause. A
    foreign card is the **same** `TrackedApplicationNotFound`, raised from
    `TrackedApplicationNotOwnedByUser`: answering "not yours" would confirm to a caller that the id
    they guessed is real, and the cause keeps the difference readable by the tests.
    """
    card = await cards.get(card_id)
    if card.user_id != user_id:
        raise TrackedApplicationNotFound(str(card_id.value)) from TrackedApplicationNotOwnedByUser(
            str(card_id.value)
        )
    return card


__all__ = ["load_owned_card"]
