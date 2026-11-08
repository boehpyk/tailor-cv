"""The credential-agnostic bodies of the tailoring routes (slice 2.3, technical plan §0.2).

**Account twins must not become drifting copies** (2.2's R-6 lesson). So the body of each tailoring
handler that has a twin lives here, taking an already resolved `requester: Owner`, the `expires_at`
its responses carry, the rate-limit principal — the `(scope, identifier)` pair a per-principal budget
is keyed on (`"session"` and the session id for a guest) — and, where a `Location` is written, the
collection's URL prefix. The guest router (`routers/tailoring.py`) is then only its decorators, its
credential and a call. The contract each body implements is documented on the guest route.
`request_tailoring_run` is the exception since slice 5.2: account-only (the guest `POST` is a fixed
403), so it takes the `user_id` and nothing guest-shaped.

**Nothing here reads a credential**, so this module adds nothing to the AST scan's transfer-route
set, which is exactly `{POST /api/me/guest-work/claim}` since 2.4 (AC-25).

**What this module logs, and what it never logs** (AC-21, Constitution §8): ids, scopes, namespaces
and exception *type names*. Never a CV, a posting or a tailored document. **Never a client IP**: it
is computed once, handed to the limiter as an identifier, and goes nowhere else.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import assert_never

import structlog
from fastapi import Request, Response, status
from fastapi.exceptions import HTTPException
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from tailorcraft.application.tailoring.get_tailoring_run import GetTailoringRun
from tailorcraft.application.tailoring.record_run_not_queued import (
    RecordRunNotQueued,
    RecordRunNotQueuedCommand,
)
from tailorcraft.application.tailoring.request_tailoring_run import (
    RequestTailoringRun,
    RequestTailoringRunCommand,
)
from tailorcraft.application.tailoring.revise_tailored_document import (
    ReviseCoverLetterCommand,
    ReviseCvCommand,
    ReviseTailoredDocument,
    ReviseTailoredDocumentCommand,
)
from tailorcraft.domain.identity.ownership import GuestOwner, Owner, UserOwner
from tailorcraft.domain.identity.value_objects import UserId
from tailorcraft.domain.intake.value_objects import BaseCvId
from tailorcraft.domain.posting.value_objects import JobPostingId
from tailorcraft.domain.shared.errors import DomainError
from tailorcraft.domain.tailoring.errors import (
    TailoredDocumentVersionConflict,
    TailoringNotQueued,
    TailoringRunNotEditable,
)
from tailorcraft.domain.tailoring.history import TailoringHistoryEntry
from tailorcraft.domain.tailoring.ports import TailoringQueuePort, TailoringRunRepository
from tailorcraft.domain.tailoring.tailoring_run import TailoringRun
from tailorcraft.domain.tailoring.value_objects import (
    CoverLetter,
    TailoredCv,
    TailoredDocumentKind,
    TailoringFailureReason,
    TailoringRunId,
)
from tailorcraft.infrastructure.api.errors import (
    PROVIDER_BUSY_COOLDOWN,
    domain_error_to_http_exception,
)
from tailorcraft.infrastructure.api.schemas.tailoring import (
    CreateTailoringRunRequest,
    HistoryBaseCvResponse,
    HistoryEntryResponse,
    HistoryPostingResponse,
    ReviseDocumentRequest,
    TailoringRunResponse,
    TailoringRunSummary,
)
from tailorcraft.infrastructure.rate_limit import (
    RateLimitDecision,
    RateLimiterUnavailable,
    RateLimitScope,
    RedisFixedWindowRateLimiter,
    client_ip,
)
from tailorcraft.infrastructure.settings import Settings

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------------------------
# Boundary helpers. Pure functions over a loaded aggregate — no I/O, so they need no fixture to
# reason about.
# ---------------------------------------------------------------------------------------------


def is_retryable(reason: TailoringFailureReason | None) -> bool:
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
        case TailoringFailureReason.BASE_CV_DELETED:
            # H-22: no *Try again*. A new run needs the CV this one was made from, and that CV is
            # gone — a retry would 404 on it. The user picks another CV, which is a new request.
            return False
        case _:
            assert_never(reason)


def retry_not_before(
    reason: TailoringFailureReason | None, completed_at: datetime | None
) -> datetime | None:
    """When *Try again* is worth pressing: `completed_at + PROVIDER_BUSY_COOLDOWN` for a run that
    failed `llm_rate_limited`, else `None` (ADR-0014, amendment of 2026-10-09).

    `None` when `completed_at` is `None` too: a failed run always has one, but the type allows it.
    Every other reason is named, as in `is_retryable`, so an eleventh is a mypy error here.
    """
    match reason:
        case TailoringFailureReason.LLM_RATE_LIMITED:
            return None if completed_at is None else completed_at + PROVIDER_BUSY_COOLDOWN
        case (
            None
            | TailoringFailureReason.LLM_UNAVAILABLE
            | TailoringFailureReason.LLM_TIMED_OUT
            | TailoringFailureReason.LLM_REFUSED
            | TailoringFailureReason.LLM_OUTPUT_INVALID
            | TailoringFailureReason.LLM_ERROR
            | TailoringFailureReason.INPUTS_TOO_LARGE
            | TailoringFailureReason.NOT_QUEUED
            | TailoringFailureReason.ABANDONED
            | TailoringFailureReason.BASE_CV_DELETED
        ):
            return None
        case _:
            assert_never(reason)


def to_response(run: TailoringRun, expires_at: datetime | None) -> TailoringRunResponse:
    """The full shape, including both documents. `expires_at` is the **session's** — the session
    owns the 24-hour promise (ADR-0006), exactly as `BaseCvResponse` and `JobPostingResponse` carry
    it.

    **`tailored_cv` / `cover_letter` mean the CURRENT document** (ADR-0015 §4): the visitor's
    revision where one exists, else the model's draft — `run.current_documents`, never
    `run.documents`. The counts are of that same text. The draft is not served at all (OQ-3):
    nothing in this slice reads it, and serving two bodies per document would double the PII in
    every poll for a feature that is not built. `*_edited_at` is how a consumer tells which of the
    two it is reading — `null` means the draft, untouched.
    """
    documents = run.current_documents
    metrics = run.metrics
    return TailoringRunResponse(
        id=run.id.value,
        status=run.status,
        base_cv_id=run.base_cv_id.value,
        job_posting_id=run.job_posting_id.value,
        failure_reason=run.failure_reason,
        retryable=is_retryable(run.failure_reason),
        retry_not_before=retry_not_before(run.failure_reason, run.completed_at),
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
        version=run.version,
        tailored_cv_edited_at=run.cv_edited_at,
        cover_letter_edited_at=run.cover_letter_edited_at,
    )


def to_summary(run: TailoringRun, expires_at: datetime | None) -> TailoringRunSummary:
    """The list shape: `to_response` minus the two document bodies.

    Written out in full rather than derived from `to_response` (e.g. by dumping and dropping two
    keys), for the reason `TailoringRunSummary`'s docstring gives for not subclassing: a derivation
    makes the *next* field added to the full shape appear in the list by default, and the direction
    that default leaks is a stranger's rewritten CV.

    The two counts are of the **current** documents, as on the full shape (ADR-0015 §4), and
    `version` rides here too so the workspace's latest-run card and the run page agree on it
    without a second read.
    """
    documents = run.current_documents
    metrics = run.metrics
    return TailoringRunSummary(
        id=run.id.value,
        status=run.status,
        base_cv_id=run.base_cv_id.value,
        job_posting_id=run.job_posting_id.value,
        failure_reason=run.failure_reason,
        retryable=is_retryable(run.failure_reason),
        retry_not_before=retry_not_before(run.failure_reason, run.completed_at),
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
        version=run.version,
        tailored_cv_edited_at=run.cv_edited_at,
        cover_letter_edited_at=run.cover_letter_edited_at,
    )


def no_store(response: Response) -> None:
    """`Cache-Control: no-store` on both reads.

    1.2 established this for the posting text; here it matters more. A `TailoringRunResponse` carries
    a person's rewritten CV and a cover letter that names the employer they are applying to — the two
    together link an identifiable person to a specific job application. Without this header that body
    can land in a shared cache or an intermediary's store, which is a disclosure nobody chose.

    The list is `no-store` too, even though it carries no document body: it still enumerates which
    postings a named session is applying against, and how often.
    """
    response.headers["Cache-Control"] = "no-store"


async def record_not_queued(
    run_id: TailoringRunId, record: RecordRunNotQueued, db: AsyncSession
) -> None:
    """G-14's **second transaction**: a committed run the broker refused is recorded `failed` /
    `not_queued`, its credit comes back and `runs_failed` counts, in one commit (F-10). The rule lives
    in `RecordRunNotQueued`; this is only the transaction around it.

    The first transaction is already committed (commit-then-enqueue, ADR-0014 §5), so there is no "no
    run" to roll back to; what remains is to tell the truth about the row that exists.

    **If this write fails, the run stays `queued` and charged, and that is the chosen survivor**
    (ADR-0006 §2): visible, and the sweep's lost-queued list fails and refunds it (F-11). Nothing is
    raised: the caller answers 503 `queue_unavailable` either way. `except Exception` is the floor
    (a publish reported refused that actually reached a worker makes `mark_failed` refuse with a
    `DomainError`; a driver error outside SQLAlchemy's tree), never `BaseException`, so a cancelled
    request still cancels. The fully-qualified type only, never the message (a driver's can quote
    parameters), and no client IP.
    """
    try:
        await record(RecordRunNotQueuedCommand(tailoring_run_id=run_id))
        await db.commit()
    except Exception as exc:
        await db.rollback()
        log.warning(
            "tailoring.not_queued_unrecorded",
            tailoring_run_id=str(run_id.value),
            error_type=f"{type(exc).__module__}.{type(exc).__qualname__}",
        )


def _owner_log_fields(requester: Owner) -> dict[str, str]:
    """The requester as log fields — `guest_session_id` for a guest (the key these lines have always
    carried), `user_id` for a user. An id, never an email."""
    match requester:
        case GuestOwner(guest_session_id=guest_session_id):
            return {"guest_session_id": str(guest_session_id.value)}
        case UserOwner(user_id=user_id):
            return {"user_id": str(user_id.value)}
        case _:
            assert_never(requester)


# ---------------------------------------------------------------------------------------------
# Handler bodies
# ---------------------------------------------------------------------------------------------


async def request_tailoring_run(
    *,
    request: Request,
    response: Response,
    body: CreateTailoringRunRequest,
    user_id: UserId,
    location_prefix: str,
    settings: Settings,
    rate_limiter: RedisFixedWindowRateLimiter,
    request_run: RequestTailoringRun,
    runs: TailoringRunRepository,
    queue: TailoringQueuePort,
    record_not_queued_run: RecordRunNotQueued,
    db: AsyncSession,
    translate: Callable[[DomainError], HTTPException],
) -> TailoringRunResponse:
    """`POST /api/me/tailoring-runs`'s body: the fail-closed limiter (principal and IP), the use case
    (which charges the credit), the in-handler commit, **then** the enqueue, then
    `Location: {location_prefix}/{id}`. Account-only since slice 5.2: the guest `POST` is a fixed 403.

    **`no-store` on every answer, refusals included** (plan §4): a raised `HTTPException` loses the
    injected `Response`'s headers, so each refusal gets its own on the way out.

    `translate` maps a `DomainError` from the use case to a response; the account's cap messages speak
    to a signed-in user (`errors.account_error_to_http_exception`).
    """
    no_store(response)
    try:
        return await _request_tailoring_run(
            request=request,
            response=response,
            body=body,
            user_id=user_id,
            location_prefix=location_prefix,
            settings=settings,
            rate_limiter=rate_limiter,
            request_run=request_run,
            runs=runs,
            queue=queue,
            record_not_queued_run=record_not_queued_run,
            db=db,
            translate=translate,
        )
    except HTTPException as exc:
        exc.headers = {**(exc.headers or {}), "Cache-Control": "no-store"}
        raise


async def _request_tailoring_run(
    *,
    request: Request,
    response: Response,
    body: CreateTailoringRunRequest,
    user_id: UserId,
    location_prefix: str,
    settings: Settings,
    rate_limiter: RedisFixedWindowRateLimiter,
    request_run: RequestTailoringRun,
    runs: TailoringRunRepository,
    queue: TailoringQueuePort,
    record_not_queued_run: RecordRunNotQueued,
    db: AsyncSession,
    translate: Callable[[DomainError], HTTPException],
) -> TailoringRunResponse:
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
        ("user", str(user_id.value), settings.tailoring_rate_limit_per_hour),
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
        owner=UserOwner(user_id),
        base_cv_id=BaseCvId(body.base_cv_id),
        job_posting_id=JobPostingId(body.job_posting_id),
    )
    try:
        result = await request_run(command)
    except DomainError as exc:
        # `TailoringAlreadyRunning`'s 409 carries `active_tailoring_run_id` — built in errors.py, so
        # the one mapping stays the one mapping.
        raise translate(exc) from exc

    # The wire shape is built from the aggregate as the repository hands it back — the same
    # instance the use case just added, served from this session's identity map — rather than
    # assembled by hand from `RequestTailoringRunResult`, which deliberately carries only the id,
    # the status and the request time.
    run_id = result.tailoring_run_id
    saved = await runs.get(run_id)
    wire = to_response(saved, None)

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
        # 503 whichever way that write went (see `record_not_queued` for both branches).
        await record_not_queued(run_id, record_not_queued_run, db)
        raise domain_error_to_http_exception(exc) from exc

    # -- 5. Tell the client where to poll. --------------------------------------------------------
    response.headers["Location"] = f"{location_prefix}/{run_id.value}"
    return wire


async def get_tailoring_run(
    *,
    tailoring_run_id: TailoringRunId,
    response: Response,
    requester: Owner,
    expires_at: datetime | None,
    get_use_case: GetTailoringRun,
    translate: Callable[[DomainError], HTTPException] = domain_error_to_http_exception,
) -> TailoringRunResponse:
    """`GET /api/tailoring-runs/{id}`'s body — the endpoint a client polls."""
    no_store(response)
    try:
        run = await get_use_case(tailoring_run_id, requester)
    except DomainError as exc:
        raise translate(exc) from exc

    return to_response(run, expires_at)


async def revise_tailored_document(
    *,
    run_id: TailoringRunId,
    kind: TailoredDocumentKind,
    response: Response,
    body: ReviseDocumentRequest,
    requester: Owner,
    expires_at: datetime | None,
    principal: tuple[RateLimitScope, str],
    settings: Settings,
    rate_limiter: RedisFixedWindowRateLimiter,
    revise: ReviseTailoredDocument,
    db: AsyncSession,
) -> TailoringRunResponse:
    """`PUT …/documents/{kind}`'s body; the eight steps are documented on the guest route.

    `requester` and `expires_at` arrive as plain values, read off the credential **before** any
    I/O: a failed flush expires every instance the session holds, a loaded guest session included,
    and reading its id in an `except` branch would be a lazy load on a session that refuses one.
    """
    # -- 8, first. The header is a property of the route, not of the answer (see `no_store`). -----
    no_store(response)

    # The requester's log fields, computed from the frozen `Owner` value BEFORE any I/O — the
    # expiry trap this function's docstring names cannot reach a value object.
    owner_fields = _owner_log_fields(requester)

    # -- 4. The rate limiter, principal scope, FAIL-OPEN (E-30, E-31; AC-18). ---------------------
    #
    # After steps 1 to 3 (the body cap, the shape and the path — FastAPI's, already done by the time
    # this body runs), so a malformed request never consumes a counter; before the value object,
    # so a client past its budget cannot spend CPU on 20,000-character strings. Session scope only:
    # a save is always bound to a session that already owns the run, so a fresh session buys a
    # hammering script nothing — which is also why there is no `client_ip` call and no IP anywhere
    # in this handler.
    #
    # `fail_open=True` (deps.py): an unreachable Redis makes `check` answer "allowed" after logging
    # `rate_limit.unavailable` with `scope`, `namespace` and the error type — E-31's row exactly —
    # so unlike the POST above there is no `RateLimiterUnavailable` to catch and no 503 to answer.
    # The cost of an unlimited save is one bounded `UPDATE` of ours, not money.
    principal_scope, principal_id = principal
    decision = await rate_limiter.check(
        principal_scope, principal_id, settings.tailoring_revise_rate_limit_per_hour
    )
    if not decision.allowed:
        # `scope` and `namespace` only (E-30). No identifier; the run id is not logged either,
        # because nothing about the run has been read yet and the counter is per session.
        log.info("rate_limit.exceeded", scope=principal_scope, namespace=rate_limiter.namespace)
        retry_after = decision.retry_after_seconds
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "code": "rate_limited",
                "message": f"Too many saves. Try again in {retry_after} seconds.",
            },
            headers={"Retry-After": str(retry_after)},
        )

    # -- 5. The value object, constructed HERE (E-10 … E-12; AC-16). ----------------------------
    #
    # This is the validation boundary, so the text meets its bounds here and not in the use case.
    # `match kind` closed by `assert_never`: a third `TailoredDocumentKind` added without an arm is
    # a mypy error naming it, not a request that silently builds the wrong type. The `except`
    # arm reaches `domain_error_to_http_exception` like every other `DomainError` in this module
    # — the four value-object errors collapse there to one 422 `document_invalid` with a fixed
    # `problem` label, and a fixed `message`; neither carries the text.
    #
    # The log line carries `character_count` from `body.content` — the size, never the string
    # (E-10's row) — and the error's fully-qualified type name, which for these four is the
    # `problem` label spelled the domain's way.
    command: ReviseTailoredDocumentCommand
    try:
        match kind:
            case TailoredDocumentKind.CV:
                command = ReviseCvCommand(
                    tailoring_run_id=run_id,
                    requester=requester,
                    content=TailoredCv(body.content),
                    expected_version=body.expected_version,
                )
            case TailoredDocumentKind.COVER_LETTER:
                command = ReviseCoverLetterCommand(
                    tailoring_run_id=run_id,
                    requester=requester,
                    content=CoverLetter(body.content),
                    expected_version=body.expected_version,
                )
            case _:
                assert_never(kind)
    except DomainError as exc:
        log.info(
            "tailoring.document_rejected",
            tailoring_run_id=str(run_id.value),
            **owner_fields,
            kind=kind.value,
            character_count=len(body.content),
            error_type=type(exc).__name__,
        )
        # `from None`, not `from exc`: this frame's locals hold `body`, which holds the text the
        # value object just refused, and a chained exception keeps the frame reachable from a
        # Sentry report (CLAUDE.md on `include_local_variables`).
        raise domain_error_to_http_exception(exc) from None

    # -- 6. The use case (E-5 … E-9; AC-12, AC-13, AC-14). ---------------------------------------
    #
    # Four `DomainError`s can come out of it, and every one of them is translated by the shared
    # mapping so the one mapping stays the one mapping: `GuestSessionExpired` (401),
    # `TailoringRunNotFound` (404, for absent *and* not mine), `TailoringRunNotEditable` (409 with
    # `status`), `TailoredDocumentVersionConflict` (409 with `current_version`) — and, from the
    # repository's flush rather than the aggregate, `TailoringRunConcurrentlyModified` (409 with
    # `current_version: null`, E-9). That last one leaves the session in the failed state a failed
    # flush leaves it in; the rollback is `get_session`'s teardown, which runs on any exception —
    # and this `raise` is one. Nothing is written on any of the five: the first four raise before
    # the save, and the fifth is a save that matched no row.
    #
    # The log line carries the ids, `kind` and the numbers the failure names (`expected_version`
    # on every branch, `status` / `current_version` where the error has one) — E-7's, E-8's and
    # E-9's rows — and never the text, which lives only on `command`, a local this line does not
    # read.
    try:
        run = await revise(command)
    except DomainError as exc:
        log.info(
            "tailoring.document_not_revised",
            tailoring_run_id=str(run_id.value),
            **owner_fields,
            kind=kind.value,
            expected_version=body.expected_version,
            error_type=type(exc).__name__,
            **conflict_fields(exc),
        )
        raise domain_error_to_http_exception(exc) from None

    # The wire shape is built before the commit, as the POST builds its own: the aggregate as the
    # use case handed it back, served from this session's identity map.
    wire = to_response(run, expires_at)

    # -- 7. Commit HERE, inside this handler's own error boundary (E-14). ------------------------
    #
    # The same reasoning as the POST above: `get_session`'s teardown commits after the response is
    # on the wire, so a commit that fails there would fire with a 200 already sent for a row that
    # then rolled back. `error_type` only — a driver error's message quotes parameters, and the
    # parameters here are the CV (CLAUDE.md, "a failed database write carries its data out").
    try:
        await db.commit()
    except SQLAlchemyError as exc:
        await db.rollback()
        log.error(
            "tailoring.revision_not_committed",
            tailoring_run_id=str(run_id.value),
            kind=kind.value,
            error_type=type(exc).__name__,
        )
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "service_unavailable",
                "message": "Something went wrong saving your document. Please try again.",
            },
        ) from None

    # The success line (AC-19): ids, `kind`, the new `version`, the size and the outcome. The
    # character count is read off the value object the aggregate now holds — the same number
    # `TailoredDocumentRevised` carries — never the string it counts.
    log.info(
        "tailoring.document_revised",
        tailoring_run_id=str(run_id.value),
        **owner_fields,
        kind=kind.value,
        version=run.version,
        character_count=command.content.character_count,
        outcome="revised",
    )
    return wire


