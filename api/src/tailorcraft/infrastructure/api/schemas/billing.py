"""Request and response schemas for the `billing` bounded context (slice 5.2, technical plan §4).

Bearer-only, every one: a credit belongs to a user. A pack is a closed set, so a request naming any
other is a 422 before a use case runs.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from tailorcraft.domain.billing.value_objects import CreditEntryKind, CreditNotice, Pack


class PackResponse(BaseModel):
    """One pack's terms, from `Pack` (a price is a code fact)."""

    id: Pack
    credits: int
    price_cents: int


class CreditEntryResponse(BaseModel):
    """One ledger line. `tailoring_run_id` is set for a charge or a refund only."""

    kind: CreditEntryKind
    credits: int
    tailoring_run_id: UUID | None
    at: datetime


class CreditsResponse(BaseModel):
    """`GET /api/me/credits`. `entries` newest first, at most 100."""

    balance: int
    out_of_credits: bool
    notice: CreditNotice | None
    payment_notice_opted_in: bool
    packs: list[PackResponse]
    entries: list[CreditEntryResponse]


class PackClickRequest(BaseModel):
    """`{"pack": "pack_3" | "pack_10"}`. Anything else, or a stray field, is a 422."""

    model_config = ConfigDict(extra="forbid")

    pack: Pack


class PaymentNoticeRequest(BaseModel):
    """`{"opted_in": true | false}`. Strict: `"yes"` or `1` is a 422, not a guess."""

    model_config = ConfigDict(extra="forbid", strict=True)

    opted_in: bool


class PaymentNoticeResponse(BaseModel):
    """The answer to a pack click and to the opt-in `PUT`."""

    payment_notice_opted_in: bool
