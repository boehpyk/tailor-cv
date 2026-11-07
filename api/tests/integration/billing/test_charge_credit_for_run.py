"""Application tests for `ChargeCreditForRun` (slice 5.2, T13 RED, AC-6, AC-7, CR-2, CR-3).

Against the fake repository, which takes no real lock: that the lock *serializes* two charges (F-2,
AC-8, AC-13) needs the real adapter and is not asserted here. What is asserted is that the charge
goes through `lock_for_charge` (never the unlocked `find`), writes only on success, and hands back
the account's events instead of publishing them.
"""

from __future__ import annotations

import pytest

from tailorcraft.application.billing.charge_credit_for_run import (
    ChargeCreditCommand,
    ChargeCreditForRun,
)
from tailorcraft.domain.billing.errors import (
    CreditAccountNotFound,
    NoCreditsLeft,
    RunAlreadyCharged,
)
from tailorcraft.domain.billing.events import CreditCharged
from tailorcraft.domain.billing.value_objects import CreditEntryKind
from tailorcraft.infrastructure.clock import FixedClock
from tests.integration.billing.support import a_run_ref, a_user_id, seed_account
from tests.integration.fakes import FakeCreditAccountRepository


@pytest.mark.parametrize("balance", [1, 2, 7])
async def test_a_charge_takes_exactly_one_credit(balance: int, clock: FixedClock) -> None:
    credits = FakeCreditAccountRepository()
    uid, run = a_user_id(), a_run_ref()
    seed_account(credits, uid, clock.now(), extra=balance - 1)

    await ChargeCreditForRun(credits)(ChargeCreditCommand(user_id=uid, run=run, at=clock.now()))

    account = await credits.find(uid)
    assert account is not None
    assert account.balance == balance - 1
    charge = account.entries[-1]
    assert (charge.kind, charge.credits, charge.run, charge.at) == (
        CreditEntryKind.CHARGE,
        -1,
        run,
        clock.now(),
    )
    # the opening entry, the grant (only when balance > 1) and the charge: one entry added
    assert len(account.entries) == (3 if balance > 1 else 2)


async def test_the_charge_is_saved_and_its_event_is_returned_not_published(
    clock: FixedClock,
) -> None:
    credits = FakeCreditAccountRepository()
    uid, run = a_user_id(), a_run_ref()
    seed_account(credits, uid, clock.now())

    events = await ChargeCreditForRun(credits)(
        ChargeCreditCommand(user_id=uid, run=run, at=clock.now())
    )

    assert credits.save_calls == 1
    account = await credits.find(uid)
    assert account is not None
    assert [type(e) for e in events] == [CreditCharged]
    (charged,) = events
    assert isinstance(charged, CreditCharged)
    assert (charged.run, charged.entry_id) == (run, account.entries[-1].id)


async def test_a_charge_reads_through_the_locking_method_and_never_the_unlocked_one(
    clock: FixedClock,
) -> None:
    credits = FakeCreditAccountRepository()
    uid = a_user_id()
    seed_account(credits, uid, clock.now())
    find_before = credits.find_calls

    await ChargeCreditForRun(credits)(
        ChargeCreditCommand(user_id=uid, run=a_run_ref(), at=clock.now())
    )

    assert credits.lock_calls == 1
    assert credits.find_calls == find_before


async def test_a_charge_at_balance_zero_is_refused_and_writes_nothing(clock: FixedClock) -> None:
    credits = FakeCreditAccountRepository()
    uid = a_user_id()
    seed_account(credits, uid, clock.now())
    first = a_run_ref()
    await ChargeCreditForRun(credits)(ChargeCreditCommand(user_id=uid, run=first, at=clock.now()))
    saves_before = credits.save_calls

    with pytest.raises(NoCreditsLeft):
        await ChargeCreditForRun(credits)(
            ChargeCreditCommand(user_id=uid, run=a_run_ref(), at=clock.now())
        )

    account = await credits.find(uid)
    assert account is not None
    assert account.balance == 0
    assert len(account.entries) == 2  # the welcome entry and the first charge: nothing added
    assert credits.save_calls == saves_before


async def test_charging_the_same_run_twice_is_refused_and_takes_one_credit_only(
    clock: FixedClock,
) -> None:
    credits = FakeCreditAccountRepository()
    uid, run = a_user_id(), a_run_ref()
    seed_account(credits, uid, clock.now(), extra=2)
    charge = ChargeCreditForRun(credits)
    await charge(ChargeCreditCommand(user_id=uid, run=run, at=clock.now()))

    with pytest.raises(RunAlreadyCharged):
        await charge(ChargeCreditCommand(user_id=uid, run=run, at=clock.now()))

    account = await credits.find(uid)
    assert account is not None
    assert account.balance == 2


async def test_a_user_with_no_account_cannot_be_charged(clock: FixedClock) -> None:
    credits = FakeCreditAccountRepository()

    with pytest.raises(CreditAccountNotFound):
        await ChargeCreditForRun(credits)(
            ChargeCreditCommand(user_id=a_user_id(), run=a_run_ref(), at=clock.now())
        )

    assert credits.save_calls == 0


async def test_a_charge_touches_only_the_chargers_account(clock: FixedClock) -> None:
    credits = FakeCreditAccountRepository()
    uid, other = a_user_id(), a_user_id()
    seed_account(credits, uid, clock.now())
    seed_account(credits, other, clock.now())

    await ChargeCreditForRun(credits)(
        ChargeCreditCommand(user_id=uid, run=a_run_ref(), at=clock.now())
    )

    untouched = await credits.find(other)
    assert untouched is not None
    assert untouched.balance == 1
