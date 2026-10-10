"""The `ChangeUserRole` use case: grant or revoke the administrator role (AC-7, slice 4.1).

Entry points: `python -m tailorcraft.cli grant-role` / `revoke-role` (CLI only — no router imports
this, AC-23). **`dry_run` is a call argument**, for `RevokeAllLogins`'s reason: one caller decides
per invocation, and a rehearsal followed by the real change is the intended sequence.

**Transaction boundary: the caller's.** The CLI's composition root opens the session and commits
after this returns; a dry run rolls back. Events are published here after `save`, as in every
identity use case. **Idempotent**: a second identical run returns `changed=False` (R-18).
"""

from __future__ import annotations

from tailorcraft.application.identity.results import RoleChange
from tailorcraft.domain.identity.ports import UserRepository
from tailorcraft.domain.identity.value_objects import Role, UserId
from tailorcraft.domain.shared.clock import Clock
from tailorcraft.domain.shared.events import EventPublisherPort


class ChangeUserRole:
    """Move the user with `user_id` to the role `to`, and report what happened as a `RoleChange`.

    Constructor arguments: the ports `users`, `clock`, `events`.

    Flow of `__call__`:

    1. `user = await users.get_for_update(user_id)` — `UserNotFound` propagates. The row lock
       serializes a concurrent grant, revoke or account erasure (R-19, R-20).
    2. `from_role = user.role`; `changed = from_role is not to`.
    3. `dry_run` or not `changed` → return the `RoleChange` with no save and no event; a dry run's
       `changed` says whether a real run would change the role.
    4. `user.change_role(to, clock.now())`; `await users.save(user)`; then
       `await events.publish(*user.release_events())` (`UserRoleChanged`) — after the save.
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

    async def __call__(self, user_id: UserId, to: Role, *, dry_run: bool) -> RoleChange:
        user = await self._users.get_for_update(user_id)
        from_role = user.role
        outcome = RoleChange(user_id, from_role, to, changed=from_role is not to)
        if dry_run or not outcome.changed:
            return outcome
        user.change_role(to, self._clock.now())
        await self._users.save(user)
        await self._events.publish(*user.release_events())
        return outcome
