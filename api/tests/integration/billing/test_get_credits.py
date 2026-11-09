"""Application tests for `GetCredits` (slice 5.2, T13 RED, AC-4's read)."""

from __future__ import annotations

import pytest

from tailorcraft.application.billing.get_credits import GetCredits, GetCreditsQuery
from tailorcraft.domain.billing.errors import CreditAccountNotFound
from tailorcraft.infrastructure.clock import FixedClock
from tests.integration.billing.support import a_user_id, seed_account
from tests.integration.fakes import FakeCreditAccountRepository


async def test_it_returns_the_users_own_account_with_its_balance(clock: FixedClock) -> None:
    credits = FakeCreditAccountRepository()
    uid, other = a_user_id(), a_user_id()
    seed_account(credits, uid, clock.now(), extra=4)
    seed_account(credits, other, clock.now())

    account = await GetCredits(credits)(GetCreditsQuery(user_id=uid))

    assert account.user_id == uid
    assert account.balance == 5


async def test_it_reads_without_the_lock_and_writes_nothing(clock: FixedClock) -> None:
    credits = FakeCreditAccountRepository()
    uid = a_user_id()
    seed_account(credits, uid, clock.now())

    await GetCredits(credits)(GetCreditsQuery(user_id=uid))

    assert (credits.lock_calls, credits.save_calls) == (0, 0)


async def test_a_user_with_no_account_is_an_error_never_a_zero_balance() -> None:
    with pytest.raises(CreditAccountNotFound):
        await GetCredits(FakeCreditAccountRepository())(GetCreditsQuery(user_id=a_user_id()))
