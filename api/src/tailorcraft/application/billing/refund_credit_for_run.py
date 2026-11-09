"""The `RefundCreditForRun` use case: a failed run's credit back, without the lock (plan §0.4)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import assert_never

from tailorcraft.domain.billing.ports import CreditAccountRepository
from tailorcraft.domain.billing.value_objects import ChargedRunRef
from tailorcraft.domain.identity.ownership import GuestOwner, Owner, UserOwner
from tailorcraft.domain.shared.events import DomainEvent


@dataclass(frozen=True, slots=True)
class RefundCreditCommand:
    """`owner` is **the run's**, so a guest-owned run is a `match` arm, not an `if`."""

    owner: Owner
    run: ChargedRunRef
    at: datetime


class RefundCreditForRun:
    """Returns the refund's events, or `()` when there is nothing to refund. Never raises for an
    ordinary no-op, each of which writes nothing:

    - a `GuestOwner` (billing rows are user-only);
    - a user with no account (`credits.find` is `None`);
    - `account.refund` returning `False`: the run was never charged (CR-5: F-19, F-28) or is already
      refunded (CR-4).

    Otherwise `credits.save(account)`. Takes **no** lock: a refund only adds, so it cannot break
    "never negative", and the refund's unique index backs "at most once".
    """

    def __init__(self, credits: CreditAccountRepository) -> None:
        self._credits = credits

    async def __call__(self, cmd: RefundCreditCommand) -> Sequence[DomainEvent]:
        match cmd.owner:
            case GuestOwner():
                return ()
            case UserOwner(user_id=user_id):
                pass
            case _:
                assert_never(cmd.owner)
        account = await self._credits.find(user_id)
        if account is None:
            return ()
        if not account.refund(cmd.run, entry_id=self._credits.next_entry_identity(), at=cmd.at):
            return ()
        await self._credits.save(account)
        return account.release_events()
