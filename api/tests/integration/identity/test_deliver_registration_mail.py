"""Application tests for `DeliverRegistrationMail` (slice 2.5, T13 RED — AC-9, V-19 … V-31).

The worker's job for one queued id. Seven outcomes, each with the writes it owes and the mail it must
(or must not) send. Every double shares one call log, which is what makes the two ordering claims
provable rather than hoped for:

- **durable, then sent** — `pending.save_issued` (or `pending.remove`) precedes `mail.send:*`, so a
  crash between them leaves no mail rather than a link to nothing (§0.4);
- **no token for an existing account** — the notice path never calls `tokens.mint`.

A mail failure is **returned, never raised** (AC-9): a `MailNotDelivered` from the mailer ends here as a
`DeliveryOutcome`. `DeliveryOutcome(status)` equality also pins that `SENT` carries no reason and no
code.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import uuid4

import pytest

from tailorcraft.application.identity.deliver_registration_mail import DeliverRegistrationMail
from tailorcraft.application.identity.delivery_outcome import DeliveryOutcome, DeliveryStatus
from tailorcraft.domain.identity.account_mail import AccountAlreadyExists, ConfirmYourEmail
from tailorcraft.domain.identity.errors import MailNotDelivered
from tailorcraft.domain.identity.pending_registration import PendingRegistration
from tailorcraft.domain.identity.user import User
from tailorcraft.domain.identity.value_objects import (
    EmailAddress,
    MailFailureReason,
    PasswordHash,
    PendingRegistrationId,
    TokenHash,
)
from tailorcraft.infrastructure.clock import FixedClock
from tests.integration.fakes import (
    CountingClock,
    FakePendingRegistrationRepository,
    LoggingUserRepository,
    RecordingAccountMailer,
    RecordingOneTimeTokenPort,
)

_TTL = timedelta(hours=24)
_HASH = PasswordHash(value="$argon2id$v=19$m=65536,t=3,p=4$c29tZXNhbHQ$ZmFrZWhhc2g")
_EMAIL = EmailAddress.parse("alex@example.com")
_EARLIER_TOKEN_HASH = TokenHash(value="a" * 64)


class _Rig:
    def __init__(
        self,
        clock: FixedClock,
        *,
        failure: MailNotDelivered | None = None,
        race_issued: bool = False,
        save_error: Exception | None = None,
    ) -> None:
        self.log: list[str] = []
        self.pending = FakePendingRegistrationRepository(
            self.log, race_issued=race_issued, save_error=save_error
        )
        self.users = LoggingUserRepository(self.log)
        self.tokens = RecordingOneTimeTokenPort(self.log)
        self.mailer = RecordingAccountMailer(self.log, failure=failure)
        self.clock = CountingClock(clock)
        self.use_case = DeliverRegistrationMail(
            self.pending, self.users, self.tokens, self.mailer, self.clock
        )

    def seed_pending(self, at: datetime, *, issued: bool = False) -> PendingRegistration:
        p = PendingRegistration.request(self.pending.next_identity(), _EMAIL, _HASH, at, _TTL)
        if issued:
            p.issue(_EARLIER_TOKEN_HASH, at)
        self.pending.seed(p)
        return p

    async def seed_user(self, at: datetime) -> None:
        user = User.register_with_password(self.users.next_identity(), _EMAIL, _HASH, at)
        user.release_events()
        await self.users.add(user)
        self.log.clear()  # the seed is not part of the delivery's call sequence


def _before(log: list[str], first: str, second: str) -> bool:
    return first in log and second in log and log.index(first) < log.index(second)


async def test_a_row_that_is_gone_is_missing_and_nothing_is_written_minted_or_sent(
    clock: FixedClock,
) -> None:
    """V-19."""
    rig = _Rig(clock)

    outcome = await rig.use_case(PendingRegistrationId(value=uuid4()))

    assert outcome == DeliveryOutcome(DeliveryStatus.MISSING)
    assert rig.mailer.sent == []
    assert rig.tokens.minted == []
    assert "pending.remove" not in rig.log
    assert "pending.save_issued" not in rig.log


async def test_an_expired_row_is_deleted_and_nothing_is_sent(clock: FixedClock) -> None:
    """V-20: the worker was down past the confirmation window."""
    rig = _Rig(clock)
    p = rig.seed_pending(clock.now() - _TTL - timedelta(seconds=1))

    outcome = await rig.use_case(p.id)

    assert outcome == DeliveryOutcome(DeliveryStatus.EXPIRED)
    assert rig.pending.all() == []
    assert rig.mailer.sent == []
    assert rig.tokens.minted == []


async def test_a_row_expiring_at_exactly_this_instant_is_expired(clock: FixedClock) -> None:
    """Expiry is inclusive, like every aggregate's `is_expired`."""
    rig = _Rig(clock)
    p = rig.seed_pending(clock.now() - _TTL)

    outcome = await rig.use_case(p.id)

    assert outcome == DeliveryOutcome(DeliveryStatus.EXPIRED)
    assert rig.mailer.sent == []


async def test_an_already_issued_row_is_skipped_with_nothing_written_minted_or_sent(
    clock: FixedClock,
) -> None:
    """V-21 / V-30 / V-31: a redelivery."""
    rig = _Rig(clock)
    p = rig.seed_pending(clock.now(), issued=True)

    outcome = await rig.use_case(p.id)

    assert outcome == DeliveryOutcome(DeliveryStatus.SKIPPED)
    assert rig.tokens.minted == []
    assert rig.mailer.sent == []
    assert rig.pending.all()[0].token_hash == _EARLIER_TOKEN_HASH
    assert "pending.save_issued" not in rig.log
    assert "pending.remove" not in rig.log


