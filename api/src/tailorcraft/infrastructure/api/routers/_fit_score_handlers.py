"""The credential-agnostic bodies of the fit-score routes (slice 5.1, technical plan §4), as
`_tailoring_handlers.py` is for tailoring.

**Nothing here reads a credential,** so the AST scan's transfer set stays exactly
`{POST /api/me/guest-work/claim}`. There is no `translate` argument: no scoring path raises a cap that
speaks differently to an account, so both credentials use `domain_error_to_http_exception`.

**No commit here, on purpose.** `RequestFitScore`'s call order is the transaction boundary and
`CommittingSpendLedger` makes it one (plan §0.2): nothing is open across `assess`, and a row is durable
before its response is built. Every other path has written nothing, so `get_session` rolls back or
commits an empty transaction.

**`no-store` is set twice for one reason.** Headers on the injected `Response` are lost when a handler
raises, so every refusal carries its own (`_refusal`, `_unavailable`, the two limiter answers).

Logs ids, counts and exception type names. Never text, a fingerprint, a score beside an identity, or
a client IP (Constitution §8).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

import structlog
from fastapi import HTTPException, Request, Response, status
from sqlalchemy.exc import SQLAlchemyError

from tailorcraft.application.scoring.find_fit_score_for_pair import FindFitScoreForPair
from tailorcraft.application.scoring.request_fit_score import (
    RequestFitScore,
    RequestFitScoreCommand,
)
from tailorcraft.application.scoring.resolve_scoring_pair import ResolveScoringPair
from tailorcraft.domain.identity.ownership import Owner
from tailorcraft.domain.intake.value_objects import BaseCvId
from tailorcraft.domain.posting.value_objects import JobPostingId
from tailorcraft.domain.scoring.fit_score import FitScore
from tailorcraft.domain.shared.errors import DomainError
from tailorcraft.infrastructure.api.errors import domain_error_to_http_exception
from tailorcraft.infrastructure.api.routers._me_responses import (
    RATE_LIMITED,
    REQUEST_TOO_LARGE,
    VALIDATION_ERROR,
)
from tailorcraft.infrastructure.api.routers._tailoring_handlers import no_store
from tailorcraft.infrastructure.api.schemas.intake import ErrorResponse
from tailorcraft.infrastructure.api.schemas.scoring import (
    CreateFitScoreRequest,
    FitScoreForPairResponse,
    FitScoreResponse,
)
from tailorcraft.infrastructure.rate_limit import (
    RateLimiterUnavailable,
    RedisFixedWindowRateLimiter,
    client_ip,
)
from tailorcraft.infrastructure.settings import Settings

log = structlog.get_logger(__name__)

_NOT_FOUND: dict[int | str, dict[str, Any]] = {
    status.HTTP_404_NOT_FOUND: {
        "model": ErrorResponse,
        "description": (
            "base_cv_not_found | job_posting_not_found: byte-identical for an id that does not "
            "exist and one that is not the caller's."
        ),
    },
}

# `dict[int | str, dict[str, Any]]` is FastAPI's own type for `responses=`. Each router adds its 401.
POST_RESPONSES: dict[int | str, dict[str, Any]] = {
    status.HTTP_200_OK: {
        "model": FitScoreResponse,
        "description": "The stored score: a hit, or the loser of a two-tab race. Nothing was spent.",
    },
    **_NOT_FOUND,
    status.HTTP_409_CONFLICT: {
        "model": ErrorResponse,
        "description": (
            "base_cv_not_extracted | too_many_fit_scores (+ `limit`, `retry_not_before`, null for "
            "a guest)."
        ),
    },
    **REQUEST_TOO_LARGE,
    status.HTTP_422_UNPROCESSABLE_CONTENT: {
        "model": ErrorResponse,
        "description": "validation_error | scoring_refused | inputs_too_large.",
    },
    **RATE_LIMITED,
    status.HTTP_503_SERVICE_UNAVAILABLE: {
        "model": ErrorResponse,
        "description": (
            "scoring_busy | scoring_failed | rate_limit_unavailable (the limiter fails closed) | "
            "service_unavailable. No 503 holds the client (ADR-0031 §6)."
        ),
    },
}

GET_RESPONSES: dict[int | str, dict[str, Any]] = {
    **_NOT_FOUND,
    **VALIDATION_ERROR,
    status.HTTP_503_SERVICE_UNAVAILABLE: {
        "model": ErrorResponse,
        "description": "service_unavailable: Postgres is down. Redis is not touched.",
    },
}


def to_response(score: FitScore, expires_at: datetime | None) -> FitScoreResponse:
    """What a person sees. No fingerprint, token count, cost or model: they stop here."""
    assessment = score.assessment
    return FitScoreResponse(
        id=score.id.value,
        score_percent=assessment.score.value,
        gaps=[gap.value for gap in assessment.gaps],
        scored_at=score.scored_at,
        expires_at=expires_at,
    )


def _refusal(exc: DomainError) -> HTTPException:
    refusal = domain_error_to_http_exception(exc)
    refusal.headers = {**(refusal.headers or {}), "Cache-Control": "no-store"}
    return refusal


def _unavailable(exc: Exception) -> HTTPException:
    """S-13 to S-15: the generic answer, `main.py`'s `SQLAlchemyError` sentence plus `no-store`. Also
    `store`'s last-resort `RuntimeError`, which no app-level handler catches. The type alone is logged:
    the driver's message can quote a parameter."""
    log.error("scoring.request_failed", error_type=type(exc).__name__)
    return HTTPException(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={
            "code": "service_unavailable",
            "message": "The service is temporarily unavailable. Please try again.",
        },
        headers={"Cache-Control": "no-store"},
    )


