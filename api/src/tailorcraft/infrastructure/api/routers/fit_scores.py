"""A guest's fit scores (slice 5.1, technical plan §4). One credential: the guest cookie, and **no
session is minted**, `routers/tailoring.py`'s reason. The bodies are in `_fit_score_handlers.py`.
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Body, Query, Request, Response, status

from tailorcraft.domain.identity.ownership import GuestOwner
from tailorcraft.infrastructure.api.deps import (
    FindFitScoreForPairDep,
    FitScoreRateLimiterDep,
    RequestFitScoreDep,
    RequireGuestSessionDep,
    ResolveScoringPairDep,
    SessionDep,
    SettingsDep,
)
from tailorcraft.infrastructure.api.routers import _fit_score_handlers as handlers
from tailorcraft.infrastructure.api.routers._fit_score_handlers import (
    GET_RESPONSES,
    POST_RESPONSES,
)
from tailorcraft.infrastructure.api.schemas.intake import ErrorResponse
from tailorcraft.infrastructure.api.schemas.scoring import (
    CreateFitScoreRequest,
    FitScoreForPairResponse,
    FitScoreResponse,
)

router = APIRouter(prefix="/api/fit-scores", tags=["scoring"])

_GUEST_SESSION_EXPIRED: dict[int | str, dict[str, Any]] = {
    status.HTTP_401_UNAUTHORIZED: {
        "model": ErrorResponse,
        "description": "guest_session_expired: no session is minted here.",
    },
}


@router.post(
    "",
    response_model=FitScoreResponse,
    status_code=status.HTTP_201_CREATED,
    responses={**POST_RESPONSES, **_GUEST_SESSION_EXPIRED},
)
async def request_fit_score(
    request: Request,
    response: Response,
    body: Annotated[CreateFitScoreRequest, Body()],
    session: RequireGuestSessionDep,
    settings: SettingsDep,
    rate_limiter: FitScoreRateLimiterDep,
    resolve_pair: ResolveScoringPairDep,
    find_score: FindFitScoreForPairDep,
    request_score: RequestFitScoreDep,
    db: SessionDep,
) -> FitScoreResponse:
    """Score a new pair inside this request, or answer with the stored score."""
    return await handlers.request_fit_score(
        request=request,
        response=response,
        body=body,
        requester=GuestOwner(session.id),
        expires_at=session.expires_at,
        settings=settings,
        rate_limiter=rate_limiter,
        resolve_pair=resolve_pair,
        find_score=find_score,
        request_score=request_score,
        db=db,
    )


@router.get(
    "",
    response_model=FitScoreForPairResponse,
    responses={**GET_RESPONSES, **_GUEST_SESSION_EXPIRED},
)
async def read_fit_score(
    response: Response,
    base_cv_id: Annotated[UUID, Query()],
    job_posting_id: Annotated[UUID, Query()],
    session: RequireGuestSessionDep,
    find_score: FindFitScoreForPairDep,
) -> FitScoreForPairResponse:
    """The stored score of a pair, or `null`. Never reaches the scorer (AC-48)."""
    return await handlers.read_fit_score(
        response=response,
        base_cv_id=base_cv_id,
        job_posting_id=job_posting_id,
        requester=GuestOwner(session.id),
        expires_at=session.expires_at,
        find_score=find_score,
    )
