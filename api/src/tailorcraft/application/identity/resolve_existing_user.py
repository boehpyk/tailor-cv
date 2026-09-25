"""`resolve_existing_user`: the "the user this token speaks for still exists" step every use case
acting for a registered user starts with (slice 2.2, technical plan §2).

The sibling of `resolve_active_guest_session`, and a plain function for the same reason that one is:
it holds no state between calls, and every dependency it needs already sits on the caller. Defense
in depth rather than a duplicate of the bearer dependency: an access token stays valid for up to
15 minutes after the account behind it is erased, and a use case reachable from a second entry point
must not trust that its caller checked.

**SKELETON step (T8).** The signature is real; the body lands in T10.
"""

from __future__ import annotations

from tailorcraft.domain.identity.ports import UserRepository
from tailorcraft.domain.identity.user import User
from tailorcraft.domain.identity.value_objects import UserId


async def resolve_existing_user(users: UserRepository, user_id: UserId) -> User:
    """Return the `User` with `user_id`. Raises `UserNotFound` (from `users.get`) if the row is gone
    — a still-valid token for an erased account (AC-8)."""
    raise NotImplementedError
