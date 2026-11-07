"""Placeholders for the two slice-5.2 ports whose adapters do not exist yet (plan §3).

**Delete this module when T22 writes the adapters and T26 binds them.** Until then the composition
roots pass these so the new constructor arguments type-check (T12); nothing calls them, because
every use case that would is still a `NotImplementedError` skeleton or holds them unused. A call is
loud on purpose: a placeholder that answered quietly would hide a missing binding.
"""

from __future__ import annotations

from datetime import date
from typing import NoReturn

from tailorcraft.domain.billing.credit_account import CreditAccount
from tailorcraft.domain.billing.value_objects import CreditEntryId
from tailorcraft.domain.identity.value_objects import UserId
from tailorcraft.domain.shared.counters import DemandCounter

_MESSAGE = "unwired until slice 5.2's T26"


class UnwiredCreditAccountRepository:
    def next_entry_identity(self) -> CreditEntryId:
        raise NotImplementedError(_MESSAGE)

    async def add(self, account: CreditAccount) -> NoReturn:
        raise NotImplementedError(_MESSAGE)

    async def find(self, user_id: UserId) -> CreditAccount | None:
        raise NotImplementedError(_MESSAGE)

    async def lock_for_charge(self, user_id: UserId) -> CreditAccount:
        raise NotImplementedError(_MESSAGE)

    async def save(self, account: CreditAccount) -> NoReturn:
        raise NotImplementedError(_MESSAGE)


class UnwiredDailyCounter:
    async def increment(self, counter: DemandCounter, day: date) -> NoReturn:
        raise NotImplementedError(_MESSAGE)
