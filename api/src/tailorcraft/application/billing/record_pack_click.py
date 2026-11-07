"""The `RecordPackClick` use case: the demand test's one signal of intent (plan §2, ADR-0043)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from tailorcraft.domain.billing.ports import CreditAccountRepository
from tailorcraft.domain.billing.value_objects import Pack
from tailorcraft.domain.identity.value_objects import UserId
from tailorcraft.domain.shared.counters import DailyCounterPort
from tailorcraft.domain.shared.events import EventPublisherPort


@dataclass(frozen=True, slots=True)
class RecordPackClickCommand:
    user_id: UserId
    pack: Pack
    at: datetime


class RecordPackClick:
    """`credits.lock_for_charge` -> `account.record_pack_click` -> `credits.save` -> `pack_clicks`
    +1, and `pack_clickers` +1 only on the account's first click ever, both on `at.date()` and in
    the caller's transaction -> publish. Returns whether the account is opted in to the payment
    notice (the response's `payment_notice_opted_in`)."""

    def __init__(
        self,
        credits: CreditAccountRepository,
        counters: DailyCounterPort,
        events: EventPublisherPort,
    ) -> None:
        self._credits = credits
        self._counters = counters
        self._events = events

    async def __call__(self, cmd: RecordPackClickCommand) -> bool:
        raise NotImplementedError
