"""The `RetitleTrackedApplication` use case: set or clear a card's title (slice 3.1, technical plan
§2, AC-9, AC-11).

**SKELETON (T10).** The constructor and the command are real; `__call__` raises
`NotImplementedError` until T12.

Flow: `MoveTrackedApplication`'s, with ``card.retitle(title, expected_version=…, at=clock.now())``
in place of the move, and ``cards.save`` **iff the version changed** — a retitle records no event
(an event would have to carry the title, which no event may), so the version is the only witness
that something changed. A same-title retitle writes nothing (AC-11).
"""

from __future__ import annotations

from dataclasses import dataclass

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
        raise NotImplementedError


__all__ = ["RetitleTrackedApplication", "RetitleTrackedApplicationCommand"]
