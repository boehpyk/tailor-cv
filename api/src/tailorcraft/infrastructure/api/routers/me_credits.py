"""A signed-in user's credits, pack clicks and payment-notice choice (slice 5.2, technical plan §4).

Built red-first (sdlc.md §2): **SKELETON** (T27, real paths and schemas, handlers raising
`NotImplementedError`), **RED** (T28, `qa`), **GREEN** (T29).

Bearer-only (ADR-0008 (f)): a credit belongs to a user, and nothing here reads the guest cookie. Not a
transfer route. Every response carries `Cache-Control: no-store`. No limiter (plan §4): a click costs a
row update, and the demand gate reads `pack_clickers`, which one account moves at most once.
"""

from __future__ import annotations

from typing import Annotated

import structlog
from fastapi import APIRouter, Body, HTTPException, Response, status
from sqlalchemy.exc import SQLAlchemyError

from tailorcraft.application.billing.choose_payment_notice import ChoosePaymentNoticeCommand
from tailorcraft.application.billing.get_credits import GetCreditsQuery
from tailorcraft.application.billing.record_pack_click import RecordPackClickCommand
from tailorcraft.domain.billing.credit_account import CreditAccount
from tailorcraft.domain.billing.errors import CreditAccountNotFound
from tailorcraft.domain.billing.value_objects import Pack
from tailorcraft.infrastructure.api.deps import (
    ChoosePaymentNoticeDep,
    ClockDep,
    GetCreditsDep,
    RecordPackClickDep,
    RequireUserDep,
    SessionDep,
)
from tailorcraft.infrastructure.api.errors import domain_error_to_http_exception
from tailorcraft.infrastructure.api.routers._me_responses import (
    NOT_SIGNED_IN,
    SERVICE_UNAVAILABLE,
    VALIDATION_ERROR,
)
from tailorcraft.infrastructure.api.schemas.billing import (
    CreditEntryResponse,
    CreditsResponse,
    PackClickRequest,
    PackResponse,
    PaymentNoticeRequest,
    PaymentNoticeResponse,
)

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/me", tags=["billing"])

# The ledger lines a read carries (plan §4, AC-4); the balance still counts every entry.
_ENTRIES_SHOWN = 100


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


def _unavailable(exc: SQLAlchemyError) -> HTTPException:
    """F-18 and AC-41 (the billing tables missing in the deploy window): 503 `service_unavailable`
    with `no-store`. The type alone is logged: a driver's message can quote a parameter."""
    log.error("billing.request_failed", error_type=type(exc).__name__)
    return HTTPException(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={
            "code": "service_unavailable",
            "message": "The service is temporarily unavailable. Please try again.",
        },
        headers={"Cache-Control": "no-store"},
    )


def _to_response(account: CreditAccount) -> CreditsResponse:
    # Newest first; the id (UUIDv7) breaks a tie inside one whole second.
    newest = sorted(account.entries, key=lambda e: (e.at, e.id.value), reverse=True)
    balance = account.balance
    return CreditsResponse(
        balance=balance,
        out_of_credits=balance <= 0,
        notice=account.notice,
        payment_notice_opted_in=account.payment_notice_opted_in_at is not None,
        packs=[
            PackResponse(id=pack, credits=pack.credits, price_cents=pack.price_cents)
            for pack in Pack
        ],
        entries=[
            CreditEntryResponse(
                kind=e.kind,
                credits=e.credits,
                tailoring_run_id=None if e.run is None else e.run.value,
                at=e.at,
            )
            for e in newest[:_ENTRIES_SHOWN]
        ],
    )


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
    _no_store(response)
    try:
        account = await get_credits(GetCreditsQuery(user_id=user_id))
    except CreditAccountNotFound as exc:
        refused = domain_error_to_http_exception(exc)
        refused.headers = {"Cache-Control": "no-store"}
        raise refused from None
    except SQLAlchemyError as exc:
        raise _unavailable(exc) from None
    return _to_response(account)


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
    """Count a click on a pack (the demand test); nothing is sold. Answers the opt-in state.
    Committed here, not in `get_session`'s teardown, which runs after the response is sent."""
    _no_store(response)
    try:
        opted_in = await record_click(
            RecordPackClickCommand(user_id=user_id, pack=body.pack, at=clock.now())
        )
        await db.commit()
    except SQLAlchemyError as exc:
        await db.rollback()
        raise _unavailable(exc) from None
    return PaymentNoticeResponse(payment_notice_opted_in=opted_in)


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
    """Set or clear the one-mail consent to hear when payment opens (plan §0.10). Committed here."""
    _no_store(response)
    try:
        await choose(
            ChoosePaymentNoticeCommand(user_id=user_id, opted_in=body.opted_in, at=clock.now())
        )
        await db.commit()
    except SQLAlchemyError as exc:
        await db.rollback()
        raise _unavailable(exc) from None
    return PaymentNoticeResponse(payment_notice_opted_in=body.opted_in)
