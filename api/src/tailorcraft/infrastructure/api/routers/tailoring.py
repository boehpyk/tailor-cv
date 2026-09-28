"""The `tailoring` HTTP surface: request a tailoring run, list a session's runs, read one, and
revise one of a run's documents.

Built in three passes (docs/sdlc.md §2): **SKELETON** (T29 — real paths, real schemas,
`NotImplementedError` bodies), **RED** (T30, `qa` — 42 tests against those exact signatures, 26 of
them failing on their assertions), **GREEN** (T31, this file — the rate limiter, commit-then-enqueue,
the response shaping and the `DomainError` -> status translation, until those tests pass without a
single edit to them). The contract — the three paths, the status codes, the schemas and every row of
the failure contract — came from the spec rather than from FastAPI, which is why this tier was
red-first at all.

Slice 1.4 (`workspace-progress-and-editor`, ADR-0015) added the fourth route the same way: **T12**
the `PUT` skeleton and the three new response fields, **T13** `qa`'s red, **T14** the handler.

**Two contract decisions live here rather than in a commit message.**

**202, not 201.** The run resource *is* created and addressable the moment this handler returns, so
201 is defensible and is the recorded alternative. 202 wins because its meaning — "accepted for
processing, processing is not complete" — is exactly the contract the client must honour: the body it
receives has `tailored_cv`, `cover_letter`, `model` and `completed_at` all `null` and stays that way
until a worker finishes. Paired with `Location`, it tells a reader of the OpenAPI document to poll
without reading any prose. A 201 would say "here is the thing you asked for", and the thing they
asked for is not there yet.

**`require_guest_session` on all four routes, and no session is minted anywhere** (OQ-3, G-4,
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

**What this module logs, and what it never logs** (AC-21, Constitution §8): ids, scopes, namespaces
and exception *type names*. Never a CV, a posting or a tailored document — the write path never holds
one in a local, and the two reads hand them straight to the response model. **Never a client IP**: it
is computed once, handed to the limiter as an identifier, and goes nowhere else — in particular never
into a line that also carries a `tailoring_run_id`, which would pair an identifiable address with a
specific job application.
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Body, Request, Response, status

from tailorcraft.domain.identity.ownership import GuestOwner
from tailorcraft.domain.shared.errors import DomainError
from tailorcraft.domain.tailoring.value_objects import TailoredDocumentKind, TailoringRunId
from tailorcraft.infrastructure.api.deps import (
    ClockDep,
    EventPublisherDep,
    GetTailoringRunDep,
    ListTailoringRunsDep,
    RequestTailoringRunDep,
    RequireGuestSessionDep,
    ReviseTailoredDocumentDep,
    SessionDep,
    SettingsDep,
    TailoringQueueDep,
    TailoringRateLimiterDep,
    TailoringReviseRateLimiterDep,
    TailoringRunRepositoryDep,
)
from tailorcraft.infrastructure.api.errors import domain_error_to_http_exception
from tailorcraft.infrastructure.api.routers import _tailoring_handlers as handlers
from tailorcraft.infrastructure.api.schemas.intake import ErrorResponse
from tailorcraft.infrastructure.api.schemas.tailoring import (
    CreateTailoringRunRequest,
    ReviseDocumentRequest,
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


# ---------------------------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------------------------


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
    request: Request,
    response: Response,
    body: Annotated[CreateTailoringRunRequest, Body()],
    session: RequireGuestSessionDep,
    settings: SettingsDep,
    clock: ClockDep,
    rate_limiter: TailoringRateLimiterDep,
    request_run: RequestTailoringRunDep,
    runs: TailoringRunRepositoryDep,
    queue: TailoringQueueDep,
    events: EventPublisherDep,
    db: SessionDep,
) -> TailoringRunResponse:
    """Request one tailoring run for the caller's guest session.

    In this order, and each step's position is load-bearing: the fail-closed rate limiter in both
    scopes; `RequestTailoringRun`, which authorizes the base CV and the job posting through their own
    use cases and enforces the active-run and per-session caps; the explicit in-handler commit;
    **then** the enqueue, with G-14's second transaction when the broker refuses; and finally the
    `Location` header.

    `session` is `RequireGuestSessionDep` rather than `resolve_or_start_guest_session`: see this
    module's docstring for why this POST is the one that refuses instead of minting. It is also safe
    as an ordinary `Depends()` here, unlike the minting variant in the other two routers, because it
    never writes anything — so FastAPI resolving it for a request whose body then fails validation
    costs nothing.
    """
    # The body is shared with the account twin (`_tailoring_handlers.py`, technical plan §0.2).
    return await handlers.request_tailoring_run(
        request=request,
        response=response,
        body=body,
        requester=GuestOwner(session.id),
        expires_at=session.expires_at,
        principal=("session", str(session.id.value)),
        location_prefix=router.prefix,
        settings=settings,
        clock=clock,
        rate_limiter=rate_limiter,
        request_run=request_run,
        runs=runs,
        queue=queue,
        events=events,
        db=db,
    )


@router.get(
    "",
    response_model=TailoringRunListResponse,
    responses=_GUEST_SESSION_EXPIRED,
)
async def list_tailoring_runs(
    response: Response,
    session: RequireGuestSessionDep,
    list_use_case: ListTailoringRunsDep,
) -> TailoringRunListResponse:
    """Every `TailoringRun` the caller's session owns, newest first, **as summaries without the
    documents** (see `TailoringRunSummary`).

    A session with no runs gets `{"items": []}` and a 200 — an empty list is an ordinary answer, never
    a 404. A missing, unknown or expired cookie is a 401 (G-31), because the client must be able to
    tell "you have no runs" from "your session is gone" and react differently.

    The `no-store` header is set before anything else, because it is a property of the route rather
    than of the answer: a handler that forgets it on one branch is the way this leaks.
    """
    handlers.no_store(response)
    try:
        runs = await list_use_case(session.id)
    except DomainError as exc:
        raise domain_error_to_http_exception(exc) from exc

    return TailoringRunListResponse(
        items=[handlers.to_summary(run, session.expires_at) for run in runs]
    )


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
    get_use_case: GetTailoringRunDep,
) -> TailoringRunResponse:
    """One `TailoringRun` in full, authorized by the link to the caller's guest session.

    **This is the endpoint the client polls** while a run is `queued` or `running`, so it must stay
    cheap and must answer 200 for a *failed* run: a run that reached a worker and failed is a recorded
    state of the resource, never a 5xx (AC-12). The only 4xx here is "that run is not yours or does
    not exist", and the two are the same 404 because `GetTailoringRun` raises the same type
    for both (G-29).

    `tailoring_run_id` is typed `UUID` so a malformed id is FastAPI's own 422 rather than something
    this handler rejects by hand (G-30).
    """
    return await handlers.get_tailoring_run(
        tailoring_run_id=TailoringRunId(tailoring_run_id),
        response=response,
        requester=GuestOwner(session.id),
        expires_at=session.expires_at,
        get_use_case=get_use_case,
    )


@router.put(
    "/{tailoring_run_id}/documents/{kind}",
    response_model=TailoringRunResponse,
    responses={
        status.HTTP_200_OK: {
            "description": (
                "The revision is committed. The body is the full run at its new `version`, with "
                "`tailored_cv` / `cover_letter` meaning the **current** document (ADR-0015 §4) and "
                "the revised one's `*_edited_at` set. `Cache-Control: no-store`."
            ),
        },
        status.HTTP_404_NOT_FOUND: {
            "model": ErrorResponse,
            "description": (
                "tailoring_run_not_found — **identical** for an id that does not exist and one "
                "owned by a different session (E-6, AC-14); never a 403, which would confirm the "
                "id is real."
            ),
        },
        status.HTTP_409_CONFLICT: {
            "model": ErrorResponse,
            "description": (
                "tailoring_run_not_editable (E-7 — the run is `queued`, `running` or `failed`; the "
                "error body carries `status`) | document_version_conflict (E-8 — a stale "
                "`expected_version`, the body carries `current_version`; E-9 — two writers raced "
                "and the second `UPDATE` matched no row, the body carries `current_version: null` "
                "and the client refetches for the number)."
            ),
        },
        status.HTTP_413_CONTENT_TOO_LARGE: {
            "model": ErrorResponse,
            "description": (
                "request_too_large — the 256 KiB JSON body cap, refused on Content-Length before "
                "the body is parsed (E-3, `MaxBodySizeMiddleware`, unchanged). The value object's "
                "20,000-character ceiling is the real bound; this cap is the transport's."
            ),
        },
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ErrorResponse,
            "description": (
                "validation_error — the body is not JSON, `content` is missing or not a string, "
                "`expected_version` is missing, not an integer or `< 1`, an extra field is present, "
                "the run id is not a UUID, or `kind` is not `cv` | `cover_letter` (E-1, E-2, E-4) "
                "| document_invalid — the value object refused the text; the error body carries "
                "`problem`: `empty` | `too_short` | `too_long` | `invalid_characters` (E-10 … "
                "E-12). **Never the text**, in the body or in a log line."
            ),
        },
        status.HTTP_429_TOO_MANY_REQUESTS: {
            "model": ErrorResponse,
            "description": (
                "rate_limited (E-30 — 600 saves per session per hour) — carries a Retry-After "
                "header."
            ),
            "headers": {
                "Retry-After": {
                    "description": "Seconds until the budget refills.",
                    "schema": {"type": "integer"},
                },
            },
        },
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ErrorResponse,
            "description": (
                "service_unavailable — Postgres down, or the commit failed; nothing survives the "
                "rollback (E-14). **Not** rate_limit_unavailable: the save limiter fails open "
                "(E-31), because the cost of a save is one bounded `UPDATE` of ours, not money."
            ),
        },
        **_GUEST_SESSION_EXPIRED,
    },
)
async def revise_tailored_document(
    tailoring_run_id: UUID,
    kind: TailoredDocumentKind,
    response: Response,
    body: Annotated[ReviseDocumentRequest, Body()],
    session: RequireGuestSessionDep,
    settings: SettingsDep,
    rate_limiter: TailoringReviseRateLimiterDep,
    revise: ReviseTailoredDocumentDep,
    db: SessionDep,
) -> TailoringRunResponse:
    """Replace the current content of one of a `succeeded` run's two documents with the caller's
    revision, and answer the full run at its new `version`.

    **`PUT`, not `PATCH` and not `POST …/revisions`** (ADR-0015 §4): the request replaces the
    current content of one sub-resource in full, and there is no revision collection to post into —
    the run keeps the draft and one current text per document, no history. `expected_version` rides
    in the body rather than in `If-Match`, because the client already holds `version` as a JSON
    field and the 409 body must carry `current_version`.

    **Validation at the boundary, in this order, and each step's position is load-bearing:**

    1. **The body cap** — `MaxBodySizeMiddleware`, 256 KiB on `Content-Length`, before a byte of the
       body is read (E-3 → 413 `request_too_large`).
    2. **The shape** — `ReviseDocumentRequest`: `content: str`, `expected_version: int >= 1`,
       `extra="forbid"` (E-1, E-2 → 422 `validation_error`).
    3. **The path** — `tailoring_run_id: UUID` and `kind: TailoredDocumentKind`, so a malformed id
       or an unknown kind is FastAPI's own 422 `validation_error` (E-4). The enum's string values
       *are* the URL segments (`cv`, `cover_letter`); there is no mapping to get wrong.
    4. **The rate limiter** — `tailoring:revise`, session scope, 600/hour, **fail-open** (E-30 → 429
       + `Retry-After`; E-31 → the save proceeds). After shape validation so a malformed request
       never consumes a counter; before the value object so a hostile client cannot burn CPU on
       20,000-character strings past its budget.
    5. **The value object, constructed HERE** — `match kind`: `TailoredCv(body.content)` or
       `CoverLetter(body.content)`. Its four failures collapse to one 422 `document_invalid` with a
       `problem` label (E-10 … E-12). In the router and not the use case because this *is* the
       validation boundary, and because the command's type (`ReviseCvCommand.content: TailoredCv`)
       then says what it holds rather than re-deriving it from `kind` a second time.
    6. **The use case** — `ReviseTailoredDocument`, through the composed read, which is where "not
       mine" becomes the same 404 as "does not exist" (E-6), `TailoringRunNotEditable` becomes 409
       `tailoring_run_not_editable` with `status` (E-7), and `TailoredDocumentVersionConflict`
       becomes 409 `document_version_conflict` with `current_version` (E-8).
    7. **The commit, inside this handler's own error boundary** — never left to the session
       dependency's teardown, which runs after the response is on the wire. A failed commit rolls
       back and answers 503 `service_unavailable` (E-14). The optimistic `UPDATE … WHERE version =
       :seen` that matched no row surfaces here as `TailoringRunConcurrentlyModified` → 409
       `document_version_conflict` with `current_version: null` (E-9).
    8. **`Cache-Control: no-store`** — set before anything else, as on the two reads: the body is
       a person's rewritten CV and a letter naming the employer.

    `session` is `RequireGuestSessionDep`, never the minting variant, for the reason the module
    docstring gives (E-5 → 401, no `Set-Cookie`, no session row — AC-15).

    **What this handler logs, and never logs** (AC-19): the run id, `kind`, `problem`, the version
    numbers, `character_count`, exception *type names*. Never `body.content`, never a fragment of
    either document, never the client IP alongside a run id — there is no IP here at all: the save
    limiter is session-scoped only (deps.py says why), so this handler never reads the request.
    """
    # Two plain values off the session aggregate, taken BEFORE any I/O, and the timing is not
    # tidiness. `session` is a mapped instance the dependency loaded into this request's
    # `AsyncSession`; a flush that fails (E-9, the `StaleDataError` the repository translates)
    # rolls its transaction back on the way out, and that rollback **expires every instance the
    # session holds** — the guest session included — while leaving the transaction inactive until
    # the boundary rolls back. Reading `session.id` after that would be a lazy load on a session that
    # refuses to run one: a `PendingRollbackError` in place of the 409. Found by AC-12(b)'s test; a
    # frozen value object read up front cannot expire.
    guest_session_id = session.id
    expires_at = session.expires_at
    # The body is shared with the account twin (`_tailoring_handlers.py`, technical plan §0.2).
    return await handlers.revise_tailored_document(
        run_id=TailoringRunId(tailoring_run_id),
        kind=kind,
        response=response,
        body=body,
        requester=GuestOwner(guest_session_id),
        expires_at=expires_at,
        principal=("session", str(guest_session_id.value)),
        settings=settings,
        rate_limiter=rate_limiter,
        revise=revise,
        db=db,
    )
