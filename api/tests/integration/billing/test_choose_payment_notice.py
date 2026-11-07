"""Application tests for `ChoosePaymentNotice` (slice 5.2, T13 RED, AC-26's use-case half).

Consent only: this slice sends no mail, and the use case holds no mail port to send one with.
"""

from __future__ import annotations

import pytest

from tailorcraft.application.billing.choose_payment_notice import (
    ChoosePaymentNotice,
    ChoosePaymentNoticeCommand,
)
from tailorcraft.domain.billing.errors import CreditAccountNotFound
from tailorcraft.domain.billing.events import PaymentNoticeChosen
from tailorcraft.domain.identity.value_objects import UserId
from tailorcraft.infrastructure.clock import FixedClock
from tests.integration.billing.support import a_user_id, seed_account
from tests.integration.fakes import FakeCreditAccountRepository, RecordingEventPublisher


class _Rig:
    def __init__(self) -> None:
        self.credits = FakeCreditAccountRepository()
        self.events = RecordingEventPublisher()
        self.use_case = ChoosePaymentNotice(self.credits, self.events)

    async def choose(self, uid: UserId, opted_in: bool, clock: FixedClock) -> None:
        await self.use_case(
            ChoosePaymentNoticeCommand(user_id=uid, opted_in=opted_in, at=clock.now())
        )


async def test_opting_in_records_consent_at_the_instant_and_publishes_one_event(
    clock: FixedClock,
) -> None:
    rig = _Rig()
    uid = a_user_id()
    seed_account(rig.credits, uid, clock.now())

    await rig.choose(uid, True, clock)

    account = await rig.credits.find(uid)
    assert account is not None
    assert account.payment_notice_opted_in_at == clock.now()
    assert rig.events.published == [PaymentNoticeChosen(occurred_at=clock.now(), opted_in=True)]
    assert rig.credits.save_calls == 1


async def test_opting_in_again_changes_nothing_and_publishes_nothing(clock: FixedClock) -> None:
    rig = _Rig()
    uid = a_user_id()
    seed_account(rig.credits, uid, clock.now())
    await rig.choose(uid, True, clock)
    first_instant = clock.now()
    clock.advance(300)
    published_before = len(rig.events.published)

    await rig.choose(uid, True, clock)

    account = await rig.credits.find(uid)
    assert account is not None
    assert account.payment_notice_opted_in_at == first_instant
    assert len(rig.events.published) == published_before


async def test_opting_out_clears_consent_and_publishes_the_change(clock: FixedClock) -> None:
    rig = _Rig()
    uid = a_user_id()
    seed_account(rig.credits, uid, clock.now())
    await rig.choose(uid, True, clock)

    await rig.choose(uid, False, clock)

    account = await rig.credits.find(uid)
    assert account is not None
    assert account.payment_notice_opted_in_at is None
    assert [e for e in rig.events.published if isinstance(e, PaymentNoticeChosen)][-1] == (
        PaymentNoticeChosen(occurred_at=clock.now(), opted_in=False)
    )


async def test_opting_out_when_never_opted_in_is_a_quiet_no_op(clock: FixedClock) -> None:
    rig = _Rig()
    uid = a_user_id()
    seed_account(rig.credits, uid, clock.now())

    await rig.choose(uid, False, clock)

    account = await rig.credits.find(uid)
    assert account is not None
    assert account.payment_notice_opted_in_at is None
    assert rig.events.published == []


async def test_choosing_changes_no_credit(clock: FixedClock) -> None:
    rig = _Rig()
    uid = a_user_id()
    seed_account(rig.credits, uid, clock.now())

    await rig.choose(uid, True, clock)

    account = await rig.credits.find(uid)
    assert account is not None
    assert (account.balance, len(account.entries)) == (1, 1)


async def test_a_user_with_no_account_cannot_choose(clock: FixedClock) -> None:
    rig = _Rig()

    with pytest.raises(CreditAccountNotFound):
        await rig.choose(a_user_id(), True, clock)

    assert rig.events.published == []


async def test_choosing_goes_through_the_account_lock(clock: FixedClock) -> None:
    rig = _Rig()
    uid = a_user_id()
    seed_account(rig.credits, uid, clock.now())

    await rig.choose(uid, True, clock)

    assert rig.credits.lock_calls == 1
