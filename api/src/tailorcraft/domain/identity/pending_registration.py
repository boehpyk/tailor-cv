"""The `PendingRegistration` aggregate: a sign-up that has not proven its address (ADR-0027).

**Not an unverified `User`.** The state that would have been a boolean on `User` is a different thing
(technical plan §0.1): an account whose invariant is "exactly one current credential, proven" has no
state in which that is false, so an unconfirmed sign-up lives here, for at most its TTL, and becomes a
`User` only when its link is confirmed. What an unconfirmed account may do is therefore *nothing* —
there is no account.

**Lifecycle**, and it is short: `request` (no token yet) → `issue` (the worker minted a token and
stores its hash; once only) → `confirm` (hands back what a `User` is built from). Expiry is inclusive,
like `Login`'s. **Invariant:** `token_hash is None ⇔ issued_at is None`, and
`requested_at ≤ issued_at < expires_at`.

**One per address, newest wins** (§0.3) — but that is a property of the *set* of pending
registrations, which this aggregate cannot see; it is `PendingRegistrationRepository.put`'s contract,
as uniqueness of `User.email` is `UserRepository.add`'s.

**Records no events.** It is an address-only fact with no id worth publishing; `UserRegistered` is
recorded by the `User` it becomes.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from tailorcraft.domain.identity.value_objects import (
    EmailAddress,
    PasswordHash,
    PendingRegistrationId,
    TokenHash,
)
from tailorcraft.domain.shared.events import RecordsEvents


# NOT `slots=True`: mapped imperatively, like `User` and `Login` — see `user.py` for the reason. The
# private-attribute names below are exactly what
# `infrastructure/persistence/mapping/identity/pending_registration.py` will target (ADR-0007).
# `RecordsEvents` is composed although nothing is recorded: every aggregate composes it, and the
# use cases release events from whatever aggregate they saved without asking which kind it was.
class PendingRegistration(RecordsEvents):
    """A sign-up awaiting its confirmation link.

    State: `_id`, `_email`, `_password_hash`, `_requested_at`, `_expires_at`, `_token_hash`
    (`None` until issued), `_issued_at` (`None` until issued) — private, exposed only through
    read-only properties.
    """

    _id: PendingRegistrationId
    _email: EmailAddress
    _password_hash: PasswordHash
    _requested_at: datetime
    _expires_at: datetime
    _token_hash: TokenHash | None
    _issued_at: datetime | None

    def __init__(self) -> None:
        """Takes nothing and does nothing. Build one with `request`.

        Must stay, and must not raise — `User.__init__` gives the reason: without it the mapper
        installs a constructor accepting mapped attribute names, and
        `PendingRegistration(_issued_at=…)` would be a link that was never minted.
        """

    @classmethod
    def request(
        cls,
        id: PendingRegistrationId,
        email: EmailAddress,
        password_hash: PasswordHash,
        at: datetime,
        ttl: timedelta,
    ) -> PendingRegistration:
        """The only constructor: not issued, `requested_at = at`, `expires_at = at + ttl`.

        Takes a `PasswordHash`, never a `Password` — the hash is made at request time (so the
        confirm path never sees a plaintext) by the port, before this is called. Raises
        `InvariantViolated` if `ttl <= 0`. Records nothing.
        """
        raise NotImplementedError

    def is_expired(self, at: datetime) -> bool:
        """Whether `at` is at or past `expires_at` — inclusive, matching `Login.is_expired`."""
        raise NotImplementedError

    def issue(self, token_hash: TokenHash, at: datetime) -> None:
        """Store the hash of the token the worker just minted: sets `token_hash` and `issued_at = at`.

        Raises `PendingRegistrationExpired` if `is_expired(at)`, and
        `PendingRegistrationAlreadyIssued` if a token was already issued — the issued-once guard that
        makes a redelivered task send no second mail (technical plan §0.4).
        """
        raise NotImplementedError

    def confirm(self, at: datetime) -> tuple[EmailAddress, PasswordHash]:
        """Return the `(email, password_hash)` a `User` is registered from.

        Raises `PendingRegistrationNotIssued` if no token was issued, and
        `PendingRegistrationExpired` if `is_expired(at)`. Changes no state: the use case removes the
        row once the `User` is added.
        """
        raise NotImplementedError

    @property
    def id(self) -> PendingRegistrationId:
        raise NotImplementedError

    @property
    def email(self) -> EmailAddress:
        raise NotImplementedError

    @property
    def password_hash(self) -> PasswordHash:
        raise NotImplementedError

    @property
    def requested_at(self) -> datetime:
        raise NotImplementedError

    @property
    def expires_at(self) -> datetime:
        raise NotImplementedError

    @property
    def token_hash(self) -> TokenHash | None:
        raise NotImplementedError

    @property
    def issued_at(self) -> datetime | None:
        raise NotImplementedError
