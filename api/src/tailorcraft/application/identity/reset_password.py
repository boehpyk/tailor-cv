"""The `ResetPassword` use case: a reset link's token and a new password replace an account's
credential and revoke every device (slice 2.5, technical plan §0.7, §0.8, ADR-0028, AC-12,
V-45 … V-53).

**Lock order is the design** (§0.8): the reset is first read *unlocked*, only to learn whose it is;
the **user** row is then locked, and only after that is the reset re-found and locked. Everything
that takes both a user and a reset takes the user first, so this and account erasure queue on one
row instead of deadlocking on two (AC-37).

**A policy refusal leaves the token usable** (V-46): the password is checked after the locks and
before any write, and nothing is removed on that path, so the user can retry with the same link.

**Logins are deleted before the event is recorded**, in the same transaction (invisible outside it),
so `User.reset_password` can record `PasswordChangedByReset(logins_revoked=n)` on the aggregate like
every other event rather than have it built here.

`TokenHash` in, never a plaintext (the route hashes). The request's transaction, committed in the
handler. **This layer does not log**; the route logs the refusal's `reason`.
"""

from __future__ import annotations

from tailorcraft.domain.identity.ports import (
    LoginRepository,
    PasswordHasherPort,
    PasswordResetRepository,
    UserRepository,
)
from tailorcraft.domain.identity.value_objects import PasswordPolicy, TokenHash
from tailorcraft.domain.shared.clock import Clock
from tailorcraft.domain.shared.events import EventPublisherPort


class ResetPassword:
    """Set the password of the account whose reset link hashes to `token_hash` to `raw_password`.

    Constructor: ports bound in the composition root, and the injected `PasswordPolicy`
    (`RegisterUser`'s reason: the 422's bounds are the refusing object's own).

    Flow of `__call__` (technical plan §2, AC-12), with **one `clock.now()`**:

    1. `password = Password.from_input(raw_password)`.
    2. `r = await resets.find_by_token_hash(token_hash)` (no lock); `None` →
       `ResetTokenInvalid(TokenRefusal.UNKNOWN)`.
    3. `user = await users.get_for_update(r.user_id)` — the user lock, **first**.
    4. `r = await resets.lock_by_token_hash(token_hash)`; `None` (used or superseded meanwhile) →
       `ResetTokenInvalid(UNKNOWN)`.
    5. `r.is_expired(now)` → `await resets.remove(r.id)` → `ResetTokenInvalid(EXPIRED)`.
    6. `policy.check(password, user.email)` — `WeakPassword`, **nothing written**, token kept.
    7. `new_hash = await hasher.hash(password)` once (`PasswordHashingFailed`, nothing written).
    8. `n = await logins.remove_all_for_user(user.id)` →
       `user.reset_password(new_hash, now, logins_revoked=n)` → `await users.save(user)` →
       `await resets.remove_all_for_user(user.id)`.
    9. `await events.publish(*user.release_events())`.
    """

    def __init__(
        self,
        users: UserRepository,
        resets: PasswordResetRepository,
        logins: LoginRepository,
        hasher: PasswordHasherPort,
        clock: Clock,
        events: EventPublisherPort,
        policy: PasswordPolicy,
    ) -> None:
        self._users = users
        self._resets = resets
        self._logins = logins
        self._hasher = hasher
        self._clock = clock
        self._events = events
        self._policy = policy

    async def __call__(self, token_hash: TokenHash, raw_password: str) -> None:
        raise NotImplementedError
