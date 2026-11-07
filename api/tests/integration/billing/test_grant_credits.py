"""Application tests for `GrantCredits` (slice 5.2, T13 RED, AC-30's use-case half).

The CLI's exit codes and printed lines are T25/T30's. Here: the result's before and after, the dry
run writing nothing, and the grant going through the charge's own lock.
"""

from __future__ import annotations

import pytest

from tailorcraft.application.billing.grant_credits import (
    GrantCredits,
    GrantCreditsCommand,
    GrantCreditsResult,
)
from tailorcraft.domain.billing.errors import CreditAccountNotFound
from tailorcraft.domain.billing.events import CreditsGranted
from tailorcraft.domain.billing.value_objects import CreditEntryKind, GrantAmount
from tailorcraft.infrastructure.clock import FixedClock
from tests.integration.billing.support import a_user_id, seed_account
from tests.integration.fakes import FakeCreditAccountRepository, RecordingEventPublisher


def _grant(
    credits: FakeCreditAccountRepository, clock: FixedClock, events: RecordingEventPublisher
) -> GrantCredits:
    return GrantCredits(credits, clock, events)


async def test_a_grant_adds_the_amount_and_reports_before_and_after(clock: FixedClock) -> None:
    credits = FakeCreditAccountRepository()
    uid = a_user_id()
    seed_account(credits, uid, clock.now())

    result = await _grant(credits, clock, RecordingEventPublisher())(
        GrantCreditsCommand(user_id=uid, amount=GrantAmount(5), dry_run=False)
    )

    assert result == GrantCreditsResult(before=1, after=6)
    account = await credits.find(uid)
    assert account is not None
    assert account.balance == 6
    entry = account.entries[-1]
    assert (entry.kind, entry.credits, entry.run, entry.at) == (
        CreditEntryKind.GRANT,
        5,
        None,
        clock.now(),
    )


async def test_a_grant_publishes_one_event_after_the_save_and_through_the_lock(
    clock: FixedClock,
) -> None:
    credits = FakeCreditAccountRepository()
    uid = a_user_id()
    seed_account(credits, uid, clock.now())
    events = RecordingEventPublisher()

    await _grant(credits, clock, events)(
        GrantCreditsCommand(user_id=uid, amount=GrantAmount(3), dry_run=False)
    )

    assert [type(e) for e in events.published] == [CreditsGranted]
    (granted,) = events.published
    assert isinstance(granted, CreditsGranted)
    assert granted.credits == 3
    assert credits.save_calls == 1
    assert credits.lock_calls == 1


async def test_a_dry_run_reports_the_balance_it_would_make_and_writes_nothing(
    clock: FixedClock,
) -> None:
    credits = FakeCreditAccountRepository()
    uid = a_user_id()
    seed_account(credits, uid, clock.now())
    events = RecordingEventPublisher()

    result = await _grant(credits, clock, events)(
        GrantCreditsCommand(user_id=uid, amount=GrantAmount(5), dry_run=True)
    )

    assert result == GrantCreditsResult(before=1, after=6)
    account = await credits.find(uid)
    assert account is not None
    assert account.balance == 1
    assert len(account.entries) == 1
    assert credits.save_calls == 0
    assert events.published == []


@pytest.mark.parametrize("dry_run", [False, True])
async def test_granting_to_a_user_with_no_account_is_refused(
    dry_run: bool, clock: FixedClock
) -> None:
    credits = FakeCreditAccountRepository()

    with pytest.raises(CreditAccountNotFound):
        await _grant(credits, clock, RecordingEventPublisher())(
            GrantCreditsCommand(user_id=a_user_id(), amount=GrantAmount(1), dry_run=dry_run)
        )

    assert credits.save_calls == 0


async def test_a_grant_touches_only_the_named_account(clock: FixedClock) -> None:
    credits = FakeCreditAccountRepository()
    uid, other = a_user_id(), a_user_id()
    seed_account(credits, uid, clock.now())
    seed_account(credits, other, clock.now())

    await _grant(credits, clock, RecordingEventPublisher())(
        GrantCreditsCommand(user_id=uid, amount=GrantAmount(2), dry_run=False)
    )

    untouched = await credits.find(other)
    assert untouched is not None
    assert untouched.balance == 1
