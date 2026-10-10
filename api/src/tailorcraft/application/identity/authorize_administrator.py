"""The `AuthorizeAdministrator` use case: may this signed-in user administer? (AC-6, slice 4.1).

Authorization is a use-case rule; its HTTP shape is the boundary's decision. This answers in domain
terms (`NotAnAdministrator`), and `require_admin` decides that the answer is a plain 404 on the wire
(technical plan §0.1, §0.4).
"""

from __future__ import annotations

from tailorcraft.domain.identity.ports import UserRepository
from tailorcraft.domain.identity.user import User
from tailorcraft.domain.identity.value_objects import UserId


class AuthorizeAdministrator:
    """Return the `User` with `user_id` if they are an administrator; raise `NotAnAdministrator`
    for a plain user; let `UserNotFound` propagate for a missing row. Constructor argument: the
    `users` port.

    Reads through `UserRepository.get` only — no lock, no clock, no write, no event.

    **The role is read per request, on purpose** (decision 4): it is never a token claim, so a
    revocation takes effect on the admin's next request rather than when a 15-minute access token
    expires. The window that remains is TOCTOU: a request that passed this check keeps running
    after a concurrent `revoke-role` commits (R-10, OQ-16). It is one request long and accepted.
    """

    def __init__(self, users: UserRepository) -> None:
        self._users = users

    async def __call__(self, user_id: UserId) -> User:
        raise NotImplementedError
