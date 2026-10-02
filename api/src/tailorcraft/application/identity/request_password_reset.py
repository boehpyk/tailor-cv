"""The `RequestPasswordReset` use case: someone asks for a reset link for an address (slice 2.5,
technical plan §0.2, AC-8, V-40, V-41).

**No branch on the address, no hash, no lookup.** This class is given neither a `UserRepository` nor
a `PasswordHasherPort`: the request writes one *addressed* reset and enqueues its id, the same two
statements whether or not an account has the address. The worker finds the account, or finds none
and sends nothing (`DeliverPasswordResetMail`).

`resets.add` is bound to a committing adapter — durable on return — so the enqueue that follows never
hands the worker an id it cannot see. No command dataclass: one raw string, parsed on the first line.
**This layer does not log**; the route logs `password_reset_id=` from the returned id.
"""

from __future__ import annotations

from datetime import timedelta

from tailorcraft.domain.identity.password_reset import PasswordReset
from tailorcraft.domain.identity.ports import AccountMailQueuePort, PasswordResetRepository
from tailorcraft.domain.identity.value_objects import EmailAddress, PasswordResetId
from tailorcraft.domain.shared.clock import Clock


class RequestPasswordReset:
    """Record an addressed password reset for `raw_email` and queue its delivery.

    `reset_ttl` is the reset's lifetime, built from `settings.password_reset_ttl_minutes` (OQ-7).

    Flow of `__call__` (technical plan §2), with **one `clock.now()`**:

    1. `EmailAddress.parse(raw_email)` (`InvalidEmailAddress`).
    2. `PasswordReset.request(resets.next_identity(), email, now, reset_ttl)`;
       `await resets.add(r)`.
    3. `await mail_queue.enqueue_password_reset(r.id)` (`AccountMailQueueUnavailable` propagates).
    4. Return `r.id`.
    """

    def __init__(
        self,
        resets: PasswordResetRepository,
        mail_queue: AccountMailQueuePort,
        clock: Clock,
        reset_ttl: timedelta,
    ) -> None:
        self._resets = resets
        self._mail_queue = mail_queue
        self._clock = clock
        self._reset_ttl = reset_ttl

    async def __call__(self, raw_email: str) -> PasswordResetId:
        email = EmailAddress.parse(raw_email)
        now = self._clock.now()

        reset = PasswordReset.request(self._resets.next_identity(), email, now, self._reset_ttl)
        await self._resets.add(reset)
        await self._mail_queue.enqueue_password_reset(reset.id)
        return reset.id
