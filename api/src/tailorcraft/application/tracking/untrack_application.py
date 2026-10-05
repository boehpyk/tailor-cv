"""The `UntrackApplication` use case: take a card off the board (slice 3.1, technical plan §2,
AC-10).

**SKELETON (T10).** The constructor and the command are real; `__call__` raises
`NotImplementedError` until T12.

Flow: ``resolve_existing_user`` (`UserNotFound`) → ``cards.get`` → authorize by one equality (the
404 collapse) → ``card.untrack(clock.now())`` → ``cards.remove`` — `False` (a concurrent removal, or
a history deletion that took the card, won) ⇒ `TrackedApplicationNotFound` and the recorded event is
**discarded**, never published → publish. A second call is `TrackedApplicationNotFound`.
"""

from __future__ import annotations

from dataclasses import dataclass

from tailorcraft.domain.identity.ports import UserRepository
from tailorcraft.domain.identity.value_objects import UserId
from tailorcraft.domain.shared.clock import Clock
from tailorcraft.domain.shared.events import EventPublisherPort
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
        raise NotImplementedError


__all__ = ["UntrackApplication", "UntrackApplicationCommand"]