async def test_a_concurrent_delivery_that_won_the_issue_makes_this_one_skip_without_sending(
    clock: FixedClock,
) -> None:
    """AC-39's guard: `save_issued` refuses a row issued meanwhile; this delivery sends no mail."""
    rig = _Rig(clock, race_issued=True)
    p = rig.seed_pending(clock.now())

    outcome = await rig.use_case(p.id)

    assert outcome == DeliveryOutcome(DeliveryStatus.SKIPPED)
    assert rig.mailer.sent == []


async def test_a_new_address_is_issued_and_committed_before_the_confirmation_link_is_sent(
    clock: FixedClock,
) -> None:
    """V-23: the happy path. The mail carries the minted plaintext token; the row carries only its
    hash; the commit precedes the send."""
    rig = _Rig(clock)
    p = rig.seed_pending(clock.now())

    outcome = await rig.use_case(p.id)

    assert outcome == DeliveryOutcome(DeliveryStatus.SENT)
    assert len(rig.tokens.minted) == 1
    minted = rig.tokens.minted[0]
    stored = rig.pending.all()[0]
    assert stored.token_hash == minted.token_hash
    assert stored.issued_at == clock.now()
    assert rig.mailer.sent == [ConfirmYourEmail(to=_EMAIL, token=minted.token, expires_in=_TTL)]
    assert _before(rig.log, "pending.save_issued", "mail.send:ConfirmYourEmail")
    assert rig.log.count("mail.send:ConfirmYourEmail") == 1


async def test_the_links_lifetime_is_what_the_row_has_left_not_a_configured_figure(
    clock: FixedClock,
) -> None:
    """The mail says how long the link *truly* has: `expires_at - now`."""
    rig = _Rig(clock)
    p = rig.seed_pending(clock.now() - timedelta(hours=2))

    await rig.use_case(p.id)

    (mail,) = rig.mailer.sent
    assert isinstance(mail, ConfirmYourEmail)
    assert mail.expires_in == _TTL - timedelta(hours=2)


async def test_an_address_that_has_an_account_gets_the_notice_after_the_row_is_deleted_and_no_token(
    clock: FixedClock,
) -> None:
    """V-22: the pending row is gone and committed **before** the notice goes out; nothing minted."""
    rig = _Rig(clock)
    p = rig.seed_pending(clock.now())
    await rig.seed_user(clock.now())

    outcome = await rig.use_case(p.id)

    assert outcome == DeliveryOutcome(DeliveryStatus.ACCOUNT_EXISTS_NOTICE_SENT)
    assert rig.mailer.sent == [AccountAlreadyExists(to=_EMAIL)]
    assert rig.pending.all() == []
    assert rig.tokens.minted == []
    assert _before(rig.log, "pending.remove", "mail.send:AccountAlreadyExists")
    assert "pending.save_issued" not in rig.log


async def test_a_notice_the_provider_rejects_is_returned_as_recipient_rejected(
    clock: FixedClock,
) -> None:
    rig = _Rig(clock, failure=MailNotDelivered(MailFailureReason.RECIPIENT_REJECTED, 550))
    p = rig.seed_pending(clock.now())
    await rig.seed_user(clock.now())

    outcome = await rig.use_case(p.id)

    assert outcome == DeliveryOutcome(DeliveryStatus.RECIPIENT_REJECTED, smtp_code=550)
    assert rig.pending.all() == []


async def test_a_rejected_recipient_deletes_the_row_and_is_returned_with_its_reply_code(
    clock: FixedClock,
) -> None:
    """V-26: the row has no future; `RECIPIENT_REJECTED` is a status, not a `FAILED` reason."""
    rig = _Rig(clock, failure=MailNotDelivered(MailFailureReason.RECIPIENT_REJECTED, 550))
    p = rig.seed_pending(clock.now())

    outcome = await rig.use_case(p.id)

    assert outcome == DeliveryOutcome(DeliveryStatus.RECIPIENT_REJECTED, smtp_code=550)
    assert rig.pending.all() == []
    assert _before(rig.log, "pending.save_issued", "mail.send:ConfirmYourEmail")
    assert _before(rig.log, "mail.send:ConfirmYourEmail", "pending.remove")


@pytest.mark.parametrize(
    ("reason", "code"),
    [
        (MailFailureReason.UNAVAILABLE, None),
        (MailFailureReason.THROTTLED, 421),
        (MailFailureReason.PROVIDER_REFUSED, 535),
    ],
)
async def test_a_send_failure_is_returned_not_raised_and_the_issued_row_stays(
    clock: FixedClock, reason: MailFailureReason, code: int | None
) -> None:
    """V-24 / V-25 / V-27 / V-28."""
    rig = _Rig(clock, failure=MailNotDelivered(reason, code))
    p = rig.seed_pending(clock.now())

    outcome = await rig.use_case(p.id)

    assert outcome == DeliveryOutcome(DeliveryStatus.FAILED, reason=reason, smtp_code=code)
    (stored,) = rig.pending.all()
    assert stored.token_hash == rig.tokens.minted[0].token_hash


async def test_a_commit_failure_before_sending_propagates_and_nothing_is_sent(
    clock: FixedClock,
) -> None:
    """V-29: the task raises (acked); no mail goes out for a row that did not commit."""

    class _CommitFailed(Exception):
        pass

    rig = _Rig(clock, save_error=_CommitFailed())
    p = rig.seed_pending(clock.now())

    with pytest.raises(_CommitFailed):
        await rig.use_case(p.id)

    assert rig.mailer.sent == []


async def test_one_clock_reading_per_delivery(clock: FixedClock) -> None:
    rig = _Rig(clock)
    p = rig.seed_pending(clock.now())

    await rig.use_case(p.id)

    assert rig.clock.calls == 1
