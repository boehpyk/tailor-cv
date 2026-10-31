"""A signed-in user's fit scores (slice 5.1, technical plan §4). One credential: the bearer, and a
`__Host-tc_guest` cookie is ignored. Not a transfer route. The bodies are in `_fit_score_handlers.py`.

SKELETON (T28), with `routers/fit_scores.py`'s note on why the signatures are short until T30.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Body, Query, status

from tailorcraft.infrastructure.api.deps import RequireUserDep
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
    body: Annotated[CreateFitScoreRequest, Body()],
    user_id: RequireUserDep,
) -> FitScoreResponse:
    """Score a new pair inside this request, or answer with the stored score. `expires_at: null`."""
    raise NotImplementedError


@router.get(
    "",
    response_model=FitScoreForPairResponse,
    responses={**GET_RESPONSES, **NOT_SIGNED_IN},
)
async def read_my_fit_score(
    base_cv_id: Annotated[UUID, Query()],
    job_posting_id: Annotated[UUID, Query()],
    user_id: RequireUserDep,
) -> FitScoreForPairResponse:
    """The stored score of a pair, or `null`. Never reaches the scorer (AC-48)."""
    raise NotImplementedError
