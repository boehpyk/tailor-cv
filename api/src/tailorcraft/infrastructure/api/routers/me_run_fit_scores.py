"""A signed-in user's before/after comparison of one tailoring run (slice 5.2, technical plan §0.8, §4).

Built red-first (sdlc.md §2): **SKELETON** (T27, handlers raising `NotImplementedError`), **RED**
(T28, `qa`), **GREEN** (T29).

Bearer-only, not a transfer route. Inline through 5.1's machinery: two sides, two requests, fired in
parallel by the client (one `AsyncSession` cannot run two operations at once). The ledger's cap
applies, and a fail-closed limiter per (user, run, side) guards it from one account (amended AC-19,
S-11). Every response carries `Cache-Control: no-store`.

Error order on the `POST`: the limiter (429 `rate_limited`, 503 `rate_limit_unavailable`; first, and
keyed on the caller, so a 429 reveals nothing about the run) -> ownership (404) -> status (409 `tailoring_run_not_succeeded`) -> CV present
(409 `base_cv_deleted`) -> stored hit (200) -> cap (503 `scoring_busy`) -> the call.

Its own router rather than routes in `me_tailoring_runs.py`: the prefix is shared, the context is
`scoring`'s. No path collides: `me_tailoring_runs` has no `/{id}/fit-scores…` route.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import structlog
from fastapi import APIRouter, Response, status
from sqlalchemy.exc import SQLAlchemyError

from tailorcraft.application.scoring.get_tailoring_run_fit_scores import (
    GetTailoringRunFitScoresQuery,
)
from tailorcraft.application.scoring.score_tailoring_run import ScoreTailoringRunCommand
from tailorcraft.domain.identity.ownership import UserOwner
from tailorcraft.domain.scoring.errors import FitScoreSpendCapReached, FitScoringFailed
from tailorcraft.domain.scoring.value_objects import ComparisonSide
from tailorcraft.domain.shared.errors import DomainError
from tailorcraft.domain.tailoring.value_objects import TailoringRunId
from tailorcraft.infrastructure.api.deps import (
    FitScoreComparisonRateLimiterDep,
    GetTailoringRunFitScoresDep,
    RequireUserDep,
    ScoreTailoringRunDep,
    SessionDep,
    SettingsDep,
)
from tailorcraft.infrastructure.api.routers._fit_score_handlers import (
    commit_busy_count,
    refusal,
    spend_limit,
    to_response,
    unavailable,
)
from tailorcraft.infrastructure.api.routers._me_responses import (
    NOT_SIGNED_IN,
    RATE_LIMITED,
    SERVICE_UNAVAILABLE,
    TAILORING_RUN_NOT_FOUND,
    VALIDATION_ERROR,
)
from tailorcraft.infrastructure.api.routers._tailoring_handlers import no_store
from tailorcraft.infrastructure.api.schemas.intake import ErrorResponse
from tailorcraft.infrastructure.api.schemas.scoring import (
    FitScoreResponse,
    RunFitScoresResponse,
    ScoreComparisonResponse,
)
from tailorcraft.infrastructure.llm.gemini import qualified_type

log = structlog.get_logger(__name__)

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
        "description": (
            "scoring_busy | scoring_failed | rate_limit_unavailable (the limiter fails closed) | "
            "service_unavailable."
        ),
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
    settings: SettingsDep,
    rate_limiter: FitScoreComparisonRateLimiterDep,
) -> FitScoreResponse:
    """Score one side of the run: 201 for a new score, 200 for the stored one. A full cap's
    `fit_scores_busy` count is committed before the 503 (plan §0.9). `from None` throughout: these
    frames hold the CV and the draft."""
    no_store(response)
    await spend_limit(
        rate_limiter,
        "user",
        f"{user_id}:{tailoring_run_id}:{side.value}",
        settings.fit_score_comparison_rate_limit_per_run_per_hour,
    )
    try:
        outcome = await score_run(
            ScoreTailoringRunCommand(
                owner=UserOwner(user_id),
                tailoring_run_id=TailoringRunId(tailoring_run_id),
                side=side,
            )
        )
    except DomainError as exc:
        if isinstance(exc, FitScoreSpendCapReached):
            await commit_busy_count(db, exc)
        if isinstance(exc, FitScoringFailed) and exc.__cause__ is not None:
            # S-16: a settle failed under the scorer's failure; the type only. Not on every error:
            # `GetTailoringRun` chains its not-found on purpose.
            log.error("scoring.settle_failed", error_type=qualified_type(exc.__cause__))
        raise refusal(exc) from None
    except (SQLAlchemyError, RuntimeError) as exc:
        raise unavailable(exc) from None

    if not outcome.created:
        response.status_code = status.HTTP_200_OK
    return to_response(outcome.stored, None)


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
    no_store(response)
    try:
        scores = await get_scores(
            GetTailoringRunFitScoresQuery(
                owner=UserOwner(user_id), tailoring_run_id=TailoringRunId(tailoring_run_id)
            )
        )
    except DomainError as exc:
        raise refusal(exc) from None
    except (SQLAlchemyError, RuntimeError) as exc:
        raise unavailable(exc) from None

    comparison = scores.comparison
    return RunFitScoresResponse(
        before=None if scores.before is None else to_response(scores.before, None),
        after=None if scores.after is None else to_response(scores.after, None),
        comparable=scores.comparable,
        comparison=(
            None
            if comparison is None
            else ScoreComparisonResponse(delta=comparison.delta, verdict=comparison.verdict)
        ),
        unavailable=scores.unavailable,
    )
