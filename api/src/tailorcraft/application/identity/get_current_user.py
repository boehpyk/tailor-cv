"""The `GetCurrentUser` use case: the user an access token speaks for (AC-12, I-39).

The `UserId` arrives already verified — `AccessTokenPort.verify` runs in the API's bearer dependency,
not here. A use case for a single `get` looks like ceremony; it is the seam that keeps the router
from holding a repository, and the place a future rule ("a suspended user is not current") lands.
"""

from __future__ import annotations

from tailorcraft.domain.identity.ports import UserRepository
from tailorcraft.domain.identity.user import User
from tailorcraft.domain.identity.value_objects import UserId


class GetCurrentUser:
    """Return the `User` with `user_id`, or raise `UserNotFound` — a valid token whose user row is
    gone (I-39). Constructor argument: the `users` port. Reads nothing else and writes nothing."""

    def __init__(self, users: UserRepository) -> None:
        self._users = users

    async def __call__(self, user_id: UserId) -> User:
        return await self._users.get(user_id)  # `UserNotFound` propagates (I-39)
