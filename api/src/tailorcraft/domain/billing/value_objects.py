"""Value objects for the `billing` context.

Each `__post_init__` imports its error inside the method, because `errors` imports this module at the
top: the deferred import every other context uses for the same cycle.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID


@dataclass(frozen=True, slots=True)
class CreditEntryId:
    """A `CreditEntry`'s identity. Every `UUID` is a valid one, so there is nothing to validate."""

    value: UUID


@dataclass(frozen=True, slots=True)
class ChargedRunRef:
    """The tailoring run an entry charged or refunded, as this context's own type (`ScoredCvRef`'s
    reason): billing never imports `tailoring`. No foreign key behind it (ADR-0014)."""

    value: UUID


class CreditEntryKind(StrEnum):
    """What an entry records. Closed, and checked in the database."""

    WELCOME = "welcome"
    EXISTING_ACCOUNT = "existing_account"
    GRANT = "grant"
    CHARGE = "charge"
    REFUND = "refund"


class OpeningGrant(StrEnum):
    """The kinds an account may open with: a subset of `CreditEntryKind`, so `open` cannot be handed
    `charge`."""

    WELCOME = "welcome"
    EXISTING_ACCOUNT = "existing_account"


@dataclass(frozen=True, slots=True)
class GrantAmount:
    """How many credits the operator grants: a whole number from 1 to 100, else
    `InvalidGrantAmount`. A `bool` is refused: it is an `int` to Python."""

    value: int

    def __post_init__(self) -> None:
        """Skeleton (T5): validates nothing yet."""


class Pack(StrEnum):
    """A credit pack. A closed set: a price is a code fact, like ADR-0030's layouts."""

    PACK_3 = "pack_3"
    PACK_10 = "pack_10"

    @property
    def credits(self) -> int:
        """3 for `pack_3`, 10 for `pack_10`."""
        raise NotImplementedError

    @property
    def price_cents(self) -> int:
        """500 for `pack_3`, 1200 for `pack_10`."""
        raise NotImplementedError


class CreditNotice(StrEnum):
    """The one-time notice an account shows until its first charge."""

    CREDITS_INTRODUCED = "credits_introduced"
    FIRST_CREDIT = "first_credit"
