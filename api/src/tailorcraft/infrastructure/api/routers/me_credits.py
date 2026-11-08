"""A signed-in user's credits, pack clicks and payment-notice choice (slice 5.2, technical plan §4).

Built red-first (sdlc.md §2): **SKELETON** (T27, real paths and schemas, handlers raising
`NotImplementedError`), **RED** (T28, `qa`), **GREEN** (T29).

Bearer-only (ADR-0008 (f)): a credit belongs to a user, and nothing here reads the guest cookie. Not a
transfer route. Every response carries `Cache-Control: no-store`. No limiter (plan §4): a click costs a
row update, and the demand gate reads `pack_clickers`, which one account moves at most once.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body, Response

from tailorcraft.infrastructure.api.deps import (
    ChoosePaymentNoticeDep,
    ClockDep,
    GetCreditsDep,
    RecordPackClickDep,
    RequireUserDep,
    SessionDep,
)
from tailorcraft.infrastructure.api.routers._me_responses import (
    NOT_SIGNED_IN,
    SERVICE_UNAVAILABLE,
    VALIDATION_ERROR,
)
from tailorcraft.infrastructure.api.schemas.billing import (
    CreditsResponse,
    PackClickRequest,
    PaymentNoticeRequest,
    PaymentNoticeResponse,
)

router = APIRouter(prefix="/api/me", tags=["billing"])


@router.get(
    "/credits",
    response_model=CreditsResponse,
    responses={**NOT_SIGNED_IN, **SERVICE_UNAVAILABLE},
)
async def read_my_credits(
    response: Response,
    user_id: RequireUserDep,
    get_credits: GetCreditsDep,
) -> CreditsResponse:
    """The balance, the notice, the packs and the newest 100 ledger entries."""
    raise NotImplementedError


@router.post(
    "/pack-clicks",
    response_model=PaymentNoticeResponse,
    responses={**NOT_SIGNED_IN, **VALIDATION_ERROR, **SERVICE_UNAVAILABLE},
)
async def record_my_pack_click(
    response: Response,
    body: Annotated[PackClickRequest, Body()],
    user_id: RequireUserDep,
    clock: ClockDep,
    record_click: RecordPackClickDep,
    db: SessionDep,
) -> PaymentNoticeResponse:
    """Count a click on a pack (the demand test); nothing is sold. Answers the opt-in state."""
    raise NotImplementedError


@router.put(
    "/payment-notice",
    response_model=PaymentNoticeResponse,
    responses={**NOT_SIGNED_IN, **VALIDATION_ERROR, **SERVICE_UNAVAILABLE},
)
async def choose_my_payment_notice(
    response: Response,
    body: Annotated[PaymentNoticeRequest, Body()],
    user_id: RequireUserDep,
    clock: ClockDep,
    choose: ChoosePaymentNoticeDep,
    db: SessionDep,
) -> PaymentNoticeResponse:
    """Set or clear the one-mail consent to hear when payment opens (plan §0.10)."""
    raise NotImplementedError
