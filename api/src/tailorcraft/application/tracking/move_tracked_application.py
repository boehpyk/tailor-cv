"""The `MoveTrackedApplication` use case: move a card to another stage (slice 3.1, technical plan §2,
AC-9, AC-11).

Flow: ``resolve_existing_user`` (`UserNotFound`) → ``cards.get`` (`TrackedApplicationNotFound`) →
``card.user_id != user_id`` ⇒ `TrackedApplicationNotFound` from `TrackedApplicationNotOwnedByUser` →
``card.move_to(stage, expected_version=…, at=clock.now())`` (`TrackedApplicationVersionConflict`
propagates) → ``cards.save`` **iff** the move recorded an event (a same-stage move is a no-op and
writes nothing, AC-11) — `TrackedApplicationConcurrentlyModified` propagates → publish.
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
from tailorcraft.domain.tracking.value_objects import ApplicationStage, TrackedApplicationId


@dataclass(frozen=True, slots=True)
class MoveTrackedApplicationCommand:
    """A signed-in user dropped card `id` on column `stage`. `expected_version` is the card version
    the client was shown; a stale one is refused by the aggregate, never applied."""

    user_id: UserId
    id: TrackedApplicationId
    stage: ApplicationStage
    expected_version: int


class MoveTrackedApplication:
    """Move `cmd.user_id`'s card `cmd.id` to `cmd.stage` and return it (module docstring)."""

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

    async def __call__(self, cmd: MoveTrackedApplicationCommand) -> TrackedApplication:
        await resolve_existing_user(self._users, cmd.user_id)
        card = await load_owned_card(self._cards, cmd.id, cmd.user_id)
        card.move_to(cmd.stage, expected_version=cmd.expected_version, at=self._clock.now())
        events = card.release_events()
        if not events:
            # The card already stands in `cmd.stage` (AC-11): nothing changed, so nothing is written.
            return card
        await self._cards.save(card)
        # After the write: a save that lost the race raised above, and a lost write announces nothing.
        await self._events.publish(*events)
        return card


__all__ = ["MoveTrackedApplication", "MoveTrackedApplicationCommand"]
