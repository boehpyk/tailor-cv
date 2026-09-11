"""The `tailoring` HTTP surface: request a tailoring run, list a session's runs, read one.

**SKELETON (T29).** Every handler below raises `NotImplementedError`, deliberately. The API contract
— the three paths, the status codes, the request and response schemas and every row of the failure
contract — comes from the spec rather than from FastAPI, so this tier is red-first (docs/sdlc.md
§2): the router and its schemas exist with real signatures first, `qa` writes the failing API tests
against them (T30), and the behaviour lands at T31 without a single test being edited to make it
pass. The router **is** included in `create_app`, because a test asserting `401` and reading `500`
proves the route exists and the assertion discriminates; a test reading `404` would only prove a file
is absent.

**Two contract decisions live here rather than in a commit message.**

**202, not 201.** The run resource *is* created and addressable the moment this handler returns, so
201 is defensible and is the recorded alternative. 202 wins because its meaning — "accepted for
processing, processing is not complete" — is exactly the contract the client must honour: the body it
receives has `tailored_cv`, `cover_letter`, `model` and `completed_at` all `null` and stays that way
until a worker finishes. Paired with `Location`, it tells a reader of the OpenAPI document to poll
without reading any prose. A 201 would say "here is the thing you asked for", and the thing they
asked for is not there yet.

**`require_guest_session` on all three routes, and no session is minted anywhere** (OQ-3, G-4,
AC-16). This is a **deliberate departure** from `routers/intake.py` and `routers/posting.py`, which
both mint a fresh `GuestSession` on `POST` for a missing, unknown or expired cookie — so a reader
arriving from either of those two will notice the inconsistency, and this paragraph is here so they
find the reason instead of "fixing" it. The reason is that those two POSTs *start* a session's story:
the user has an artifact in hand (a file, a pasted posting) and no prior state to lose, so minting is
the friendliest possible answer. This one *continues* it. A tailoring run names a base CV and a job
posting that a session must already own, so a request arriving without a valid session can only be
one of two things: a reference to another session's objects, which must be 404, or a resumed tab
whose session has expired, whose objects are gone with it. Minting there would hand back a brand-new
empty session and then answer 404 for the ids the user is looking at — two confusing round trips to
say "your session expired", when 401 `guest_session_expired` says it once and the client already
knows how to react. It also means the expensive endpoint of this product cannot be driven by a
cookieless caller who simply keeps asking.
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Body, Response, status

from tailorcraft.infrastructure.api.deps import RequireGuestSessionDep
from tailorcraft.infrastructure.api.schemas.intake import ErrorResponse
from tailorcraft.infrastructure.api.schemas.tailoring import (
    CreateTailoringRunRequest,
    TailoringRunListResponse,
    TailoringRunResponse,
)

router = APIRouter(prefix="/api/tailoring-runs", tags=["tailoring"])

# Shared `responses=` fragments, so one failure mode is documented with one shape at every handler
# that can produce it. `dict[str, Any]` because that is FastAPI's own type for a `responses=` entry
# (it takes a `type[BaseModel]` under "model" and a `str` under "description" in the same dict) — the
# `Any` is FastAPI's API, not a shortcut.
_GUEST_SESSION_EXPIRED: dict[int | str, dict[str, Any]] = {
    status.HTTP_401_UNAUTHORIZED: {
        "model": ErrorResponse,
        "description": (
            "guest_session_expired — missing, unknown or expired `tc_guest` cookie (G-4, G-5, "
            "G-31). **No session is minted**, unlike this API's other two POSTs."
        ),
    },
}


def _no_store(response: Response) -> None:
    """`Cache-Control: no-store` on both reads.

    1.2 established this for the posting text; here it matters more. A `TailoringRunResponse` carries
    a person's rewritten CV and a cover letter that names the employer they are applying to — the two
    together link an identifiable person to a specific job application. Without this header that body
    can land in a shared cache or an intermediary's store, which is a disclosure nobody chose.

    The list is `no-store` too, even though it carries no document body: it still enumerates which
    postings a named session is applying against, and how often.
    """
    response.headers["Cache-Control"] = "no-store"


@router.post(
    "",
    response_model=TailoringRunResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        status.HTTP_202_ACCEPTED: {
            "description": (
                "Accepted for processing. The run is committed as `queued` and handed to a worker; "
                "the body's `tailored_cv`, `cover_letter`, `model` and `completed_at` are all "
                "`null` until it finishes (AC-1). Poll the `Location` URL."
            ),
            "headers": {
                "Location": {
                    "description": "`/api/tailoring-runs/{id}` — the run to poll.",
                    "schema": {"type": "string"},
                },
            },
        },
        status.HTTP_404_NOT_FOUND: {
            "model": ErrorResponse,
            "description": (
                "base_cv_not_found | job_posting_not_found — also returned when the object exists "
                "but belongs to a different session (G-6, G-7, AC-14); never a 403, which would "
                "confirm the id is real."
            ),
        },
        status.HTTP_409_CONFLICT: {
            "model": ErrorResponse,
            "description": (
                "base_cv_not_extracted (G-8) | tailoring_already_running (G-9 — the error body "
                "carries `active_tailoring_run_id` so the client can attach its poller to the run "
                "already paying for itself) | too_many_tailoring_runs (G-10)."
            ),
        },
        status.HTTP_413_CONTENT_TOO_LARGE: {
            "model": ErrorResponse,
            "description": (
                "request_too_large — the 256 KiB JSON body cap, refused on Content-Length before "
                "the body is parsed (G-3, `MaxBodySizeMiddleware`, unchanged from 1.2)."
            ),
        },
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ErrorResponse,
            "description": (
                "validation_error — the body is not JSON, or an id is missing or not a UUID "
                "(G-1, G-2)."
            ),
        },
        status.HTTP_429_TOO_MANY_REQUESTS: {
            "model": ErrorResponse,
            "description": "rate_limited (G-11) — carries a Retry-After header.",
            "headers": {
                "Retry-After": {
                    "description": "Seconds until the smaller of the two budgets refills.",
                    "schema": {"type": "integer"},
                },
            },
        },
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ErrorResponse,
            "description": (
                "rate_limit_unavailable (G-12 — the tailoring limiter **fails closed**, because "
                "this endpoint spends money) | queue_unavailable (G-14 — the row is committed and "
                "recorded `failed`/`not_queued` before this answer) | service_unavailable (G-13)."
            ),
        },
        **_GUEST_SESSION_EXPIRED,
    },
)
async def request_tailoring_run(
    response: Response,
    body: Annotated[CreateTailoringRunRequest, Body()],
    session: RequireGuestSessionDep,
) -> TailoringRunResponse:
    """Request one tailoring run for the caller's guest session.

    **SKELETON — T31 implements this.** What lands here, in this order and for the reasons the spec
    records: the fail-closed rate limiter in both scopes (session and client IP); `RequestTailoringRun`,
    which authorizes the base CV and the job posting through their own use cases and enforces the
    active-run and per-session caps; the explicit in-handler `db.commit()` inside this function's own
    error boundary, so G-13 is reachable at all (a commit left to `get_session`'s teardown fires after
    the response is already on the wire); then **commit-then-enqueue**, with G-14's second-transaction
    `failed`/`not_queued` write when the broker refuses; and finally the `Location` header.

    `session` is `RequireGuestSessionDep` rather than `resolve_or_start_guest_session`: see this
    module's docstring for why this POST is the one that refuses instead of minting.
    """
    raise NotImplementedError("T31 implements the POST handler; T29 fixes only its contract.")


@router.get(
    "",
    response_model=TailoringRunListResponse,
    responses=_GUEST_SESSION_EXPIRED,
)
async def list_tailoring_runs(
    response: Response,
    session: RequireGuestSessionDep,
) -> TailoringRunListResponse:
    """Every `TailoringRun` the caller's session owns, newest first, **as summaries without the
    documents** (see `TailoringRunSummary`).

    A session with no runs gets `{"items": []}` and a 200 — an empty list is an ordinary answer, never
    a 404. A missing, unknown or expired cookie is a 401 (G-31), because the client must be able to
    tell "you have no runs" from "your session is gone" and react differently.

    **SKELETON — T31 implements this.** The `no-store` header is set here and not deferred, because
    it is a property of the route rather than of the answer: a handler that forgets it on one branch
    is the way this leaks.
    """
    _no_store(response)
    raise NotImplementedError("T31 implements the list handler; T29 fixes only its contract.")


@router.get(
    "/{tailoring_run_id}",
    response_model=TailoringRunResponse,
    responses={
        status.HTTP_404_NOT_FOUND: {
            "model": ErrorResponse,
            "description": (
                "tailoring_run_not_found — **identical** for an id that does not exist and one "
                "owned by a different session (G-29); never a 403, which would confirm the id is "
                "real."
            ),
        },
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ErrorResponse,
            "description": "validation_error — the path id is not a UUID (G-30).",
        },
        **_GUEST_SESSION_EXPIRED,
    },
)
async def get_tailoring_run(
    tailoring_run_id: UUID,
    response: Response,
    session: RequireGuestSessionDep,
) -> TailoringRunResponse:
    """One `TailoringRun` in full, authorized by the link to the caller's guest session.

    **This is the endpoint the client polls** while a run is `queued` or `running`, so it must stay
    cheap and must answer 200 for a *failed* run: a run that reached a worker and failed is a recorded
    state of the resource, never a 5xx (AC-12). The only 4xx here is "that run is not yours or does
    not exist".

    `tailoring_run_id` is typed `UUID` so a malformed id is FastAPI's own 422 rather than something
    this handler rejects by hand (G-30).

    **SKELETON — T31 implements this.** The `no-store` header is set here for the reason the list
    handler gives.
    """
    _no_store(response)
    raise NotImplementedError("T31 implements the detail handler; T29 fixes only its contract.")
