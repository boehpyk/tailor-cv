"""The `DisableAccount` use case: an administrator disables an account (slice 4.2, AC-7, ADR-0033).

**Lock order: the user row first, then its logins** (ADR-0028), so a disable cannot deadlock against
a reset, a login's `FOR SHARE` re-check or an erasure. **Transaction boundary: the caller's** — the
route commits inside its handler. Naturally idempotent: a second call returns `changed=False`.
"""

from __future__ import annotations

from tailorcraft.application.identity.results import AccountStatusChange
from tailorcraft.domain.identity.ports import LoginRepository, UserRepository
from tailorcraft.domain.identity.value_objects import UserId
from tailorcraft.domain.shared.clock import Clock
from tailorcraft.domain.shared.events import EventPublisherPort


class DisableAccount:
    """Disable `target` and delete every login it holds.

    Flow of `__call__`:

    1. `user = await users.get_for_update(target)` — `UserNotFound` propagates; the lock comes first.
    2. `revoked = await logins.remove_all_for_user(target)` — even when already disabled: a login
       of a disabled user should not exist, and the cure is deletion.
    3. `was = user.is_disabled`; `user.disable(clock.now(), revoked)` (a no-op if `was`).
    4. Not `was` → `await users.save(user)`. Then `await events.publish(*user.release_events())`.
    5. Return `AccountStatusChange(target, user.disabled_at, changed=not was, logins_revoked=revoked)`.
    """

    def __init__(
        self,
        users: UserRepository,
        logins: LoginRepository,
        clock: Clock,
        events: EventPublisherPort,
    ) -> None:
        self._users = users
        self._logins = logins
        self._clock = clock
        self._events = events

    async def __call__(self, target: UserId) -> AccountStatusChange:
        raise NotImplementedError