def conflict_fields(exc: DomainError) -> dict[str, str | int]:
    """The extra log fields a rejected revision's error names — E-7's `status`, E-8's
    `current_version` — and nothing for the errors that carry no number (E-5, E-6, E-9). Ids,
    enums and integers only; never text (AC-19)."""
    if isinstance(exc, TailoringRunNotEditable):
        return {"status": exc.status.value}
    if isinstance(exc, TailoredDocumentVersionConflict):
        return {"current_version": exc.current_version}
    return {}


def to_history_entry(entry: TailoringHistoryEntry) -> HistoryEntryResponse:
    """One history read-model entry on the wire (shared by `/api/me/` and `/api/admin`, 4.3 §0.5). `retryable` is the API's rule, as on the run."""
    base_cv = entry.base_cv
    posting = entry.posting
    return HistoryEntryResponse(
        id=entry.tailoring_run_id.value,
        status=entry.status,
        failure_reason=entry.failure_reason,
        retryable=is_retryable(entry.failure_reason),
        retry_not_before=retry_not_before(entry.failure_reason, entry.completed_at),
        requested_at=entry.requested_at,
        completed_at=entry.completed_at,
        version=entry.version,
        edited=entry.edited,
        base_cv_id=entry.base_cv_id.value,
        base_cv=(
            HistoryBaseCvResponse(
                id=base_cv.base_cv_id.value,
                label=base_cv.label,
                original_filename=base_cv.original_filename,
            )
            if base_cv is not None
            else None
        ),
        posting=(
            HistoryPostingResponse(
                id=posting.job_posting_id.value,
                source=posting.source,
                title=posting.title,
                source_url=posting.source_url,
                preview=posting.preview,
            )
            if posting is not None
            else None
        ),
    )
