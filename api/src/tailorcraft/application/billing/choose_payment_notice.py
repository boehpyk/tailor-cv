"""The `ChoosePaymentNotice` use case: opt in to, or out of, the one payment mail (plan §0.10)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from tailorcraft.domain.billing.ports import CreditAccountRepository
from tailorcraft.domain.identity.value_objects import UserId
from tailorcraft.domain.shared.events import EventPublisherPort


@dataclass(frozen=True, slots=True)
class ChoosePaymentNoticeCommand:
    user_id: UserId
    opted_in: bool
    at: datetime


class ChoosePaymentNotice:
    """`credits.lock_for_charge` -> `account.choose_payment_notice` -> `credits.save` -> publish
    (`PaymentNoticeChosen` only on a change). Returns nothing: the state is `cmd.opted_in`."""

    def __init__(self, credits: CreditAccountRepository, events: EventPublisherPort) -> None:
        self._credits = credits
        self._events = events

    async def __call__(self, cmd: ChoosePaymentNoticeCommand) -> None:
        raise NotImplementedError
