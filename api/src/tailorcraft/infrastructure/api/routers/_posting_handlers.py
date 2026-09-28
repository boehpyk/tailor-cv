"""The credential-agnostic bodies of the posting routes (slice 2.3, technical plan §0.2).

**Twelve account twins must not become twelve drifting copies** (2.2's R-6 lesson). So the body of
each posting handler that has a twin lives here, taking an already resolved `requester: Owner`, the
`expires_at` its responses carry, and the rate-limit principal — the `(scope, identifier)` pair the
per-principal budget is keyed on (`"session"` and the session id for a guest). The guest router
(`routers/posting.py`) is then only its decorators, its credential and a call.

**Nothing here reads a credential.** No cookie, no bearer, no `resolve_or_start_guest_session`: the
caller resolved the principal before calling, which keeps the AST scan's transfer-route set exactly
`{POST /api/base-cvs/copies}` (AC-25) — a body that resolved a guest session would make every
router calling it a guest-touching route.

The contract each body implements is documented on the guest route that calls it.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import Request, Response, status
from fastapi.exceptions import HTTPException
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from tailorcraft.application.posting.capture_job_posting import (
    CaptureJobPosting,
    FetchJobPostingCommand,
    PasteJobPostingCommand,
)
from tailorcraft.domain.identity.ownership import Owner
from tailorcraft.domain.posting.job_posting import JobPosting
from tailorcraft.domain.posting.ports import JobPostingRepository
from tailorcraft.domain.posting.value_objects import JobPostingText, SourceUrl
from tailorcraft.domain.shared.errors import DomainError
from tailorcraft.infrastructure.api.errors import domain_error_to_http_exception
from tailorcraft.infrastructure.api.schemas.posting import (
    PREVIEW_CHARACTERS,
    CreateJobPostingRequest,
    JobPostingResponse,
    JobPostingSummary,
)
from tailorcraft.infrastructure.rate_limit import (
    RateLimitDecision,
    RateLimiterUnavailable,
    RateLimitScope,
    RedisFixedWindowRateLimiter,
    client_ip,
)
from tailorcraft.infrastructure.settings import Settings

# ---------------------------------------------------------------------------------------------
# Boundary helpers. Pure functions over a saved aggregate — no I/O, so they need no fixture to
# reason about, and the preview lives here rather than in the domain because how much of a posting
# a LIST should show is a wire-format decision (see `JobPostingSummary`).
# ---------------------------------------------------------------------------------------------


def to_response(posting: JobPosting, expires_at: datetime) -> JobPostingResponse:
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


def to_summary(posting: JobPosting, expires_at: datetime) -> JobPostingSummary:
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


def no_store(response: Response) -> None:
    """`Cache-Control: no-store` on the reads.

    Unlike slice 1.1's API, this one returns user content in a response body — the posting text, and
    a source URL that names the job a specific person is applying for. Without this it can land in a
    shared cache or an intermediary's store, which is a disclosure nobody chose.
    """
    response.headers["Cache-Control"] = "no-store"


# ---------------------------------------------------------------------------------------------
# Handler bodies
# ---------------------------------------------------------------------------------------------


async def create_job_posting(
    *,
    request: Request,
    body: CreateJobPostingRequest,
    requester: Owner,
    expires_at: datetime,
    principal: tuple[RateLimitScope, str],
    settings: Settings,
    create_limiter: RedisFixedWindowRateLimiter,
    fetch_limiter: RedisFixedWindowRateLimiter,
    capture: CaptureJobPosting,
    postings: JobPostingRepository,
    db: AsyncSession,
) -> JobPostingResponse:
    """Capture one job posting for `requester`, pasted or fetched — `POST /api/job-postings`'s body.

    `principal` keys the per-principal budgets (create, and fetch for a `fetched` body); the fetch
    path also checks the client-IP budget. Each limit is the setting the guest route has always used.
    """
    principal_scope, principal_id = principal

    # Two limiters, and they fail in OPPOSITE directions on an unreachable Redis. Creating a posting
    # costs our own database, so that one fails open. Fetching spends someone else's infrastructure
    # from our IP address, so that one fails closed — an unbounded outbound endpoint with no backstop
    # is how a server lands on a job board's blocklist (ADR-0012).
    #
    # The create limiter is checked for BOTH sources; the fetch limiter only for `fetched`, because
    # a paste makes no outbound request and should not consume an outbound budget.
    create_decision = await create_limiter.check(
        principal_scope, principal_id, settings.posting_rate_limit_per_hour
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
            PasteJobPostingCommand(owner=requester, text=JobPostingText(body.text))
            if body.source == "pasted"
            else FetchJobPostingCommand(owner=requester, url=SourceUrl(body.url))
        )
    except DomainError as exc:
        raise domain_error_to_http_exception(exc) from exc

    if body.source == "fetched":
        try:
            decisions.append(
                await fetch_limiter.check(
                    principal_scope,
                    principal_id,
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
    wire = to_response(saved, expires_at)

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
