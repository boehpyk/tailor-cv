"""The credential-agnostic bodies of the fit-score routes (slice 5.1, technical plan §4), as
`_tailoring_handlers.py` is for tailoring.

SKELETON (T28): real signatures, `NotImplementedError` bodies. **Nothing here reads a credential,**
so the AST scan's transfer set stays exactly `{POST /api/me/guest-work/claim}`. There is no `translate`
argument: no scoring path raises a cap that speaks differently to an account, so both credentials use
`domain_error_to_http_exception`.

Logs ids, counts and exception type names. Never text, a fingerprint, a score beside an identity, or
a client IP (Constitution §8).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import Request, Response, status

from tailorcraft.application.scoring.find_fit_score_for_pair import FindFitScoreForPair
from tailorcraft.application.scoring.request_fit_score import RequestFitScore
from tailorcraft.application.scoring.resolve_scoring_pair import ResolveScoringPair
from tailorcraft.domain.identity.ownership import Owner
from tailorcraft.infrastructure.api.routers._me_responses import (
    RATE_LIMITED,
    REQUEST_TOO_LARGE,
    VALIDATION_ERROR,
)
from tailorcraft.infrastructure.api.schemas.intake import ErrorResponse
from tailorcraft.infrastructure.api.schemas.scoring import (
    CreateFitScoreRequest,
    FitScoreForPairResponse,
    FitScoreResponse,
)
from tailorcraft.infrastructure.rate_limit import RedisFixedWindowRateLimiter
from tailorcraft.infrastructure.settings import Settings

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

    For T30: a `DomainError` with a `__cause__` is a failed settle under another failure (S-16), on
    both the `FitScoringFailed` and the refused-store path, so log the cause's type, then translate.
    `store`'s last-resort `RuntimeError` is not a `DomainError` and must reach the generic 503.
    """
    raise NotImplementedError


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
    raise NotImplementedError
