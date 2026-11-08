"""A signed-in user's before/after comparison of one tailoring run (slice 5.2, technical plan §0.8, §4).

Built red-first (sdlc.md §2): **SKELETON** (T27, handlers raising `NotImplementedError`), **RED**
(T28, `qa`), **GREEN** (T29).

Bearer-only, not a transfer route. Inline through 5.1's machinery: two sides, two requests, fired in
parallel by the client (one `AsyncSession` cannot run two operations at once). No fit-score limiter:
the comparison is bounded by credits, and the ledger's cap applies. Every response carries
`Cache-Control: no-store`.

Error order on the `POST`: ownership (404) -> status (409 `tailoring_run_not_succeeded`) -> CV present
(409 `base_cv_deleted`) -> stored hit (200) -> cap (503 `scoring_busy`) -> the call.

Its own router rather than routes in `me_tailoring_runs.py`: the prefix is shared, the context is
`scoring`'s. No path collides: `me_tailoring_runs` has no `/{id}/fit-scores…` route.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Response, status

from tailorcraft.domain.scoring.value_objects import ComparisonSide
from tailorcraft.infrastructure.api.deps import (
    GetTailoringRunFitScoresDep,
    RequireUserDep,
    ScoreTailoringRunDep,
    SessionDep,
)
from tailorcraft.infrastructure.api.routers._me_responses import (
    NOT_SIGNED_IN,
    RATE_LIMITED,
    SERVICE_UNAVAILABLE,
    TAILORING_RUN_NOT_FOUND,
    VALIDATION_ERROR,
)
from tailorcraft.infrastructure.api.schemas.intake import ErrorResponse
from tailorcraft.infrastructure.api.schemas.scoring import FitScoreResponse, RunFitScoresResponse

router = APIRouter(prefix="/api/me/tailoring-runs", tags=["scoring"])

_POST_RESPONSES: dict[int | str, dict[str, Any]] = {
    status.HTTP_200_OK: {
        "model": FitScoreResponse,
        "description": "The stored score for this side: nothing was spent.",
    },
    **TAILORING_RUN_NOT_FOUND,
    status.HTTP_409_CONFLICT: {
        "model": ErrorResponse,
        "description": "tailoring_run_not_succeeded | base_cv_deleted | base_cv_not_extracted.",
    },
    status.HTTP_422_UNPROCESSABLE_CONTENT: {
        "model": ErrorResponse,
        "description": "validation_error (an unknown side) | scoring_refused | inputs_too_large.",
    },
    **RATE_LIMITED,
    status.HTTP_503_SERVICE_UNAVAILABLE: {
        "model": ErrorResponse,
        "description": "scoring_busy | scoring_failed | service_unavailable.",
    },
}


@router.post(
    "/{tailoring_run_id}/fit-scores/{side}",
    response_model=FitScoreResponse,
    status_code=status.HTTP_201_CREATED,
    responses={**_POST_RESPONSES, **NOT_SIGNED_IN},
)
async def score_my_tailoring_run(
    response: Response,
    tailoring_run_id: UUID,
    side: ComparisonSide,
    user_id: RequireUserDep,
    score_run: ScoreTailoringRunDep,
    db: SessionDep,
) -> FitScoreResponse:
    """Score one side of the run: 201 for a new score, 200 for the stored one."""
    raise NotImplementedError


@router.get(
    "/{tailoring_run_id}/fit-scores",
    response_model=RunFitScoresResponse,
    responses={
        **TAILORING_RUN_NOT_FOUND,
        **VALIDATION_ERROR,
        **NOT_SIGNED_IN,
        **SERVICE_UNAVAILABLE,
    },
)
async def read_my_tailoring_run_fit_scores(
    response: Response,
    tailoring_run_id: UUID,
    user_id: RequireUserDep,
    get_scores: GetTailoringRunFitScoresDep,
) -> RunFitScoresResponse:
    """The stored sides and their verdict. Never reaches the scorer."""
    raise NotImplementedError