async def _spend_the_ip_limit(
    request: Request, settings: Settings, rate_limiter: RedisFixedWindowRateLimiter
) -> None:
    """Fails closed (S-12). The address is computed in the call and kept in no local, so no frame or
    log line can pair it with a score. `from None` for the same reason."""
    try:
        decision = await rate_limiter.check(
            "ip",
            client_ip(request, settings.trusted_proxy_hops),
            settings.fit_score_rate_limit_per_ip_per_hour,
        )
    except RateLimiterUnavailable:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "rate_limit_unavailable",
                "message": "Scoring is temporarily unavailable. Please try again shortly.",
            },
            headers={"Cache-Control": "no-store"},
        ) from None
    if not decision.allowed:
        log.info("rate_limit.exceeded", scope="ip", namespace=rate_limiter.namespace)
        retry_after = decision.retry_after_seconds
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "code": "rate_limited",
                "message": f"Too many checks. Try again in {retry_after} seconds.",
            },
            headers={"Retry-After": str(retry_after), "Cache-Control": "no-store"},
        )


async def request_fit_score(
    *,
    request: Request,
    response: Response,
    body: CreateFitScoreRequest,
    requester: Owner,
    expires_at: datetime | None,
    settings: Settings,
    rate_limiter: RedisFixedWindowRateLimiter,
    resolve_pair: ResolveScoringPair,
    find_score: FindFitScoreForPair,
    request_score: RequestFitScore,
) -> FitScoreResponse:
    """`POST`'s body, in plan §0.5's order: resolve the pair, a stored hit answers 200, the fail-closed
    IP limiter, then `RequestFitScore`, whose ledger commits around the call. 201, or 200 for the
    loser of a race. `no-store` on every answer.

    **`__cause__` is read only on `RequestFitScore`'s errors**, where it means a failed settle under
    another failure (S-16). `GetBaseCv` and `GetJobPosting` chain their not-founds on purpose, so
    reading it on the outer `except` would report a settle that never ran. Type name only: `exc_info`
    would carry the message. `from None` throughout: these frames hold the CV.
    """
    no_store(response)
    try:
        pair = await resolve_pair(
            requester, BaseCvId(body.base_cv_id), JobPostingId(body.job_posting_id)
        )
        stored = await find_score.for_pair(requester, pair)
        if stored is not None:
            response.status_code = status.HTTP_200_OK
            return to_response(stored, expires_at)

        await _spend_the_ip_limit(request, settings, rate_limiter)

        try:
            outcome = await request_score(RequestFitScoreCommand(owner=requester, pair=pair))
        except DomainError as exc:
            if exc.__cause__ is not None:
                log.error("scoring.settle_failed", error_type=type(exc.__cause__).__name__)
            raise
    except DomainError as exc:
        raise _refusal(exc) from None
    except (SQLAlchemyError, RuntimeError) as exc:
        raise _unavailable(exc) from None

    if not outcome.created:
        response.status_code = status.HTTP_200_OK
    return to_response(outcome.stored, expires_at)


async def read_fit_score(
    *,
    response: Response,
    base_cv_id: UUID,
    job_posting_id: UUID,
    requester: Owner,
    expires_at: datetime | None,
    find_score: FindFitScoreForPair,
) -> FitScoreForPairResponse:
    """`GET`'s body: `FindFitScoreForPair`, with `null` for "not scored yet" and for a CV with no text
    (S-24). No limiter and no Redis (S-11). `no-store`."""
    no_store(response)
    try:
        stored = await find_score(requester, BaseCvId(base_cv_id), JobPostingId(job_posting_id))
    except DomainError as exc:
        raise _refusal(exc) from None
    except SQLAlchemyError as exc:
        raise _unavailable(exc) from None
    return FitScoreForPairResponse(
        fit_score=None if stored is None else to_response(stored, expires_at)
    )
