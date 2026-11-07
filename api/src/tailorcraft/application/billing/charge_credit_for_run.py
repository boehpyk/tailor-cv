"""The `ChargeCreditForRun` use case: one credit for one run, under the account lock (plan §0.3)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from tailorcraft.domain.billing.ports import CreditAccountRepository
from tailorcraft.domain.billing.value_objects import ChargedRunRef
from tailorcraft.domain.identity.value_objects import UserId
from tailorcraft.domain.shared.events import DomainEvent


@dataclass(frozen=True, slots=True)
class ChargeCreditCommand:
    user_id: UserId
    run: ChargedRunRef
    at: datetime


class ChargeCreditForRun:
    """`credits.lock_for_charge` -> `account.charge` -> `credits.save`; returns the account's events.

    Raises `CreditAccountNotFound`, `NoCreditsLeft` (CR-2) or `RunAlreadyCharged` (CR-3), with
    nothing saved. **The lock is held until the caller's transaction ends**: `RequestTailoringRun`
    runs its hard active-run re-check after this returns, still under it (F-2). Publishes nothing:
    the caller publishes these after the run's own events.
    """

    def __init__(self, credits: CreditAccountRepository) -> None:
        self._credits = credits

    async def __call__(self, cmd: ChargeCreditCommand) -> Sequence[DomainEvent]:
        raise NotImplementedError
