"""Application tests for `RecordPackClick` (slice 5.2, T13 RED, AC-25, AC-27's two pack counters).

`pack_clicks` counts every click; `pack_clickers` only the account's first click ever (OQ-2). A click
charges nothing. Both counters go to `at.date()` (UTC), proven with an instant a second before
midnight so a `date.today()` or a next-day bug cannot pass.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tailorcraft.application.billing.record_pack_click import (
    RecordPackClick,
    RecordPackClickCommand,
)
from tailorcraft.domain.billing.errors import CreditAccountNotFound
from tailorcraft.domain.billing.events import PackClicked
from tailorcraft.domain.billing.value_objects import Pack
from tailorcraft.domain.identity.value_objects import UserId
from tailorcraft.domain.shared.counters import DemandCounter
from tests.integration.billing.support import a_user_id, seed_account
from tests.integration.fakes import (
    FakeCreditAccountRepository,
    RecordingDailyCounter,
    RecordingEventPublisher,
)

_AT = datetime(2026, 11, 7, 23, 59, 59, tzinfo=UTC)


class _Rig:
    def __init__(self) -> None:
        self.credits = FakeCreditAccountRepository()
        self.counters = RecordingDailyCounter()
        self.events = RecordingEventPublisher()
        self.use_case = RecordPackClick(self.credits, self.counters, self.events)

    async def click(self, uid: UserId, pack: Pack = Pack.PACK_3) -> bool:
        return await self.use_case(RecordPackClickCommand(user_id=uid, pack=pack, at=_AT))


async def test_a_first_click_counts_a_click_and_a_clicker_on_the_clicks_utc_day() -> None:
    rig = _Rig()
    uid = a_user_id()
    seed_account(rig.credits, uid, _AT)

    await rig.click(uid)

    assert rig.counters.count(DemandCounter.PACK_CLICKS, _AT.date()) == 1
    assert rig.counters.count(DemandCounter.PACK_CLICKERS, _AT.date()) == 1
    assert len(rig.counters.calls) == 2


async def test_a_second_click_by_the_same_account_counts_a_click_but_no_new_clicker() -> None:
    rig = _Rig()
    uid = a_user_id()
    seed_account(rig.credits, uid, _AT)
    await rig.click(uid, Pack.PACK_3)

    await rig.click(uid, Pack.PACK_10)

    assert rig.counters.count(DemandCounter.PACK_CLICKS, _AT.date()) == 2
    assert rig.counters.count(DemandCounter.PACK_CLICKERS, _AT.date()) == 1


async def test_two_accounts_are_two_clickers() -> None:
    rig = _Rig()
    first, second = a_user_id(), a_user_id()
    seed_account(rig.credits, first, _AT)
    seed_account(rig.credits, second, _AT)

    await rig.click(first)
    await rig.click(second)

    assert rig.counters.count(DemandCounter.PACK_CLICKERS, _AT.date()) == 2


async def test_a_click_charges_nothing() -> None:
    rig = _Rig()
    uid = a_user_id()
    seed_account(rig.credits, uid, _AT)

    await rig.click(uid)

    account = await rig.credits.find(uid)
    assert account is not None
    assert account.balance == 1
    assert len(account.entries) == 1


@pytest.mark.parametrize("opted_in", [False, True])
async def test_it_answers_whether_the_account_is_opted_in_to_the_payment_notice(
    opted_in: bool,
) -> None:
    rig = _Rig()
    uid = a_user_id()
    account = seed_account(rig.credits, uid, _AT)
    if opted_in:
        account.choose_payment_notice(True, at=_AT)
        rig.credits.seed(account)

    assert await rig.click(uid) is opted_in


async def test_each_click_publishes_a_pack_clicked_event_flagging_only_the_first() -> None:
    rig = _Rig()
    uid = a_user_id()
    seed_account(rig.credits, uid, _AT)

    await rig.click(uid, Pack.PACK_10)
    await rig.click(uid, Pack.PACK_10)

    assert [e for e in rig.events.published if isinstance(e, PackClicked)] == [
        PackClicked(occurred_at=_AT, pack=Pack.PACK_10, first_click=True),
        PackClicked(occurred_at=_AT, pack=Pack.PACK_10, first_click=False),
    ]


async def test_the_click_goes_through_the_account_lock_and_is_saved() -> None:
    rig = _Rig()
    uid = a_user_id()
    seed_account(rig.credits, uid, _AT)

    await rig.click(uid)

    assert (rig.credits.lock_calls, rig.credits.save_calls) == (1, 1)


async def test_a_user_with_no_account_cannot_click_and_nothing_is_counted() -> None:
    rig = _Rig()

    with pytest.raises(CreditAccountNotFound):
        await rig.click(a_user_id())

    assert rig.counters.calls == []
    assert rig.events.published == []
