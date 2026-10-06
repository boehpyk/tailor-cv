"""The `RetitleTrackedApplication` use case: set or clear a card's title (slice 3.1, technical plan
§2, AC-9, AC-11).

Flow: `MoveTrackedApplication`'s, with ``card.retitle(title, expected_version=…, at=clock.now())``
in place of the move, and ``cards.save`` **iff the version changed** — a retitle records no event
(an event would have to carry the title, which no event may), so the version is the only witness
that something changed. A same-title retitle writes nothing (AC-11).
"""

from __future__ import annotations

from dataclasses import dataclass

from tailorcraft.application.identity.resolve_existing_user import resolve_existing_user
from tailorcraft.application.tracking.owned_card import load_owned_card
from tailorcraft.domain.identity.ports import UserRepository
from tailorcraft.domain.identity.value_objects import UserId
from tailorcraft.domain.shared.clock import Clock
from tailorcraft.domain.shared.events import EventPublisherPort
from tailorcraft.domain.tracking.ports import TrackedApplicationRepository
from tailorcraft.domain.tracking.tracked_application import TrackedApplication
from tailorcraft.domain.tracking.value_objects import ApplicationTitle, TrackedApplicationId


@dataclass(frozen=True, slots=True)
class RetitleTrackedApplicationCommand:
    """A signed-in user renamed card `id`. `title` is `None` to clear it, and otherwise an
    `ApplicationTitle` already validated at the boundary."""

    user_id: UserId
    id: TrackedApplicationId
    title: ApplicationTitle | None
    expected_version: int


class RetitleTrackedApplication:
    """Retitle `cmd.user_id`'s card `cmd.id` and return it (module docstring)."""

    def __init__(
        self,
        users: UserRepository,
        cards: TrackedApplicationRepository,
        events: EventPublisherPort,
        clock: Clock,
    ) -> None:
        self._users = users
        self._cards = cards
        # Held although a retitle records no event today: its constructor matches its siblings', so
        # the wiring is one shape, and a future event on the aggregate needs no new dependency here.
        self._events = events
        self._clock = clock

    async def __call__(self, cmd: RetitleTrackedApplicationCommand) -> TrackedApplication:
        await resolve_existing_user(self._users, cmd.user_id)
        card = await load_owned_card(self._cards, cmd.id, cmd.user_id)
        version_before = card.version
        card.retitle(cmd.title, expected_version=cmd.expected_version, at=self._clock.now())
        if card.version == version_before:
            # The same title (AC-11): no event exists to say so, so the version is the witness.
            return card
        await self._cards.save(card)
        return card


__all__ = ["RetitleTrackedApplication", "RetitleTrackedApplicationCommand"]
