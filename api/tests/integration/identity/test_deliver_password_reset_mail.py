"""Application tests for `DeliverPasswordResetMail` (slice 2.5, T13 RED — AC-10, V-42 … V-44).

Read beside `test_deliver_registration_mail.py`. The rule that differs, and the reason the two
use cases are two: **no account means no mail at all** (`NO_ACCOUNT`, the row deleted), where
registration sends a notice. A reset mail to an address that has no account would be a stranger's
inbox receiving a link to nothing — or a confirmation that the address was ever looked up.

That the adapter deletes the account's *other* resets in the same commit as the issue is the
repository's contract (T23 proves it against Postgres); here the use case's share is pinned: one
`save_issued` of an aggregate now addressed to the account, the address dropped, before the send.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import uuid4

import pytest

from tailorcraft.application.identity.deliver_password_reset_mail import DeliverPasswordResetMail
from tailorcraft.application.identity.delivery_outcome import DeliveryOutcome, DeliveryStatus
from tailorcraft.domain.identity.account_mail import ResetYourPassword
from tailorcraft.domain.identity.errors import MailNotDelivered
from tailorcraft.domain.identity.password_reset import PasswordReset
from tailorcraft.domain.identity.user import User
from tailorcraft.domain.identity.value_objects import (
    EmailAddress,
    IssuedReset,
    MailFailureReason,
    PasswordHash,
    PasswordResetId,
    TokenHash,
    UserId,
)
from tailorcraft.infrastructure.clock import FixedClock
from tests.integration.fakes import (
    CountingClock,
    FakePasswordResetRepository,
    LoggingUserRepository,
    RecordingAccountMailer,
    RecordingOneTimeTokenPort,
)

_TTL = timedelta(minutes=60)
_HASH = PasswordHash(value="$argon2id$v=19$m=65536,t=3,p=4$c29tZXNhbHQ$ZmFrZWhhc2g")
_EMAIL = EmailAddress.parse("alex@example.com")
_EARLIER_TOKEN_HASH = TokenHash(value="b" * 64)


class _Rig:
    def __init__(
        self,
        clock: FixedClock,
        *,
        failure: MailNotDelivered | None = None,
        race_issued: bool = False,
    ) -> None:
        self.log: list[str] = []
        self.resets = FakePasswordResetRepository(self.log, race_issued=race_issued)
        self.users = LoggingUserRepository(self.log)
        self.tokens = RecordingOneTimeTokenPort(self.log)
        self.mailer = RecordingAccountMailer(self.log, failure=failure)
        self.clock = CountingClock(clock)
        self.use_case = DeliverPasswordResetMail(
            self.resets, self.users, self.tokens, self.mailer, self.clock
        )

    def seed_reset(self, at: datetime, *, issued_to: UserId | None = None) -> PasswordReset:
        r = PasswordReset.request(self.resets.next_identity(), _EMAIL, at, _TTL)
        if issued_to is not None:
            r.issue(issued_to, _EARLIER_TOKEN_HASH, at)
        self.resets.seed(r)
        return r

    async def seed_user(self, at: datetime) -> User:
        user = User.register_with_password(self.users.next_identity(), _EMAIL, _HASH, at)
        user.release_events()
        await self.users.add(user)
        self.log.clear()
        return user


def _before(log: list[str], first: str, second: str) -> bool:
    return first in log and second in log and log.index(first) < log.index(second)


async def test_a_row_that_is_gone_is_missing_and_nothing_is_written_minted_or_sent(
    clock: FixedClock,
) -> None:
    rig = _Rig(clock)

    outcome = await rig.use_case(PasswordResetId(value=uuid4()))

    assert outcome == DeliveryOutcome(DeliveryStatus.MISSING)
    assert rig.mailer.sent == []
    assert rig.tokens.minted == []


async def test_an_expired_reset_is_deleted_and_nothing_is_sent(clock: FixedClock) -> None:
    rig = _Rig(clock)
    await rig.seed_user(clock.now())
    r = rig.seed_reset(clock.now() - _TTL - timedelta(seconds=1))

    outcome = await rig.use_case(r.id)

    assert outcome == DeliveryOutcome(DeliveryStatus.EXPIRED)
    assert rig.resets.all() == []
    assert rig.mailer.sent == []


async def test_an_already_issued_reset_is_skipped_with_nothing_minted_or_sent(
    clock: FixedClock,
) -> None:
    rig = _Rig(clock)
    user = await rig.seed_user(clock.now())
    r = rig.seed_reset(clock.now(), issued_to=user.id)

    outcome = await rig.use_case(r.id)

    assert outcome == DeliveryOutcome(DeliveryStatus.SKIPPED)
    assert rig.tokens.minted == []
    assert rig.mailer.sent == []
    assert "resets.save_issued" not in rig.log


async def test_a_concurrent_delivery_that_won_the_issue_makes_this_one_skip_without_sending(
    clock: FixedClock,
) -> None:
    rig = _Rig(clock, race_issued=True)
    await rig.seed_user(clock.now())
    r = rig.seed_reset(clock.now())

    outcome = await rig.use_case(r.id)

    assert outcome == DeliveryOutcome(DeliveryStatus.SKIPPED)
    assert rig.mailer.sent == []


async def test_an_address_with_no_account_deletes_the_reset_and_sends_nothing_at_all(
    clock: FixedClock,
) -> None:
    """V-42: nothing minted, nothing sent, the row has no future."""
    rig = _Rig(clock)
    r = rig.seed_reset(clock.now())

    outcome = await rig.use_case(r.id)

    assert outcome == DeliveryOutcome(DeliveryStatus.NO_ACCOUNT)
    assert rig.mailer.sent == []
    assert rig.tokens.minted == []
    assert rig.resets.all() == []
    assert "resets.save_issued" not in rig.log


async def test_an_account_is_issued_the_reset_and_it_is_committed_before_the_link_is_sent(
    clock: FixedClock,
) -> None:
    """V-43: the stored reset is now the account's (the address dropped) and holds only the hash."""
    rig = _Rig(clock)
    user = await rig.seed_user(clock.now())
    r = rig.seed_reset(clock.now())

    outcome = await rig.use_case(r.id)

    assert outcome == DeliveryOutcome(DeliveryStatus.SENT)
    minted = rig.tokens.minted[0]
    (stored,) = rig.resets.all()
    assert stored.target == IssuedReset(user.id)
    assert stored.token_hash == minted.token_hash
    assert stored.issued_at == clock.now()
    assert rig.mailer.sent == [ResetYourPassword(to=_EMAIL, token=minted.token, expires_in=_TTL)]
    assert _before(rig.log, "resets.save_issued", "mail.send:ResetYourPassword")
    assert rig.log.count("mail.send:ResetYourPassword") == 1


