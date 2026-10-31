"""A signed-in user's fit scores (slice 5.1, technical plan §4). One credential: the bearer, and a
`__Host-tc_guest` cookie is ignored. Not a transfer route. The bodies are in `_fit_score_handlers.py`.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Body, Query, Request, Response, status

from tailorcraft.domain.identity.ownership import UserOwner
from tailorcraft.infrastructure.api.deps import (
    FindFitScoreForPairDep,
    FitScoreRateLimiterDep,
    RequestFitScoreDep,
    RequireUserDep,
    ResolveScoringPairDep,
    SettingsDep,
)
from tailorcraft.infrastructure.api.routers import _fit_score_handlers as handlers
from tailorcraft.infrastructure.api.routers._fit_score_handlers import (
    GET_RESPONSES,
    POST_RESPONSES,
)
from tailorcraft.infrastructure.api.routers._me_responses import NOT_SIGNED_IN
from tailorcraft.infrastructure.api.schemas.scoring import (
    CreateFitScoreRequest,
    FitScoreForPairResponse,
    FitScoreResponse,
)

router = APIRouter(prefix="/api/me/fit-scores", tags=["scoring"])


@router.post(
    "",
    response_model=FitScoreResponse,
    status_code=status.HTTP_201_CREATED,
    responses={**POST_RESPONSES, **NOT_SIGNED_IN},
)
async def request_my_fit_score(
    request: Request,
    response: Response,
    body: Annotated[CreateFitScoreRequest, Body()],
    user_id: RequireUserDep,
    settings: SettingsDep,
    rate_limiter: FitScoreRateLimiterDep,
    resolve_pair: ResolveScoringPairDep,
    find_score: FindFitScoreForPairDep,
    request_score: RequestFitScoreDep,
) -> FitScoreResponse:
    """Score a new pair inside this request, or answer with the stored score. `expires_at: null`."""
    return await handlers.request_fit_score(
        request=request,
        response=response,
        body=body,
        requester=UserOwner(user_id),
        expires_at=None,
        settings=settings,
        rate_limiter=rate_limiter,
        resolve_pair=resolve_pair,
        find_score=find_score,
        request_score=request_score,
    )


@router.get(
    "",
    response_model=FitScoreForPairResponse,
    responses={**GET_RESPONSES, **NOT_SIGNED_IN},
)
async def read_my_fit_score(
    response: Response,
    base_cv_id: Annotated[UUID, Query()],
    job_posting_id: Annotated[UUID, Query()],
    user_id: RequireUserDep,
    find_score: FindFitScoreForPairDep,
) -> FitScoreForPairResponse:
    """The stored score of a pair, or `null`. Never reaches the scorer (AC-48)."""
    return await handlers.read_fit_score(
        response=response,
        base_cv_id=base_cv_id,
        job_posting_id=job_posting_id,
        requester=UserOwner(user_id),
        expires_at=None,
        find_score=find_score,
    )
