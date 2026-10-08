"""A signed-in user's job postings: capture one, list the recent ones (slice 2.3, technical plan §4).

Built red-first (sdlc.md §2): **SKELETON** (T20 — real paths, real schemas, every documented status
in each `responses=` map), **RED** (T21, `qa`), **GREEN** (T23, this file — thin calls into
`_posting_handlers.py`, T19's shared bodies, with the account's principal and `expires_at: null`).

**One credential: the bearer** (`require_user`). A `__Host-tc_guest` cookie riding along changes
nothing, and nothing in this module reads it (ADR-0008 (f)): the account twin of `POST
/api/job-postings`, not a transfer route. A guest's posting id is not reachable here, and there is
no `GET /{id}` — a user's posting is read through its history entry.

Every response carries `Cache-Control: no-store`, and every `expires_at` is `null`: an account
posting is kept until the history entry that uses it is deleted (OQ-8).
"""

from __future__ import annotations

from functools import partial
from typing import Annotated

from fastapi import APIRouter, Body, Query, Request, Response, status

from tailorcraft.domain.identity.ownership import UserOwner
from tailorcraft.domain.shared.errors import DomainError
from tailorcraft.infrastructure.api.deps import (
    CaptureJobPostingDep,
    JobPostingRepositoryDep,
    ListRecentJobPostingsDep,
    PostingCreateRateLimiterDep,
    PostingFetchRateLimiterDep,
    RequireUserDep,
    SessionDep,
    SettingsDep,
)
from tailorcraft.infrastructure.api.errors import (
    account_error_to_http_exception,
    domain_error_to_http_exception,
)
from tailorcraft.infrastructure.api.routers import _posting_handlers as handlers
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
# AC-28: the workspace shows the newest posting, so one is the default; the picker asks for more.
RECENT_POSTINGS_DEFAULT = 1


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
    settings: SettingsDep,
    create_limiter: PostingCreateRateLimiterDep,
    fetch_limiter: PostingFetchRateLimiterDep,
    capture: CaptureJobPostingDep,
    postings: JobPostingRepositoryDep,
    db: SessionDep,
) -> JobPostingResponse:
    """Capture one job posting for the bearer's account, pasted or fetched — 1.2's body, 1.2's
    codes, the principal `("user", <id>)` on the create and fetch budgets (the fetch path also
    checks the client-IP budget). The create budget fails open and the fetch budget closed, as
    1.2's do; the cap is checked before any fetch (H-10). The shared body commits."""
    response.headers["Cache-Control"] = "no-store"
    return await handlers.create_job_posting(
        request=request,
        body=body,
        requester=UserOwner(user_id),
        expires_at=None,
        principal=("user", str(user_id.value)),
        settings=settings,
        create_limiter=create_limiter,
        fetch_limiter=fetch_limiter,
        capture=capture,
        postings=postings,
        db=db,
        translate=partial(
            account_error_to_http_exception,
            max_job_postings_per_user=settings.max_job_postings_per_user,
        ),
    )


@router.get(
    "",
    response_model=JobPostingListResponse,
    responses={**NOT_SIGNED_IN, **VALIDATION_ERROR, **SERVICE_UNAVAILABLE},
)
async def list_my_recent_job_postings(
    response: Response,
    user_id: RequireUserDep,
    list_recent: ListRecentJobPostingsDep,
    db: SessionDep,
    limit: Annotated[int, Query(ge=1, le=RECENT_POSTINGS_MAX)] = RECENT_POSTINGS_DEFAULT,
) -> JobPostingListResponse:
    """The account's most recent postings, newest first, as summaries with a preview — never the
    full text. `items: []` for none, never a 404.

    Writes nothing and commits anyway — 2.1's `/me` reason: `get_session`'s commit runs after the
    response is on the wire, so a failing database would otherwise answer 200.
    """
    handlers.no_store(response)
    try:
        postings = await list_recent(user_id, limit)
    except DomainError as exc:
        raise domain_error_to_http_exception(exc) from None

    wire = JobPostingListResponse(
        items=[handlers.to_summary(posting, None) for posting in postings]
    )
    await db.commit()
    return wire
