"""The `GetCredits` use case: the account behind `GET /api/me/credits`."""

from __future__ import annotations

from dataclasses import dataclass

from tailorcraft.domain.billing.credit_account import CreditAccount
from tailorcraft.domain.billing.errors import CreditAccountNotFound
from tailorcraft.domain.billing.ports import CreditAccountRepository
from tailorcraft.domain.identity.value_objects import UserId


@dataclass(frozen=True, slots=True)
class GetCreditsQuery:
    user_id: UserId


class GetCredits:
    """`credits.find`, unlocked. `CreditAccountNotFound` when missing, which eager opening and the
    backfill say cannot happen (AC-2): a loud error, never a silent zero balance."""

    def __init__(self, credits: CreditAccountRepository) -> None:
        self._credits = credits

    async def __call__(self, query: GetCreditsQuery) -> CreditAccount:
        account = await self._credits.find(query.user_id)
        if account is None:
            raise CreditAccountNotFound(query.user_id)
        return account
