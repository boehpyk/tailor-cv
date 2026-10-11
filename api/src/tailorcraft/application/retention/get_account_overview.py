"""The `GetAccountOverview` use case: one account and what erasing it would take (slice 4.2, AC-11,
ADR-0033).

In `retention` because the counts are retention's ("what erasure would take"); it reads identity's
`UserRepository`, as `tracking` already does. **No lock**: a read for a page, not a decision.
"""

from __future__ import annotations

from dataclasses import dataclass

from tailorcraft.domain.identity.ports import UserRepository
from tailorcraft.domain.identity.user import User
from tailorcraft.domain.identity.value_objects import UserId
from tailorcraft.domain.retention.ports import AccountDataPort
from tailorcraft.domain.retention.value_objects import AccountCounts


@dataclass(frozen=True, slots=True)
class AccountOverview:
    """The account and `count_account`'s numbers for it."""

    user: User
    counts: AccountCounts


class GetAccountOverview:
    """Flow of `__call__`: `user = await users.get(target)` (`UserNotFound`) → `counts = await
    accounts.count_account(target)`; `None` (erased between the two reads) → `UserNotFound` →
    `AccountOverview(user, counts)`."""

    def __init__(self, users: UserRepository, accounts: AccountDataPort) -> None:
        self._users = users
        self._accounts = accounts

    async def __call__(self, target: UserId) -> AccountOverview:
        raise NotImplementedError
