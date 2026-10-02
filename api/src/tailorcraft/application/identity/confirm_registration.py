"""The `ConfirmRegistration` use case: a confirmation link's token turns a pending registration into a
`User` — the only way a `User` is created from slice 2.5 on (technical plan §0.6, AC-11, V-32 … V-39).

**It does not sign anyone in**, and cannot: this class is given no `LoginRepository` and no
`AccessTokenPort`. If confirming signed the clicker in, an attacker who registered your address with
their password would have you signed into their account the moment you clicked (§0.6's pre-hijack).
The constructor's missing ports are the structural half of AC-11's "no `Login` is created".

**A `TokenHash` in, never a plaintext.** The route hashes the presented token (and refuses a
malformed one as `MALFORMED` before this is called), as it hashes the refresh cookie.

**A refusal that writes** (an expired row is deleted) raises after the write; the route commits the
removal and answers 400 — 2.1's *refusal-that-writes* rule, applied in the handler.

**This layer does not log.** `UserRegistered` is published after the insert; the route logs the
refusal's `reason` from `ConfirmationTokenInvalid`.
"""

from __future__ import annotations

from tailorcraft.domain.identity.ports import PendingRegistrationRepository, UserRepository
from tailorcraft.domain.identity.value_objects import TokenHash, UserId
from tailorcraft.domain.shared.clock import Clock
from tailorcraft.domain.shared.events import EventPublisherPort


class ConfirmRegistration:
    """Create the account the pending registration behind `token_hash` asked for.

    Flow of `__call__` (technical plan §2, §0.8's lock order: pending row, then `User` insert, then
    the delete), with **one `clock.now()`**:

    1. `p = await pending.lock_by_token_hash(token_hash)`; `None` →
       `ConfirmationTokenInvalid(TokenRefusal.UNKNOWN)`.
    2. `p.is_expired(now)` → `await pending.remove(p.id)` →
       `ConfirmationTokenInvalid(TokenRefusal.EXPIRED)`.
    3. `email, password_hash = p.confirm(now)`.
    4. `user = User.register_with_password(users.next_identity(), email, password_hash, now)`;
       `await users.add(user)` — `EmailAlreadyRegistered` → `await pending.remove(p.id)`, re-raised.
    5. `await pending.remove(p.id)`.
    6. `await events.publish(*user.release_events())`; return `user.id`.
    """

    def __init__(
        self,
        pending: PendingRegistrationRepository,
        users: UserRepository,
        clock: Clock,
        events: EventPublisherPort,
    ) -> None:
        self._pending = pending
        self._users = users
        self._clock = clock
        self._events = events

    async def __call__(self, token_hash: TokenHash) -> UserId:
        raise NotImplementedError
