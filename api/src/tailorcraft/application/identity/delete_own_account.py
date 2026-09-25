"""The `DeleteOwnAccount` use case: a signed-in user erases their own account, after re-confirming
their password (slice 2.2, technical plan §0.5, AC-12).

The password check is identity's; the erasure is retention's. So this use case verifies and then
**composes** `EraseAccount` — a use case calling a use case, `RequestTailoringRun`'s precedent —
rather than re-implementing "rows first, then files" in a second place.

**Verify before anything is read for deletion.** A wrong password deletes nothing and reads nothing
beyond the user row it must verify against; a `PasswordHashingFailed` propagates exactly (→ 503) with
nothing deleted. `MATCH_NEEDS_REHASH` is a match, and no rehash is written: the account is about to be
deleted, and a write to a row one step from its `DELETE` would only lengthen the transaction.

**No command dataclass**, like `GetCurrentUser`: a verified id and an already-validated `Password`.

**SKELETON step (T8).** `__init__` is real; `__call__`'s body lands in T10.
"""

from __future__ import annotations

from tailorcraft.application.retention.erase_account import EraseAccount
from tailorcraft.domain.identity.ports import PasswordHasherPort, UserRepository
from tailorcraft.domain.identity.value_objects import Password, UserId
from tailorcraft.domain.retention.value_objects import AccountErasureReport


class DeleteOwnAccount:
    """Erase `user_id`'s account if `password` is theirs.

    Flow (technical plan §2): ``users.get(user_id)`` (→ `UserNotFound`) →
    ``hasher.verify(password, user.password_hash)`` (→ `PasswordHashingFailed` propagates) →
    `MISMATCH` → `InvalidCredentials`, nothing deleted → ``erase_account(user_id)`` → its report.
    """

    def __init__(
        self,
        users: UserRepository,
        hasher: PasswordHasherPort,
        erase_account: EraseAccount,
    ) -> None:
        self._users = users
        self._hasher = hasher
        self._erase_account = erase_account

    async def __call__(self, user_id: UserId, password: Password) -> AccountErasureReport:
        raise NotImplementedError
