"""Builders for slice 5.2's billing application tests: an account in the fake repository, a run
reference, a user id. Accounts are made through the aggregate's own `open` and `grant` (never by
setting a private attribute), then `seed`ed so arrangement leaves no pending entries or events."""

from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from tailorcraft.domain.billing.credit_account import CreditAccount
from tailorcraft.domain.billing.value_objects import (
    ChargedRunRef,
    CreditEntryId,
    GrantAmount,
    OpeningGrant,
)
from tailorcraft.domain.identity.value_objects import UserId
from tests.integration.fakes import FakeCreditAccountRepository


def a_user_id() -> UserId:
    return UserId(value=uuid4())


def a_run_ref() -> ChargedRunRef:
    return ChargedRunRef(value=uuid4())


def seed_account(
    credits: FakeCreditAccountRepository,
    user_id: UserId,
    at: datetime,
    *,
    opening: OpeningGrant = OpeningGrant.WELCOME,
    extra: int = 0,
) -> CreditAccount:
    """An account holding the opening credit plus `extra` granted ones (balance `1 + extra`)."""
    account = CreditAccount.open(
        user_id=user_id, opening=opening, entry_id=CreditEntryId(value=uuid4()), at=at
    )
    if extra:
        account.grant(GrantAmount(extra), entry_id=CreditEntryId(value=uuid4()), at=at)
    credits.seed(account)
    return account


async def charge_directly(
    credits: FakeCreditAccountRepository, user_id: UserId, run: ChargedRunRef, at: datetime
) -> None:
    """Arrange a charge through the aggregate, never through `ChargeCreditForRun`: a test of the
    refund must not fail in its arrangement because the use case it is not testing is a skeleton."""
    account = await credits.find(user_id)
    assert account is not None
    account.charge(run, entry_id=CreditEntryId(value=uuid4()), at=at)
    credits.seed(account)  # clears the pending entry and event: arrangement, not behaviour
