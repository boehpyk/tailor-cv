"""The `posting` HTTP surface: capture a job posting, list a session's postings, read one.

One endpoint for two sources (ADR-0013). Both bodies produce the same resource through the same use
case, the same authorization rule, the same per-session cap and the same event; two endpoints would
be two places for all four to drift.
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Body, Request, Response, status

from tailorcraft.domain.identity.ownership import GuestOwner
from tailorcraft.domain.posting.value_objects import JobPostingId
from tailorcraft.domain.shared.errors import DomainError
from tailorcraft.infrastructure.api.deps import (
    CaptureJobPostingDep,
    ClockDep,
    GetJobPostingDep,
    GuestSessionRepositoryDep,
    JobPostingRepositoryDep,
    ListJobPostingsDep,
    PostingCreateRateLimiterDep,
    PostingFetchRateLimiterDep,
    RequireGuestSessionDep,
    SessionDep,
    SettingsDep,
    StartGuestSessionDep,
    resolve_or_start_guest_session,
)
from tailorcraft.infrastructure.api.errors import domain_error_to_http_exception
from tailorcraft.infrastructure.api.routers import _posting_handlers as handlers
from tailorcraft.infrastructure.api.schemas.intake import ErrorResponse
from tailorcraft.infrastructure.api.schemas.posting import (
    CreateJobPostingRequest,
    JobPostingListResponse,
    JobPostingResponse,
)

router = APIRouter(prefix="/api/job-postings", tags=["posting"])

# Shared `responses=` fragments, so one failure mode is documented with one shape at every handler
# that can produce it. `dict[str, Any]` because that is FastAPI's own type for a `responses=` entry
# (it takes a `type[BaseModel]` under "model" and a `str` under "description" in the same dict) — the
# `Any` is FastAPI's API, not a shortcut.
_GUEST_SESSION_EXPIRED: dict[int | str, dict[str, Any]] = {
    status.HTTP_401_UNAUTHORIZED: {
        "model": ErrorResponse,
        "description": "guest_session_expired — missing, unknown or expired `tc_guest` cookie.",
    },
}


@router.post(
    "",
    response_model=JobPostingResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ErrorResponse,
            "description": (
                "validation_error | invalid_source_url | posting_text_too_short | "
                "posting_text_too_long | fetch_blocked"
            ),
        },
        status.HTTP_413_CONTENT_TOO_LARGE: {
            "model": ErrorResponse,
            "description": "request_too_large — the 256 KiB JSON body cap, refused on "
            "Content-Length before the body is parsed.",
        },
        status.HTTP_409_CONFLICT: {
            "model": ErrorResponse,
            "description": "too_many_job_postings — the session already owns the per-session cap.",
        },
        status.HTTP_429_TOO_MANY_REQUESTS: {
            "model": ErrorResponse,
            "description": "rate_limited — carries a Retry-After header.",
        },
        status.HTTP_502_BAD_GATEWAY: {
            "model": ErrorResponse,
            "description": (
                "source_unreachable | source_rejected | source_too_many_redirects | "
                "source_response_too_large | source_not_html | source_no_readable_text | "
                "source_text_too_long | fetcher_error — every one of them names the paste fallback"
            ),
        },
        status.HTTP_504_GATEWAY_TIMEOUT: {
            "model": ErrorResponse,
            "description": "source_timed_out",
        },
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ErrorResponse,
            "description": "rate_limit_unavailable | service_unavailable",
        },
    },
)
async def create_job_posting(
    request: Request,
    response: Response,
    body: Annotated[CreateJobPostingRequest, Body()],
    settings: SettingsDep,
    clock: ClockDep,
    sessions: GuestSessionRepositoryDep,
    start_guest_session: StartGuestSessionDep,
    create_limiter: PostingCreateRateLimiterDep,
    fetch_limiter: PostingFetchRateLimiterDep,
    capture: CaptureJobPostingDep,
    postings: JobPostingRepositoryDep,
    db: SessionDep,
) -> JobPostingResponse:
    """Capture one job posting for the caller's guest session, pasted or fetched.

    Tolerates a missing, unknown or expired `tc_guest` cookie by minting a fresh `GuestSession` and
    returning it via `Set-Cookie` (P-27/P-28) — unlike the two reads below, this never answers 401
    for a bad cookie.
    """
    # `resolve_or_start_guest_session` is called HERE, in the body, and never as a `Depends()`.
    # Slice 1.1 established empirically that FastAPI resolves a route's sibling dependencies even
    # when another required parameter of that same route fails validation — dependency resolution
    # and body validation are separate steps and the former does not short-circuit on the latter.
    # A `Depends()` here would therefore mint and flush a `GuestSession` row for a request whose
    # JSON body never validated. That footgun is not upload-specific: it applies verbatim to a
    # tagged-union body, which is why this line looks like a needless deviation from the obvious
    # style and is not one.
    session = await resolve_or_start_guest_session(
        request, response, sessions, clock, settings, start_guest_session
    )
    # The body is shared with the account twin (`_posting_handlers.py`, technical plan §0.2).
    return await handlers.create_job_posting(
        request=request,
        body=body,
        requester=GuestOwner(session.id),
        expires_at=session.expires_at,
        principal=("session", str(session.id.value)),
        settings=settings,
        create_limiter=create_limiter,
        fetch_limiter=fetch_limiter,
        capture=capture,
        postings=postings,
        db=db,
    )


@router.get("", response_model=JobPostingListResponse, responses=_GUEST_SESSION_EXPIRED)
async def list_job_postings(
    response: Response,
    session: RequireGuestSessionDep,
    list_use_case: ListJobPostingsDep,
) -> JobPostingListResponse:
    """Every `JobPosting` the caller's session owns, as summaries with a preview — never the full
    text of every posting (see `JobPostingSummary`).

    A missing, unknown or expired cookie is a 401 here, not a fresh session (P-29): the client must
    be able to tell "you have no postings" from "your session is gone" and react differently.
    """
    handlers.no_store(response)
    try:
        postings = await list_use_case(session.id)
    except DomainError as exc:
        raise domain_error_to_http_exception(exc) from exc

    return JobPostingListResponse(
        items=[handlers.to_summary(posting, session.expires_at) for posting in postings]
    )


@router.get(
    "/{job_posting_id}",
    response_model=JobPostingResponse,
    responses={
        status.HTTP_404_NOT_FOUND: {
            "model": ErrorResponse,
            "description": "job_posting_not_found — also returned for a posting owned by a "
            "different session (P-30); never a 403, which would confirm the id exists.",
        },
        **_GUEST_SESSION_EXPIRED,
    },
)
async def get_job_posting(
    job_posting_id: UUID,
    response: Response,
    session: RequireGuestSessionDep,
    get_use_case: GetJobPostingDep,
) -> JobPostingResponse:
    """One `JobPosting` in full, authorized by the link to the caller's guest session.

    `job_posting_id` is typed `UUID` so a malformed id is FastAPI's own 422 rather than something
    this handler rejects by hand (P-31). A well-formed id naming another session's posting is
    indistinguishable from one that does not exist: both are 404.
    """
    handlers.no_store(response)
    try:
        posting = await get_use_case(JobPostingId(job_posting_id), GuestOwner(session.id))
    except DomainError as exc:
        raise domain_error_to_http_exception(exc) from exc

    return handlers.to_response(posting, session.expires_at)
