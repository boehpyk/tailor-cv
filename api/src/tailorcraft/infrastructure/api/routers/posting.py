"""The `posting` HTTP surface: capture a job posting, list a session's postings, read one.

One endpoint for two sources (ADR-0013). Both bodies produce the same resource through the same use
case, the same authorization rule, the same per-session cap and the same event; two endpoints would
be two places for all four to drift.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Body, Request, Response, status
from fastapi.exceptions import HTTPException
from sqlalchemy.exc import SQLAlchemyError

from tailorcraft.application.posting.capture_job_posting import (
    FetchJobPostingCommand,
    PasteJobPostingCommand,
)
from tailorcraft.domain.posting.job_posting import JobPosting
from tailorcraft.domain.posting.value_objects import JobPostingId, JobPostingText, SourceUrl
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
from tailorcraft.infrastructure.api.schemas.intake import ErrorResponse
from tailorcraft.infrastructure.api.schemas.posting import (
    PREVIEW_CHARACTERS,
    CreateJobPostingRequest,
    JobPostingListResponse,
    JobPostingResponse,
    JobPostingSummary,
)
from tailorcraft.infrastructure.rate_limit import (
    RateLimitDecision,
    RateLimiterUnavailable,
    client_ip,
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


# ---------------------------------------------------------------------------------------------
# Boundary helpers. Pure functions over a saved aggregate — no I/O, so they need no fixture to
# reason about, and the preview lives here rather than in the domain because how much of a posting
# a LIST should show is a wire-format decision (see `JobPostingSummary`).
# ---------------------------------------------------------------------------------------------


def _to_response(posting: JobPosting, expires_at: datetime) -> JobPostingResponse:
    """The full shape, including the text. `expires_at` is the SESSION's, not the row's — the
    session owns the 24-hour promise, and carrying it here puts that promise in the payload as well
    as in the UI copy."""
    return JobPostingResponse(
        id=posting.id.value,
        source=posting.source,
        source_url=posting.source_url.value if posting.source_url is not None else None,
        title=posting.title.value if posting.title is not None else None,
        character_count=posting.text.character_count,
        text=posting.text.value,
        created_at=posting.created_at,
        expires_at=expires_at,
    )


def _to_summary(posting: JobPosting, expires_at: datetime) -> JobPostingSummary:
    """The list shape: everything except the full text, plus a bounded preview.

    Five postings at 30,000 characters is 150 KB of user content in one response and in whatever
    caches it; the detail endpoint exists for the one posting the user actually opened.
    """
    text = posting.text.value
    return JobPostingSummary(
        id=posting.id.value,
        source=posting.source,
        source_url=posting.source_url.value if posting.source_url is not None else None,
        title=posting.title.value if posting.title is not None else None,
        character_count=posting.text.character_count,
        preview=text[:PREVIEW_CHARACTERS],
        created_at=posting.created_at,
        expires_at=expires_at,
    )


def _no_store(response: Response) -> None:
    """`Cache-Control: no-store` on both reads.

    Unlike slice 1.1's API, this one returns user content in a response body — the posting text, and
    a source URL that names the job a specific person is applying for. Without this it can land in a
    shared cache or an intermediary's store, which is a disclosure nobody chose.
    """
    response.headers["Cache-Control"] = "no-store"


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

    # Two limiters, and they fail in OPPOSITE directions on an unreachable Redis. Creating a posting
    # costs our own database, so that one fails open. Fetching spends someone else's infrastructure
    # from our IP address, so that one fails closed — an unbounded outbound endpoint with no backstop
    # is how a server lands on a job board's blocklist (ADR-0012).
    #
    # The create limiter is checked for BOTH sources; the fetch limiter only for `fetched`, because
    # a paste makes no outbound request and should not consume an outbound budget.
    create_decision = await create_limiter.check(
        "session", str(session.id.value), settings.posting_rate_limit_per_hour
    )
    # The create budget is answered IMMEDIATELY, before validation — matching slice 1.1's upload
    # handler, which likewise answers 429 before it validates a filename.
    #
    # Deferring this one alongside the fetch checks was a regression introduced while fixing their
    # ordering: a session over its create budget that also sent a bad URL got 422, spent the counter
    # anyway, fixed the URL, and only then learned it was rate-limited. Two round trips to deliver
    # one piece of bad news, and the second one contradicted the first. The fetch checks are the
    # ones that must wait for validation, because they bound OUTBOUND requests; this one bounds our
    # own database and an attempt is an attempt either way.
    if not create_decision.allowed:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "code": "rate_limited",
                "message": (
                    f"Too many job postings. Try again in "
                    f"{create_decision.retry_after_seconds} seconds."
                ),
            },
            headers={"Retry-After": str(create_decision.retry_after_seconds)},
        )

    decisions: list[RateLimitDecision] = []

    # Boundary validation happens HERE — between the two limiters — and the ordering is deliberate.
    #
    # The `create` counter is consumed first because every source costs a row if it succeeds, so an
    # attempt is an attempt. The `fetch` counters are consumed only AFTER the URL has been proven to
    # be a `SourceUrl`, because they bound outbound requests and a request that fails validation
    # never reaches the network. Checking them first means a visitor who mistypes `htp://` four
    # times spends four of their ten hourly fetches on requests that never opened a socket — a limit
    # they cannot see, enforced against something they did not do.
    #
    # This is also where `file:///etc/passwd` dies (ADR-0012 obligation 1): the type refuses it, so
    # the fetch counters are never even reached for a URL we would not have fetched.
    try:
        command = (
            PasteJobPostingCommand(guest_session_id=session.id, text=JobPostingText(body.text))
            if body.source == "pasted"
            else FetchJobPostingCommand(guest_session_id=session.id, url=SourceUrl(body.url))
        )
    except DomainError as exc:
        raise domain_error_to_http_exception(exc) from exc

    if body.source == "fetched":
        try:
            decisions.append(
                await fetch_limiter.check(
                    "session",
                    str(session.id.value),
                    settings.posting_fetch_rate_limit_per_hour,
                )
            )
            decisions.append(
                await fetch_limiter.check(
                    "ip",
                    client_ip(request, settings.trusted_proxy_hops),
                    settings.posting_fetch_rate_limit_per_ip_per_hour,
                )
            )
        except RateLimiterUnavailable as exc:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "code": "rate_limit_unavailable",
                    "message": "We cannot read links just now. Paste the description instead.",
                },
            ) from exc

    if any(not decision.allowed for decision in decisions):
        retry_after = max(decision.retry_after_seconds for decision in decisions)
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "code": "rate_limited",
                "message": f"Too many job postings. Try again in {retry_after} seconds.",
            },
            headers={"Retry-After": str(retry_after)},
        )

    try:
        result = await capture(command)
    except DomainError as exc:
        # Every `JobPostingFetchFailed` subclass arrives here, having propagated straight through
        # the use case (ADR-0013). This is the boundary that turns one into a status and a code.
        raise domain_error_to_http_exception(exc) from exc

    saved = await postings.get(result.job_posting_id)
    wire = _to_response(saved, session.expires_at)

    # Commit HERE rather than leaving it to `get_session`'s teardown, and this is the only place a
    # failed commit can still change the answer. FastAPI runs the exit half of a yield-dependency
    # AFTER the response is sent, so a commit failure there fires with the 201 already on the wire
    # and the client keeps it. P-36 is only reachable from inside the handler's own error boundary.
    try:
        await db.commit()
    except SQLAlchemyError as exc:
        await db.rollback()
        # Unlike slice 1.1's equivalent, nothing was written outside this transaction — no file, no
        # cache entry, no queue row — so the rollback leaves nothing behind and there is no orphan
        # for 1.6's sweep to find.
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "service_unavailable",
                "message": "Could not save that job posting just now. Please try again.",
            },
        ) from exc

    return wire


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
    _no_store(response)
    try:
        postings = await list_use_case(session.id)
    except DomainError as exc:
        raise domain_error_to_http_exception(exc) from exc

    return JobPostingListResponse(
        items=[_to_summary(posting, session.expires_at) for posting in postings]
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
    _no_store(response)
    try:
        posting = await get_use_case(JobPostingId(job_posting_id), session.id)
    except DomainError as exc:
        raise domain_error_to_http_exception(exc) from exc

    return _to_response(posting, session.expires_at)
