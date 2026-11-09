"""Domain events for the `billing` bounded context.

An event's field set **is** a log field set, so there is no owner id here (5.1's rule). Pure data, so
complete at the skeleton step.
"""

from __future__ import annotations

from dataclasses import dataclass

from tailorcraft.domain.billing.value_objects import (
    ChargedRunRef,
    CreditEntryId,
    OpeningGrant,
    Pack,
)
from tailorcraft.domain.shared.events import DomainEvent


@dataclass(frozen=True, slots=True, kw_only=True)
class CreditAccountOpened(DomainEvent):
    opening: OpeningGrant


@dataclass(frozen=True, slots=True, kw_only=True)
class CreditCharged(DomainEvent):
    entry_id: CreditEntryId
    run: ChargedRunRef


@dataclass(frozen=True, slots=True, kw_only=True)
class CreditRefunded(DomainEvent):
    entry_id: CreditEntryId
    run: ChargedRunRef


@dataclass(frozen=True, slots=True, kw_only=True)
class CreditsGranted(DomainEvent):
    entry_id: CreditEntryId
    credits: int


@dataclass(frozen=True, slots=True, kw_only=True)
class PackClicked(DomainEvent):
    pack: Pack
    first_click: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class PaymentNoticeChosen(DomainEvent):
    """Recorded only when the choice changes."""

    opted_in: bool
