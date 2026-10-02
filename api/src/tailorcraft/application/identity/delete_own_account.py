"""The `DeleteOwnAccount` use case: a signed-in user erases their own account, after re-confirming
their password (slice 2.2, technical plan §0.5, AC-12).

The password check is identity's; the erasure is retention's. So this use case verifies and then
**composes** `EraseAccount` — a use case calling a use case, `RequestTailoringRun`'s precedent —
rather than re-implementing "rows first, then files" in a second place.

**Verify before anything is read for deletion.** A wrong password deletes nothing and reads nothing
beyond the user row it must verify against; a `PasswordHashingFailed` propagates exactly (→ 503) with
nothing deleted. `MATCH_NEEDS_REHASH` is a match, and no rehash is written: the account is about to be
deleted, and a write to a row one step from its `DELETE` would only lengthen the transaction.

**The credential re-check is taken `FOR UPDATE`** (slice 2.5, ADR-0028 §5). A reset can commit during
the ~50 ms verify, and an account must not be erased on the strength of a password that reset just
replaced. `LogIn` re-checks under `FOR SHARE`; this use case must not, because account erasure takes
`FOR UPDATE` on the same row next, in the same transaction. A shared lock followed by an exclusive one
is a lock *upgrade*, and two concurrent deletions of one account would each hold the shared lock and
wait for the other's — a deadlock (503), where one deletion should win and the other answer 401. So
the re-check takes erasure's own lock first: the second deletion waits, then finds the row gone
(`UserNotFound` → 401), and erasure's own `FOR UPDATE` is a re-entry on a lock already held.

**No command dataclass**, like `GetCurrentUser`: a verified id and an already-validated `Password`.
"""

from __future__ import annotations

from tailorcraft.application.identity.resolve_existing_user import resolve_existing_user
from tailorcraft.application.retention.erase_account import EraseAccount
from tailorcraft.domain.identity.errors import InvalidCredentials
from tailorcraft.domain.identity.ports import PasswordHasherPort, UserRepository
from tailorcraft.domain.identity.value_objects import Password, PasswordVerdict, UserId
from tailorcraft.domain.retention.value_objects import AccountErasureReport


class DeleteOwnAccount:
    """Erase `user_id`'s account if `password` is theirs.

    Flow (technical plan §2): ``resolve_existing_user`` (→ `UserNotFound`) →
    ``hasher.verify(password, user.password_hash)`` (→ `PasswordHashingFailed` propagates) →
    `MISMATCH` → `InvalidCredentials`, nothing deleted → ``users.get_for_update(user.id)`` (gone →
    `UserNotFound`; its hash no longer the verified one → `InvalidCredentials`, slice 2.5's re-check)
    → ``erase_account(user_id)`` → its report.
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
        user = await resolve_existing_user(self._users, user_id)

        # `PasswordHashingFailed` propagates from here untouched, before anything is read for
        # deletion. `MATCH_NEEDS_REHASH` falls through as a match, and no rehash is written.
        # The hash verified against, captured in a local *before* the lock: the real repository's
        # `get_for_update` reads with `populate_existing`, which overwrites this very identity-map
        # instance from the locked row — so `user.password_hash` read afterwards would be the
        # locked value, and comparing it with itself would be vacuous.
        verified_hash = user.password_hash
        verdict = await self._hasher.verify(password, verified_hash)
        if verdict is PasswordVerdict.MISMATCH:
            raise InvalidCredentials()
        # The credential re-check (slice 2.5, technical plan §0.7): a reset that committed during
        # the verify means the password just checked is no longer this account's, so it erases
        # nothing. `FOR UPDATE`, never `FOR SHARE` — see the module docstring. The row stays locked
        # until the erasure commits; a row already gone raises `UserNotFound` (→ 401).
        locked = await self._users.get_for_update(user.id)
        if locked.password_hash != verified_hash:
            raise InvalidCredentials()

        return await self._erase_account(user.id)
