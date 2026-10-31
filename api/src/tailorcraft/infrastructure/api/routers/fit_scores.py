"""A guest's fit scores (slice 5.1, technical plan §4). One credential: the guest cookie, and **no
session is minted**, `routers/tailoring.py`'s reason. The bodies are in `_fit_score_handlers.py`.

SKELETON (T28). These signatures carry only the credential and the input: a route that depends on a
scoring port makes that port "bound", which `test_port_bindings.py` refuses while the port is still
in `_BOUND_ELSEWHERE`. T30 adds the dependencies and calls the shared bodies.
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Body, Query, status

from tailorcraft.infrastructure.api.deps import RequireGuestSessionDep
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
    body: Annotated[CreateFitScoreRequest, Body()],
    session: RequireGuestSessionDep,
) -> FitScoreResponse:
    """Score a new pair inside this request, or answer with the stored score."""
    raise NotImplementedError


@router.get(
    "",
    response_model=FitScoreForPairResponse,
    responses={**GET_RESPONSES, **_GUEST_SESSION_EXPIRED},
)
async def read_fit_score(
    base_cv_id: Annotated[UUID, Query()],
    job_posting_id: Annotated[UUID, Query()],
    session: RequireGuestSessionDep,
) -> FitScoreForPairResponse:
    """The stored score of a pair, or `null`. Never reaches the scorer (AC-48)."""
    raise NotImplementedError
