"""The `DeliverPasswordResetMail` use case: the worker's job for one requested reset — find the
account, issue a token, commit (superseding the account's other resets), then send (slice 2.5,
technical plan §0.2, §0.4, AC-10, V-42 … V-44).

Read it beside `DeliverRegistrationMail`. The two share a shape and are deliberately two classes:
what differs is the rule that matters — **no account means no mail at all** here (`NO_ACCOUNT`, the
row deleted), where registration sends a notice instead. A shared base would have to guess which.

Commit, then send; the plaintext token crosses this layer only from `OneTimeTokenPort.mint` to
`AccountMailPort.send`; a mail failure is returned, never raised; `expires_in` is derived from the
row (`reset.expires_at - now`). Each for `deliver_registration_mail.py`'s reasons.
"""

from __future__ import annotations

from typing import assert_never

from tailorcraft.application.identity.delivery_outcome import (
    DeliveryOutcome,
    DeliveryStatus,
    outcome_of_failed_send,
)
from tailorcraft.domain.identity.account_mail import ResetYourPassword
from tailorcraft.domain.identity.errors import MailNotDelivered, PasswordResetAlreadyIssued
from tailorcraft.domain.identity.ports import (
    AccountMailPort,
    OneTimeTokenPort,
    PasswordResetRepository,
    UserRepository,
)
from tailorcraft.domain.identity.value_objects import (
    AddressedReset,
    IssuedReset,
    MailFailureReason,
    PasswordResetId,
)
from tailorcraft.domain.shared.clock import Clock


class DeliverPasswordResetMail:
    """Deliver the mail for the password reset `reset_id`.

    Flow of `__call__` (technical plan §2), with **one `clock.now()`**:

    1. `r = await resets.get(reset_id)`; `None` → `MISSING`.
    2. `r.is_expired(now)` → `await resets.remove(r.id)` → `EXPIRED`.
    3. `r.token_hash is not None` (issued: a redelivery) → `SKIPPED`.
    4. `user = await users.find_by_email(r.target.email)` (`AddressedReset`); `None` →
       `await resets.remove(r.id)` → `NO_ACCOUNT`. **Nothing sent.**
    5. `minted = tokens.mint()` → `r.issue(user.id, minted.token_hash, now)` →
       `await resets.save_issued(r)` (committed, and the account's other resets deleted;
       `PasswordResetAlreadyIssued` → `SKIPPED`) → **then**
       `send(ResetYourPassword(user.email, minted.token, r.expires_at - now))` → `SENT`.
    6. `MailNotDelivered(RECIPIENT_REJECTED, code)` → `await resets.remove(r.id)` →
       `RECIPIENT_REJECTED`; any other `MailNotDelivered(reason, code)` → `FAILED`, row left issued.
    """

    def __init__(
        self,
        resets: PasswordResetRepository,
        users: UserRepository,
        tokens: OneTimeTokenPort,
        mailer: AccountMailPort,
        clock: Clock,
    ) -> None:
        self._resets = resets
        self._users = users
        self._tokens = tokens
        self._mailer = mailer
        self._clock = clock

    async def __call__(self, reset_id: PasswordResetId) -> DeliveryOutcome:
        now = self._clock.now()

        reset = await self._resets.get(reset_id)
        if reset is None:
            return DeliveryOutcome(DeliveryStatus.MISSING)
        if reset.is_expired(now):
            await self._resets.remove(reset.id)
            return DeliveryOutcome(DeliveryStatus.EXPIRED)

        match reset.target:
            case IssuedReset():
                return DeliveryOutcome(DeliveryStatus.SKIPPED)
            case AddressedReset(email=email):
                pass
            case _:
                assert_never(reset.target)

        user = await self._users.find_by_email(email)
        if user is None:
            # No account, no mail of any kind (registration sends a notice here; a reset does not).
            await self._resets.remove(reset.id)
            return DeliveryOutcome(DeliveryStatus.NO_ACCOUNT)

        minted = self._tokens.mint()
        reset.issue(user.id, minted.token_hash, now)
        try:
            # Durable on return, and supersedes the account's other resets in the same commit.
            await self._resets.save_issued(reset)
        except PasswordResetAlreadyIssued:
            return DeliveryOutcome(DeliveryStatus.SKIPPED)

        try:
            await self._mailer.send(
                ResetYourPassword(
                    to=user.email, token=minted.token, expires_in=reset.expires_at - now
                )
            )
        except MailNotDelivered as failure:
            if failure.reason is MailFailureReason.RECIPIENT_REJECTED:
                await self._resets.remove(reset.id)
            return outcome_of_failed_send(failure)
        return DeliveryOutcome(DeliveryStatus.SENT)
