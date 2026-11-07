"""The `OpenCreditAccount` use case: a user's account and its opening entry (plan §0.6)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from tailorcraft.domain.billing.credit_account import CreditAccount
from tailorcraft.domain.billing.ports import CreditAccountRepository
from tailorcraft.domain.billing.value_objects import OpeningGrant
from tailorcraft.domain.identity.value_objects import UserId
from tailorcraft.domain.shared.events import EventPublisherPort


@dataclass(frozen=True, slots=True)
class OpenCreditAccountCommand:
    user_id: UserId
    opening: OpeningGrant
    at: datetime


class OpenCreditAccount:
    """`CreditAccount.open` -> `credits.add` -> publish `CreditAccountOpened`.

    Composed by `ConfirmRegistration` after `users.add`, on the same session, so the user and their
    `welcome` entry commit together. Returns nothing: the caller already holds the user id.
    """

    def __init__(self, credits: CreditAccountRepository, events: EventPublisherPort) -> None:
        self._credits = credits
        self._events = events

    async def __call__(self, cmd: OpenCreditAccountCommand) -> None:
        account = CreditAccount.open(
            user_id=cmd.user_id,
            opening=cmd.opening,
            entry_id=self._credits.next_entry_identity(),
            at=cmd.at,
        )
        await self._credits.add(account)
        await self._events.publish(*account.release_events())
