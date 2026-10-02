"""The `RequestRegistration` use case: someone asks to create an account for an address (slice 2.5,
technical plan §0.2, AC-7, V-11 … V-18).

**No branch on the address — and no way to write one.** This class is not given a `UserRepository`:
the request path must be the same statements for an address that has an account, has a pending
registration, or has neither, so that nothing a requester can time distinguishes them. The worker,
which nobody can time, decides which mail is sent (`DeliverRegistrationMail`). The absence of the
port from the constructor is the structural half of that proof; AC-7's recording doubles are the
behavioural half.

**Durable, then enqueued.** `PendingRegistrationRepository.put` is bound to a committing adapter, so
the row is committed when it returns; only then is the id handed to the queue — a worker must never
race a commit it cannot see (the tailoring precedent). An enqueue failure after the commit leaves
the row for *Send it again* to supersede, or for the sweep.

**No command dataclass**, `RegisterUser`'s precedent: two raw strings whose rules belong to the
value objects that parse them on the first two lines.

**This layer does not log.** The route logs `pending_registration_id=` from the returned id.
"""

from __future__ import annotations

from datetime import timedelta

from tailorcraft.domain.identity.ports import (
    AccountMailQueuePort,
    PasswordHasherPort,
    PendingRegistrationRepository,
)
from tailorcraft.domain.identity.value_objects import PasswordPolicy, PendingRegistrationId
from tailorcraft.domain.shared.clock import Clock


class RequestRegistration:
    """Record a pending registration for `raw_email` with the hash of `raw_password`, and queue its
    delivery.

    Constructor arguments:

    - `pending`, `hasher`, `mail_queue`, `clock` — ports, bound in the composition root (`pending`
      through a committing adapter: durable on return).
    - `policy` — the `PasswordPolicy` applied, injected so the 422's bounds are the refusing
      object's own (`RegisterUser`'s reason).
    - `confirmation_ttl` — the pending registration's lifetime, built from
      `settings.email_confirmation_ttl_hours` (OQ-7). A `timedelta`, so the unit is a type.

    Flow of `__call__` (technical plan §2), with **one `clock.now()`**:

    1. `EmailAddress.parse(raw_email)` → `Password.from_input(raw_password)` →
       `policy.check(password, email)` — `InvalidEmailAddress` / `WeakPassword` before any hash.
    2. `await hasher.hash(password)` **exactly once** (`PasswordHashingFailed` propagates, nothing
       written).
    3. `PendingRegistration.request(pending.next_identity(), email, hash, now, confirmation_ttl)`;
       `await pending.put(p)` — the supersede is `put`'s contract (§0.3); no read first.
    4. `await mail_queue.enqueue_registration(p.id)` (`AccountMailQueueUnavailable` propagates).
    5. Return `p.id`.
    """

    def __init__(
        self,
        pending: PendingRegistrationRepository,
        hasher: PasswordHasherPort,
        mail_queue: AccountMailQueuePort,
        clock: Clock,
        policy: PasswordPolicy,
        confirmation_ttl: timedelta,
    ) -> None:
        self._pending = pending
        self._hasher = hasher
        self._mail_queue = mail_queue
        self._clock = clock
        self._policy = policy
        self._confirmation_ttl = confirmation_ttl

    async def __call__(self, raw_email: str, raw_password: str) -> PendingRegistrationId:
        raise NotImplementedError
