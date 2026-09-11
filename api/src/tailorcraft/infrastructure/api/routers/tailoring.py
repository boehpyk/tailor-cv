"""The `tailoring` HTTP surface: request a tailoring run, list a session's runs, read one.

Built in three passes (docs/sdlc.md §2): **SKELETON** (T29 — real paths, real schemas,
`NotImplementedError` bodies), **RED** (T30, `qa` — 42 tests against those exact signatures, 26 of
them failing on their assertions), **GREEN** (T31, this file — the rate limiter, commit-then-enqueue,
the response shaping and the `DomainError` -> status translation, until those tests pass without a
single edit to them). The contract — the three paths, the status codes, the schemas and every row of
the failure contract — came from the spec rather than from FastAPI, which is why this tier was
red-first at all.

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

**What this module logs, and what it never logs** (AC-21, Constitution §8): ids, scopes, namespaces
and exception *type names*. Never a CV, a posting or a tailored document — the write path never holds
one in a local, and the two reads hand them straight to the response model. **Never a client IP**: it
is computed once, handed to the limiter as an identifier, and goes nowhere else — in particular never
into a line that also carries a `tailoring_run_id`, which would pair an identifiable address with a
specific job application.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, assert_never
from uuid import UUID

import structlog
from fastapi import APIRouter, Body, Request, Response, status
from fastapi.exceptions import HTTPException
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from tailorcraft.application.tailoring.request_tailoring_run import RequestTailoringRunCommand
from tailorcraft.domain.intake.value_objects import BaseCvId
from tailorcraft.domain.posting.value_objects import JobPostingId
from tailorcraft.domain.shared.clock import Clock
from tailorcraft.domain.shared.errors import DomainError
from tailorcraft.domain.shared.events import EventPublisherPort
from tailorcraft.domain.tailoring.errors import TailoringNotQueued
from tailorcraft.domain.tailoring.ports import TailoringRunRepository
from tailorcraft.domain.tailoring.tailoring_run import TailoringRun
from tailorcraft.domain.tailoring.value_objects import TailoringFailureReason, TailoringRunId
from tailorcraft.infrastructure.api.deps import (
    ClockDep,
    EventPublisherDep,
    GetTailoringRunDep,
    ListTailoringRunsDep,
    RequestTailoringRunDep,
    RequireGuestSessionDep,
    SessionDep,
    SettingsDep,
    TailoringQueueDep,
    TailoringRateLimiterDep,
    TailoringRunRepositoryDep,
)
from tailorcraft.infrastructure.api.errors import domain_error_to_http_exception
from tailorcraft.infrastructure.api.schemas.intake import ErrorResponse
from tailorcraft.infrastructure.api.schemas.tailoring import (
    CreateTailoringRunRequest,
    TailoringRunListResponse,
    TailoringRunResponse,
    TailoringRunSummary,
)
from tailorcraft.infrastructure.rate_limit import (
    RateLimitDecision,
    RateLimiterUnavailable,
    RateLimitScope,
    client_ip,
)

log = structlog.get_logger(__name__)

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
# Boundary helpers. Pure functions over a loaded aggregate — no I/O, so they need no fixture to
# reason about.
# ---------------------------------------------------------------------------------------------


def _is_retryable(reason: TailoringFailureReason | None) -> bool:
    """Whether "Try again" is worth offering for a run that failed for `reason` (AC-13).

    **A business rule, and the API is its only authority** (Constitution §4.5): the React client
    branches on the boolean and never re-derives it. `llm_refused` and `inputs_too_large` are the two
    `False` answers because a second identical call answers identically — offering the button would
    be selling the same refusal twice. Every other reason is transient (the provider, the network,
    the broker, a dead worker, a malformed-but-possibly-better next completion) and a new run may
    well succeed.

    `None` — a run that has not failed — is `False`, which is not a claim that it is unretryable:
    a `queued`, `running` or `succeeded` run has nothing to retry.

    **A `match` closed by `assert_never`, not a set membership test.** `reason in {REFUSED,
    TOO_LARGE}` would compile today and silently answer `True` for a tenth reason added next month —
    which may well be one that must never be retried. Here that reason is a `mypy` error naming it,
    at the one line where somebody has to decide.
    """
    match reason:
        case None:
            return False
        case TailoringFailureReason.LLM_REFUSED | TailoringFailureReason.INPUTS_TOO_LARGE:
            return False
        case (
            TailoringFailureReason.LLM_UNAVAILABLE
            | TailoringFailureReason.LLM_RATE_LIMITED
            | TailoringFailureReason.LLM_TIMED_OUT
            | TailoringFailureReason.LLM_OUTPUT_INVALID
            | TailoringFailureReason.LLM_ERROR
            | TailoringFailureReason.NOT_QUEUED
            | TailoringFailureReason.ABANDONED
        ):
            return True
        case _:
            assert_never(reason)


def _to_response(run: TailoringRun, expires_at: datetime) -> TailoringRunResponse:
    """The full shape, including both documents. `expires_at` is the **session's** — the session
    owns the 24-hour promise (ADR-0006), exactly as `BaseCvResponse` and `JobPostingResponse` carry
    it."""
    documents = run.documents
    metrics = run.metrics
    return TailoringRunResponse(
        id=run.id.value,
        status=run.status,
        base_cv_id=run.base_cv_id.value,
        job_posting_id=run.job_posting_id.value,
        failure_reason=run.failure_reason,
        retryable=_is_retryable(run.failure_reason),
        tailored_cv=documents.cv.value if documents is not None else None,
        cover_letter=documents.cover_letter.value if documents is not None else None,
        tailored_cv_character_count=(
            documents.cv.character_count if documents is not None else None
        ),
        cover_letter_character_count=(
            documents.cover_letter.character_count if documents is not None else None
        ),
        model=metrics.model.value if metrics is not None else None,
        prompt_version=metrics.prompt_version.value if metrics is not None else None,
        llm_duration_ms=metrics.duration_ms if metrics is not None else None,
        requested_at=run.requested_at,
        started_at=run.started_at,
        completed_at=run.completed_at,
        expires_at=expires_at,
    )


def _to_summary(run: TailoringRun, expires_at: datetime) -> TailoringRunSummary:
    """The list shape: `_to_response` minus the two document bodies.

    Written out in full rather than derived from `_to_response` (e.g. by dumping and dropping two
    keys), for the reason `TailoringRunSummary`'s docstring gives for not subclassing: a derivation
    makes the *next* field added to the full shape appear in the list by default, and the direction
    that default leaks is a stranger's rewritten CV.
    """
    documents = run.documents
    metrics = run.metrics
    return TailoringRunSummary(
        id=run.id.value,
        status=run.status,
        base_cv_id=run.base_cv_id.value,
        job_posting_id=run.job_posting_id.value,
        failure_reason=run.failure_reason,
        retryable=_is_retryable(run.failure_reason),
        tailored_cv_character_count=(
            documents.cv.character_count if documents is not None else None
        ),
        cover_letter_character_count=(
            documents.cover_letter.character_count if documents is not None else None
        ),
        model=metrics.model.value if metrics is not None else None,
        prompt_version=metrics.prompt_version.value if metrics is not None else None,
        llm_duration_ms=metrics.duration_ms if metrics is not None else None,
        requested_at=run.requested_at,
        started_at=run.started_at,
        completed_at=run.completed_at,
        expires_at=expires_at,
    )


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


async def _record_not_queued(
    run_id: TailoringRunId,
    runs: TailoringRunRepository,
    events: EventPublisherPort,
    clock: Clock,
    db: AsyncSession,
) -> None:
    """G-14's **second transaction**: record a committed run the broker refused as `failed` /
    `not_queued`, so the client is not left polling a run that can never run.

    A separate transaction from the one that created the row, and it has to be: that one is already
    committed — deliberately, *before* the enqueue (ADR-0014 §5) — so there is nothing left to roll
    back into "no run". What remains is to tell the truth about the row that exists. `mark_failed`
    is legal from `queued` for exactly this case, and it leaves `started_at` `None`, because no
    worker ever began a call.

    **If this write fails too, the run stays `queued`, and that is the chosen survivor rather than a
    gap** — ADR-0006 §2's rule: of the crash windows available, pick the one whose survivor is
    recoverable. A `queued` run with no task is visible in the database, reads to the user as
    "Waiting for a worker…" (G-15's copy), and is recovered by re-enqueuing its id. Nothing is
    raised from here in that branch: the caller still answers 503 `queue_unavailable`, which is the
    true answer to what the user asked for regardless of which of the two states the row ended in.

    The event is published only **after** the commit, so a log line never announces a `failed` state
    that a rollback is about to un-happen.

    Known, accepted residual: `get` returns the instance already in this session's identity map
    (`expire_on_commit=False`), not a fresh read. The only way a worker could have touched the row
    in between is a publish that *reached* the broker and was still reported as refused (a timeout
    after the write) — rare enough that a `populate_existing` re-read on the broker-down path is not
    worth its own branch today.
    """
    try:
        run = await runs.get(run_id)
        run.mark_failed(TailoringFailureReason.NOT_QUEUED, clock.now())
        await runs.save(run)
        await db.commit()
    except SQLAlchemyError as exc:
        await db.rollback()
        # The run stays `queued` — the recoverable survivor. The id and the exception's type name
        # only: no message (a driver error can quote parameters) and, above all, no client IP.
        log.warning(
            "tailoring.not_queued_unrecorded",
            tailoring_run_id=str(run_id.value),
            error_type=type(exc).__name__,
        )
        return

    await events.publish(*run.release_events())


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
    # -- 1. The rate limiter, both scopes, FAIL-CLOSED (G-11, G-12, AC-17, AC-18). -----------------
    #
    # Before the use case, so a 429 or a 503 here creates no row and spends nothing (ADR-0014 §2:
    # before the enqueue, no rejection leaves anything to own). After body validation, which FastAPI
    # has already done, so a malformed request never consumes a counter.
    #
    # Both scopes are always checked, never short-circuited on the first — a client over its session
    # budget has still made an attempt from its IP, and that attempt counts (the same call 1.1's
    # upload handler makes).
    #
    # The limiter was built with `fail_open=False` (deps.py says why at length): an unreachable Redis
    # raises rather than waving the request through, because an endpoint that spends money with its
    # only backstop switched off is a funded denial-of-wallet. The limiter has already logged `scope`,
    # `namespace` and the error type — and never the identifier, which for the IP scope IS the client
    # address — so this branch adds no second line.
    ip_identifier = client_ip(request, settings.trusted_proxy_hops)
    checks: tuple[tuple[RateLimitScope, str, int], ...] = (
        ("session", str(session.id.value), settings.tailoring_rate_limit_per_hour),
        ("ip", ip_identifier, settings.tailoring_rate_limit_per_ip_per_hour),
    )
    decisions: list[tuple[RateLimitScope, RateLimitDecision]] = []
    try:
        for scope, identifier, limit in checks:
            decisions.append((scope, await rate_limiter.check(scope, identifier, limit)))
    except RateLimiterUnavailable:
        # `from None`: `RateLimiterUnavailable` is already raised `from None` inside the limiter for
        # the identifier's sake, and chaining it here would put this frame — whose locals hold the
        # client IP in `ip_identifier` — back within reach of a Sentry report.
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "rate_limit_unavailable",
                "message": "Tailoring is temporarily unavailable. Please try again shortly.",
            },
        ) from None

    denied = [(scope, decision) for scope, decision in decisions if not decision.allowed]
    if denied:
        for scope, _decision in denied:
            # `scope` and `namespace` only (G-11). No identifier, no run id — there is no run yet.
            log.info("rate_limit.exceeded", scope=scope, namespace=rate_limiter.namespace)
        retry_after = max(decision.retry_after_seconds for _scope, decision in denied)
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "code": "rate_limited",
                "message": f"Too many tailoring runs. Try again in {retry_after} seconds.",
            },
            headers={"Retry-After": str(retry_after)},
        )

    # -- 2. The use case (G-6 … G-10, AC-14, AC-15). ---------------------------------------------
    command = RequestTailoringRunCommand(
        guest_session_id=session.id,
        base_cv_id=BaseCvId(body.base_cv_id),
        job_posting_id=JobPostingId(body.job_posting_id),
    )
    try:
        result = await request_run(command)
    except DomainError as exc:
        # `TailoringAlreadyRunning`'s 409 carries `active_tailoring_run_id` — built in errors.py, so
        # the one mapping stays the one mapping.
        raise domain_error_to_http_exception(exc) from exc

    # The wire shape is built from the aggregate as the repository hands it back — the same
    # instance the use case just added, served from this session's identity map — rather than
    # assembled by hand from `RequestTailoringRunResult`, which deliberately carries only the id,
    # the status and the request time.
    run_id = result.tailoring_run_id
    saved = await runs.get(run_id)
    wire = _to_response(saved, session.expires_at)

    # -- 3. Commit HERE, inside this handler's own error boundary (G-13). -------------------------
    #
    # Not left to `get_session`'s teardown, and this is not belt-and-braces: FastAPI runs the exit
    # half of a yield-dependency AFTER the response is sent, so a commit failing there would fire
    # with the 202 already on the wire and a task possibly already published for a row that then
    # never existed. G-13 is only reachable because the commit is here.
    try:
        await db.commit()
    except SQLAlchemyError as exc:
        await db.rollback()
        # Nothing survives the rollback, and — the consequence of commit-then-enqueue — nothing was
        # enqueued either, so there is no task anywhere naming an id that does not exist.
        log.error("tailoring.request_not_committed", error_type=type(exc).__name__)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "service_unavailable",
                "message": "Something went wrong starting your tailoring run. Please try again.",
            },
        ) from exc

    # -- 4. THEN enqueue (ADR-0014 §5, AC-19). ----------------------------------------------------
    #
    # Commit-then-enqueue, never the other way round: the worker is a separate process picking up
    # tasks in milliseconds, so enqueue-then-commit is a race that fires under normal load — a task
    # reads an id whose row is not committed yet, returns MISSING, and the run sits `queued` forever.
    # This order instead leaves a crash window between two statements whose survivor is a `queued`
    # run with no task: visible, and recoverable by re-enqueuing.
    try:
        await queue.enqueue(run_id)
    except TailoringNotQueued as exc:
        # G-14. The adapter has already logged `tailoring.not_queued` with the run id and the error
        # type. The row is committed; record the truth about it in a second transaction, then answer
        # 503 whichever way that write went (see `_record_not_queued` for both branches).
        await _record_not_queued(run_id, runs, events, clock, db)
        raise domain_error_to_http_exception(exc) from exc

    # -- 5. Tell the client where to poll. --------------------------------------------------------
    response.headers["Location"] = f"{router.prefix}/{run_id.value}"
    return wire


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
    _no_store(response)
    try:
        runs = await list_use_case(session.id)
    except DomainError as exc:
        raise domain_error_to_http_exception(exc) from exc

    return TailoringRunListResponse(items=[_to_summary(run, session.expires_at) for run in runs])


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
    not exist", and the two are the same 404 because `GetTailoringRunForSession` raises the same type
    for both (G-29).

    `tailoring_run_id` is typed `UUID` so a malformed id is FastAPI's own 422 rather than something
    this handler rejects by hand (G-30).
    """
    _no_store(response)
    try:
        run = await get_use_case(TailoringRunId(tailoring_run_id), session.id)
    except DomainError as exc:
        raise domain_error_to_http_exception(exc) from exc

    return _to_response(run, session.expires_at)
