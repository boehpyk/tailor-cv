"""The `DeliverRegistrationMail` use case: the worker's job for one pending registration — decide
which mail goes, issue a token if one is needed, commit, then send (slice 2.5, technical plan §0.2,
§0.4, AC-9, V-19 … V-31).

**This is where the address is looked up**, and the only place: the request path may not ask
(§0.2), and nobody can time a worker.

**Commit, then send.** Every write here goes through a committing adapter (`remove`, `save_issued`
are durable on return), so "commit, then send" is simply the order of two calls: a crash between them
leaves no mail rather than a dead link, and a redelivery finds the row issued and skips (AC-39).

**This is the one place a plaintext token crosses `application/`** (§0.4): minted here from
`OneTimeTokenPort`, its hash stored on the aggregate, and the `OneTimeToken` itself — masked in every
`repr` — handed to `AccountMailPort.send` and nowhere else.

**Never raises for a mail failure.** `MailNotDelivered` becomes a returned `DeliveryOutcome`; this
layer does not log, and the task logs the outcome.

**The link's `expires_in` is derived, not configured**: `pending.expires_at - now`, the time the link
truly has left when it is mailed. A setting passed in here would be a second copy of the lifetime the
row already carries, and could only disagree with it.
"""

from __future__ import annotations

from tailorcraft.application.identity.delivery_outcome import (
    DeliveryOutcome,
    DeliveryStatus,
    outcome_of_failed_send,
)
from tailorcraft.domain.identity.account_mail import AccountAlreadyExists, ConfirmYourEmail
from tailorcraft.domain.identity.errors import MailNotDelivered, PendingRegistrationAlreadyIssued
from tailorcraft.domain.identity.ports import (
    AccountMailPort,
    OneTimeTokenPort,
    PendingRegistrationRepository,
    UserRepository,
)
from tailorcraft.domain.identity.value_objects import MailFailureReason, PendingRegistrationId
from tailorcraft.domain.shared.clock import Clock


class DeliverRegistrationMail:
    """Deliver the mail for the pending registration `pending_id`.

    Flow of `__call__` (technical plan §2), with **one `clock.now()`**:

    1. `p = await pending.get(pending_id)`; `None` → `MISSING`.
    2. `p.is_expired(now)` → `await pending.remove(p.id)` → `EXPIRED`.
    3. `p.token_hash is not None` (already issued: a redelivery) → `SKIPPED`.
    4. `await users.find_by_email(p.email)` found → `await pending.remove(p.id)` (committed),
       **then** `send(AccountAlreadyExists(p.email))` → `ACCOUNT_EXISTS_NOTICE_SENT`. No token minted.
    5. Otherwise `minted = tokens.mint()` → `p.issue(minted.token_hash, now)` →
       `await pending.save_issued(p)` (committed; `PendingRegistrationAlreadyIssued` from it — a
       concurrent delivery won — → `SKIPPED`) → **then**
       `send(ConfirmYourEmail(p.email, minted.token, p.expires_at - now))` → `SENT`.
    6. `MailNotDelivered(RECIPIENT_REJECTED, code)` from a `send` → `await pending.remove(p.id)` →
       `RECIPIENT_REJECTED` (with `smtp_code`); any other `MailNotDelivered(reason, code)` →
       `FAILED` with `reason` and `smtp_code`, the row left as it is.
    """

    def __init__(
        self,
        pending: PendingRegistrationRepository,
        users: UserRepository,
        tokens: OneTimeTokenPort,
        mailer: AccountMailPort,
        clock: Clock,
    ) -> None:
        self._pending = pending
        self._users = users
        self._tokens = tokens
        self._mailer = mailer
        self._clock = clock

    async def __call__(self, pending_id: PendingRegistrationId) -> DeliveryOutcome:
        now = self._clock.now()

        pending = await self._pending.get(pending_id)
        if pending is None:
            return DeliveryOutcome(DeliveryStatus.MISSING)
        if pending.is_expired(now):
            await self._pending.remove(pending.id)
            return DeliveryOutcome(DeliveryStatus.EXPIRED)
        if pending.token_hash is not None:
            return DeliveryOutcome(DeliveryStatus.SKIPPED)

        if await self._users.find_by_email(pending.email) is not None:
            # The row is deleted and committed before the notice goes; no token is minted, because
            # there is nothing to confirm.
            await self._pending.remove(pending.id)
            try:
                await self._mailer.send(AccountAlreadyExists(to=pending.email))
            except MailNotDelivered as failure:
                # The row is already gone, so a rejected recipient has nothing left to delete.
                return outcome_of_failed_send(failure)
            return DeliveryOutcome(DeliveryStatus.ACCOUNT_EXISTS_NOTICE_SENT)

        minted = self._tokens.mint()
        pending.issue(minted.token_hash, now)
        try:
            await self._pending.save_issued(pending)
        except PendingRegistrationAlreadyIssued:
            # A concurrent delivery of the same id issued it first and sends its own mail.
            return DeliveryOutcome(DeliveryStatus.SKIPPED)

        # Committed above, sent below: a crash between the two leaves no mail, never a dead link.
        try:
            await self._mailer.send(
                ConfirmYourEmail(
                    to=pending.email, token=minted.token, expires_in=pending.expires_at - now
                )
            )
        except MailNotDelivered as failure:
            if failure.reason is MailFailureReason.RECIPIENT_REJECTED:
                await self._pending.remove(pending.id)
            return outcome_of_failed_send(failure)
        return DeliveryOutcome(DeliveryStatus.SENT)
