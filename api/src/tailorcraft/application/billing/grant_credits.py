"""The `GrantCredits` use case: the operator's `grant-credits` (plan §2)."""

from __future__ import annotations

from dataclasses import dataclass

from tailorcraft.domain.billing.ports import CreditAccountRepository
from tailorcraft.domain.billing.value_objects import GrantAmount
from tailorcraft.domain.identity.value_objects import UserId
from tailorcraft.domain.shared.clock import Clock
from tailorcraft.domain.shared.events import EventPublisherPort


@dataclass(frozen=True, slots=True)
class GrantCreditsCommand:
    user_id: UserId
    amount: GrantAmount
    dry_run: bool


@dataclass(frozen=True, slots=True)
class GrantCreditsResult:
    """The balance before and after. A dry run reports the `after` it would have made."""

    before: int
    after: int


class GrantCredits:
    """`credits.lock_for_charge` (the charge's serialization) -> `account.grant` -> `credits.save`
    and publish, **unless `dry_run`**, which saves and publishes nothing. `CreditAccountNotFound` for
    a user with no account."""

    def __init__(
        self, credits: CreditAccountRepository, clock: Clock, events: EventPublisherPort
    ) -> None:
        self._credits = credits
        self._clock = clock
        self._events = events

    async def __call__(self, cmd: GrantCreditsCommand) -> GrantCreditsResult:
        account = await self._credits.lock_for_charge(cmd.user_id)
        before = account.balance
        if cmd.dry_run:
            # The account is left untouched: no entry appended, so nothing could leak into a save.
            return GrantCreditsResult(before=before, after=before + cmd.amount.value)
        account.grant(
            cmd.amount, entry_id=self._credits.next_entry_identity(), at=self._clock.now()
        )
        await self._credits.save(account)
        await self._events.publish(*account.release_events())
        return GrantCreditsResult(before=before, after=account.balance)