async def test_the_links_lifetime_is_what_the_row_has_left(clock: FixedClock) -> None:
    rig = _Rig(clock)
    await rig.seed_user(clock.now())
    r = rig.seed_reset(clock.now() - timedelta(minutes=10))

    await rig.use_case(r.id)

    (mail,) = rig.mailer.sent
    assert isinstance(mail, ResetYourPassword)
    assert mail.expires_in == _TTL - timedelta(minutes=10)


async def test_a_rejected_recipient_deletes_the_reset_and_is_returned_with_its_reply_code(
    clock: FixedClock,
) -> None:
    rig = _Rig(clock, failure=MailNotDelivered(MailFailureReason.RECIPIENT_REJECTED, 550))
    await rig.seed_user(clock.now())
    r = rig.seed_reset(clock.now())

    outcome = await rig.use_case(r.id)

    assert outcome == DeliveryOutcome(DeliveryStatus.RECIPIENT_REJECTED, smtp_code=550)
    assert rig.resets.all() == []


@pytest.mark.parametrize(
    ("reason", "code"),
    [
        (MailFailureReason.UNAVAILABLE, None),
        (MailFailureReason.THROTTLED, 450),
        (MailFailureReason.PROVIDER_REFUSED, 535),
    ],
)
async def test_a_send_failure_is_returned_not_raised_and_the_issued_reset_stays(
    clock: FixedClock, reason: MailFailureReason, code: int | None
) -> None:
    """V-44."""
    rig = _Rig(clock, failure=MailNotDelivered(reason, code))
    await rig.seed_user(clock.now())
    r = rig.seed_reset(clock.now())

    outcome = await rig.use_case(r.id)

    assert outcome == DeliveryOutcome(DeliveryStatus.FAILED, reason=reason, smtp_code=code)
    (stored,) = rig.resets.all()
    assert stored.token_hash == rig.tokens.minted[0].token_hash


async def test_one_clock_reading_per_delivery(clock: FixedClock) -> None:
    rig = _Rig(clock)
    await rig.seed_user(clock.now())
    r = rig.seed_reset(clock.now())

    await rig.use_case(r.id)

    assert rig.clock.calls == 1
