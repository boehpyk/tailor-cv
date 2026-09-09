"""The `posting` HTTP surface: capture a job posting, list a session's postings, read one.

**SKELETON (T25).** All three handlers raise `NotImplementedError`; T26 records the red against
these exact signatures and T27 fills them in. The router IS mounted in `create_app`, so a correct
red here is every test failing on its assertion — never on a 404, which would mean the route did not
exist and the test proved nothing about the contract.

One endpoint for two sources (ADR-0013). Both bodies produce the same resource through the same use
case, the same authorization rule, the same per-session cap and the same event; two endpoints would
be two places for all four to drift.
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Body, Request, Response, status

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
)
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
    raise NotImplementedError


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
    raise NotImplementedError


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
    raise NotImplementedError
