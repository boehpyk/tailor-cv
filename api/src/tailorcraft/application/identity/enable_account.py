"""The `EnableAccount` use case: an administrator re-enables a disabled account (slice 4.2, AC-8,
ADR-0033). Restores no session: the disable deleted every login and this creates none.

**Transaction boundary: the caller's.** Naturally idempotent: a second call returns `changed=False`.
"""

from __future__ import annotations

from tailorcraft.application.identity.results import AccountStatusChange
from tailorcraft.domain.identity.ports import UserRepository
from tailorcraft.domain.identity.value_objects import UserId
from tailorcraft.domain.shared.clock import Clock
from tailorcraft.domain.shared.events import EventPublisherPort


class EnableAccount:
    """Enable `target`.

    Flow of `__call__`: `get_for_update(target)` (`UserNotFound` propagates) → `was =
    user.is_disabled` → `user.enable(clock.now())` → save if `was` → publish released events →
    `AccountStatusChange(target, user.disabled_at, changed=was, logins_revoked=0)`.
    """

    def __init__(
        self,
        users: UserRepository,
        clock: Clock,
        events: EventPublisherPort,
    ) -> None:
        self._users = users
        self._clock = clock
        self._events = events

    async def __call__(self, target: UserId) -> AccountStatusChange:
        raise NotImplementedError
