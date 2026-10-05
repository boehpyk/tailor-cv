"""The `UntrackApplication` use case: take a card off the board (slice 3.1, technical plan §2,
AC-10).

Flow: ``resolve_existing_user`` (`UserNotFound`) → ``cards.get`` → authorize by one equality (the
404 collapse) → ``card.untrack(clock.now())`` → ``cards.remove`` — `False` (a concurrent removal, or
a history deletion that took the card, won) ⇒ `TrackedApplicationNotFound` and the recorded event is
**discarded**, never published → publish. A second call is `TrackedApplicationNotFound`.
"""

from __future__ import annotations

from dataclasses import dataclass

from tailorcraft.application.identity.resolve_existing_user import resolve_existing_user
from tailorcraft.application.tracking.owned_card import load_owned_card
from tailorcraft.domain.identity.ports import UserRepository
from tailorcraft.domain.identity.value_objects import UserId
from tailorcraft.domain.shared.clock import Clock
from tailorcraft.domain.shared.events import EventPublisherPort
from tailorcraft.domain.tracking.errors import TrackedApplicationNotFound
from tailorcraft.domain.tracking.ports import TrackedApplicationRepository
from tailorcraft.domain.tracking.value_objects import TrackedApplicationId


@dataclass(frozen=True, slots=True)
class UntrackApplicationCommand:
    """A signed-in user removed card `id` from their board. The run and its documents stay."""

    user_id: UserId
    id: TrackedApplicationId


class UntrackApplication:
    """Remove `cmd.user_id`'s card `cmd.id` (module docstring). Returns nothing: the answer is a 204."""

    def __init__(
        self,
        users: UserRepository,
        cards: TrackedApplicationRepository,
        events: EventPublisherPort,
        clock: Clock,
    ) -> None:
        self._users = users
        self._cards = cards
        self._events = events
        self._clock = clock

    async def __call__(self, cmd: UntrackApplicationCommand) -> None:
        await resolve_existing_user(self._users, cmd.user_id)
        card = await load_owned_card(self._cards, cmd.id, cmd.user_id)
        card.untrack(self._clock.now())
        events = card.release_events()
        if not await self._cards.remove(card.id):
            # A concurrent removal (or a history deletion that took the card) won between `get` and
            # here. The fact `events` records did not happen in this call, so it is dropped unpublished.
            raise TrackedApplicationNotFound(str(cmd.id.value))
        await self._events.publish(*events)


__all__ = ["UntrackApplication", "UntrackApplicationCommand"]
