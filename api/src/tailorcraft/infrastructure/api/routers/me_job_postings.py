"""A signed-in user's job postings: capture one, list the recent ones (slice 2.3, technical plan §4).

Built red-first (sdlc.md §2): **SKELETON** (T20, this file — real paths, real schemas, every
documented status in each `responses=` map, handlers raising `NotImplementedError`), **RED** (T21,
`qa`), **GREEN** (T23 — thin calls into `_posting_handlers.py`, T19's shared bodies).

**One credential: the bearer** (`require_user`). A `tc_guest` cookie riding along changes nothing,
and nothing in this module reads it (ADR-0008 (f)): the account twin of `POST /api/job-postings`, not
a transfer route. A guest's posting id is not reachable here, and there is no `GET /{id}` — a user's
posting is read through its history entry.

Every response carries `Cache-Control: no-store`, and every `expires_at` is `null`: an account
posting is kept until the history entry that uses it is deleted (OQ-8).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body, Query, Request, Response, status

from tailorcraft.infrastructure.api.deps import RequireUserDep
from tailorcraft.infrastructure.api.routers._me_responses import (
    NOT_SIGNED_IN,
    RATE_LIMITED,
    REQUEST_TOO_LARGE,
    SERVICE_UNAVAILABLE,
    VALIDATION_ERROR,
)
from tailorcraft.infrastructure.api.schemas.intake import ErrorResponse
from tailorcraft.infrastructure.api.schemas.posting import (
    CreateJobPostingRequest,
    JobPostingListResponse,
    JobPostingResponse,
)

router = APIRouter(prefix="/api/me/job-postings", tags=["posting"])

# The bound on `GET ?limit=` — the "recent postings" list is a picker, not an archive (plan §4).
RECENT_POSTINGS_MAX = 20


@router.post(
    "",
    response_model=JobPostingResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        status.HTTP_201_CREATED: {
            "description": "The posting, owned by the bearer's account, `expires_at: null`.",
        },
        status.HTTP_409_CONFLICT: {
            "model": ErrorResponse,
            "description": (
                "too_many_job_postings — the account already owns `max_job_postings_per_user`."
            ),
        },
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ErrorResponse,
            "description": (
                "validation_error | invalid_source_url | posting_text_too_short | "
                "posting_text_too_long | fetch_blocked — 1.2's codes, unchanged."
            ),
        },
        status.HTTP_502_BAD_GATEWAY: {
            "model": ErrorResponse,
            "description": (
                "source_unreachable | source_rejected | source_too_many_redirects | "
                "source_response_too_large | source_not_html | source_no_readable_text | "
                "source_text_too_long | fetcher_error — 1.2's, each naming the paste fallback."
            ),
        },
        status.HTTP_504_GATEWAY_TIMEOUT: {
            "model": ErrorResponse,
            "description": "source_timed_out",
        },
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ErrorResponse,
            "description": (
                "rate_limit_unavailable (the fetch limiter fails closed, as 1.2's does) | "
                "service_unavailable"
            ),
        },
        **REQUEST_TOO_LARGE,
        **RATE_LIMITED,
        **NOT_SIGNED_IN,
    },
)
async def create_my_job_posting(
    request: Request,
    response: Response,
    body: Annotated[CreateJobPostingRequest, Body()],
    user_id: RequireUserDep,
) -> JobPostingResponse:
    """Capture one job posting for the bearer's account, pasted or fetched — 1.2's body, 1.2's
    codes, the principal `("user", <id>)` on the create and fetch budgets (the fetch path also
    checks the client-IP budget)."""
    raise NotImplementedError


@router.get(
    "",
    response_model=JobPostingListResponse,
    responses={**NOT_SIGNED_IN, **VALIDATION_ERROR, **SERVICE_UNAVAILABLE},
)
async def list_my_recent_job_postings(
    response: Response,
    user_id: RequireUserDep,
    limit: Annotated[int, Query(ge=1, le=RECENT_POSTINGS_MAX)] = RECENT_POSTINGS_MAX,
) -> JobPostingListResponse:
    """The account's most recent postings, newest first, as summaries with a preview — never the
    full text. `items: []` for none, never a 404."""
    raise NotImplementedError
