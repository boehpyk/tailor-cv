"""Application tests for `RefundCreditForRun` (slice 5.2, T13 RED, AC-9, AC-10, CR-4, CR-5).

Every no-op below must write nothing and raise nothing: a refund that finds no charge is ordinary
(a run made by the old API during the deploy window, a guest run claimed before release), never an
error. The refund takes no lock (plan §0.4), so it reads through `find`.
"""

from __future__ import annotations

from tailorcraft.application.billing.charge_credit_for_run import (
    ChargeCreditCommand,
    ChargeCreditForRun,
)
from tailorcraft.application.billing.refund_credit_for_run import (
    RefundCreditCommand,
    RefundCreditForRun,
)
from tailorcraft.domain.billing.events import CreditRefunded
from tailorcraft.domain.billing.value_objects import ChargedRunRef, CreditEntryKind
from tailorcraft.domain.identity.ownership import GuestOwner, UserOwner
from tailorcraft.domain.identity.value_objects import GuestSessionId, UserId
from tailorcraft.infrastructure.clock import FixedClock
from tests.integration.billing.support import a_run_ref, a_user_id, seed_account
from tests.integration.fakes import FakeCreditAccountRepository


async def _charged(
    credits: FakeCreditAccountRepository, uid: UserId, clock: FixedClock, *, extra: int = 0
) -> ChargedRunRef:
    run = a_run_ref()
    seed_account(credits, uid, clock.now(), extra=extra)
    await ChargeCreditForRun(credits)(ChargeCreditCommand(user_id=uid, run=run, at=clock.now()))
    return run


async def test_refunding_a_charged_run_gives_the_credit_back_and_returns_the_event(
    clock: FixedClock,
) -> None:
    credits = FakeCreditAccountRepository()
    uid = a_user_id()
    run = await _charged(credits, uid, clock)

    events = await RefundCreditForRun(credits)(
        RefundCreditCommand(owner=UserOwner(uid), run=run, at=clock.now())
    )

    account = await credits.find(uid)
    assert account is not None
    assert account.balance == 1
    refund = account.entries[-1]
    assert (refund.kind, refund.credits, refund.run, refund.at) == (
        CreditEntryKind.REFUND,
        1,
        run,
        clock.now(),
    )
    assert [type(e) for e in events] == [CreditRefunded]
    (refunded,) = events
    assert isinstance(refunded, CreditRefunded)
    assert (refunded.run, refunded.entry_id) == (run, refund.id)


async def test_a_refund_takes_no_lock(clock: FixedClock) -> None:
    credits = FakeCreditAccountRepository()
    uid = a_user_id()
    run = await _charged(credits, uid, clock)
    locks_before = credits.lock_calls

    await RefundCreditForRun(credits)(
        RefundCreditCommand(owner=UserOwner(uid), run=run, at=clock.now())
    )

    assert credits.lock_calls == locks_before


async def test_a_guest_owned_run_refunds_nothing_and_reads_no_account(clock: FixedClock) -> None:
    credits = FakeCreditAccountRepository()
    find_before = credits.find_calls

    events = await RefundCreditForRun(credits)(
        RefundCreditCommand(
            owner=GuestOwner(GuestSessionId(value=a_user_id().value)),
            run=a_run_ref(),
            at=clock.now(),
        )
    )

    assert tuple(events) == ()
    assert credits.find_calls == find_before
    assert credits.save_calls == 0


async def test_a_user_with_no_account_refunds_nothing_and_does_not_raise(
    clock: FixedClock,
) -> None:
    credits = FakeCreditAccountRepository()

    events = await RefundCreditForRun(credits)(
        RefundCreditCommand(owner=UserOwner(a_user_id()), run=a_run_ref(), at=clock.now())
    )

    assert tuple(events) == ()
    assert credits.save_calls == 0


async def test_a_run_that_was_never_charged_refunds_nothing(clock: FixedClock) -> None:
    credits = FakeCreditAccountRepository()
    uid = a_user_id()
    seed_account(credits, uid, clock.now())

    events = await RefundCreditForRun(credits)(
        RefundCreditCommand(owner=UserOwner(uid), run=a_run_ref(), at=clock.now())
    )

    account = await credits.find(uid)
    assert account is not None
    assert tuple(events) == ()
    assert account.balance == 1
    assert len(account.entries) == 1
    assert credits.save_calls == 0


async def test_a_run_charged_to_someone_else_is_not_refunded_to_this_account(
    clock: FixedClock,
) -> None:
    credits = FakeCreditAccountRepository()
    payer, other = a_user_id(), a_user_id()
    run = await _charged(credits, payer, clock)
    seed_account(credits, other, clock.now())

    events = await RefundCreditForRun(credits)(
        RefundCreditCommand(owner=UserOwner(other), run=run, at=clock.now())
    )

    account = await credits.find(other)
    assert account is not None
    assert tuple(events) == ()
    assert account.balance == 1


async def test_a_second_refund_of_the_same_run_writes_nothing(clock: FixedClock) -> None:
    credits = FakeCreditAccountRepository()
    uid = a_user_id()
    run = await _charged(credits, uid, clock)
    refund = RefundCreditForRun(credits)
    await refund(RefundCreditCommand(owner=UserOwner(uid), run=run, at=clock.now()))
    saves_before = credits.save_calls

    events = await refund(RefundCreditCommand(owner=UserOwner(uid), run=run, at=clock.now()))

    account = await credits.find(uid)
    assert account is not None
    assert tuple(events) == ()
    assert account.balance == 1  # not 2
    assert credits.save_calls == saves_before
