"""The `PasswordReset` aggregate: a request to prove an address and set a new password (ADR-0028).

**Two states, and the type says which.** A reset is requested for an *address* (`AddressedReset`) —
the request path never looks the account up (technical plan §0.2) — and the worker, finding an
account, issues it to that *account* (`IssuedReset`), dropping the address: the account row holds it,
so this one need not (data minimisation). **Invariant:** issued ⇔ the target is an `IssuedReset` ⇔ a
token hash is present.

Not a sibling of `PendingRegistration`, although the two have the same lifecycle shape (request →
issue once → use, inclusive expiry). Shared shape is not shared behaviour (CLAUDE.md): one becomes a
`User`, the other changes one and revokes its logins; one holds a password hash, the other must
never; and their supersede rules differ (per address vs per account). No base class, no shared
`Protocol`.

**Records no events.** `PasswordChangedByReset` is recorded by the `User` whose password changed.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from tailorcraft.domain.identity.value_objects import (
    EmailAddress,
    PasswordResetId,
    ResetTarget,
    TokenHash,
    UserId,
)
from tailorcraft.domain.shared.events import RecordsEvents


# NOT `slots=True`: mapped imperatively — see `user.py`. The private-attribute names below are what
# `infrastructure/persistence/mapping/identity/password_reset.py` will target (ADR-0007); `_target`
# is translated there into two columns (`email`, `user_id`), as `Owner` is (ADR-0022).
class PasswordReset(RecordsEvents):
    """A password reset, addressed or issued.

    State: `_id`, `_target`, `_requested_at`, `_expires_at`, `_token_hash` (`None` until issued),
    `_issued_at` (`None` until issued) — private, exposed only through read-only properties.
    """

    _id: PasswordResetId
    _target: ResetTarget
    _requested_at: datetime
    _expires_at: datetime
    _token_hash: TokenHash | None
    _issued_at: datetime | None

    def __init__(self) -> None:
        """Takes nothing and does nothing. Build one with `request`.

        Must stay, and must not raise — `User.__init__` gives the reason.
        """

    @classmethod
    def request(
        cls,
        id: PasswordResetId,
        email: EmailAddress,
        at: datetime,
        ttl: timedelta,
    ) -> PasswordReset:
        """The only constructor: target `AddressedReset(email)`, not issued, `requested_at = at`,
        `expires_at = at + ttl`. Raises `InvariantViolated` if `ttl <= 0`. Records nothing."""
        raise NotImplementedError

    def is_expired(self, at: datetime) -> bool:
        """Whether `at` is at or past `expires_at` — inclusive, matching `Login.is_expired`."""
        raise NotImplementedError

    def issue(self, user_id: UserId, token_hash: TokenHash, at: datetime) -> None:
        """Address the reset to the account found for its address: target becomes
        `IssuedReset(user_id)` (**the address is dropped**), `token_hash` and `issued_at = at` set.

        Raises `PasswordResetExpired` if `is_expired(at)`, and `PasswordResetAlreadyIssued` if it was
        already issued (the issued-once guard, technical plan §0.4).
        """
        raise NotImplementedError

    @property
    def id(self) -> PasswordResetId:
        raise NotImplementedError

    @property
    def target(self) -> ResetTarget:
        raise NotImplementedError

    @property
    def user_id(self) -> UserId:
        """The account this reset was issued to. Raises `PasswordResetNotIssued` while the reset is
        still only addressed — it has no account yet, by construction."""
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
