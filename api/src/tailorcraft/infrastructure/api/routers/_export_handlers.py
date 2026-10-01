"""The credential-agnostic bodies of the export routes (slice 2.3, technical plan §0.2).

**Account twins must not become drifting copies** (2.2's R-6 lesson). So the body of every export
handler lives here, taking an already resolved `requester: Owner`, the `expires_at` its responses
carry, the rate-limit principal — the `(scope, identifier)` pair a per-principal budget is keyed on
(`"session"` and the session id for a guest) — and `jobs_prefix`, the job collection's URL, from
which both `Location` and `file_url` are built (`/api/export-jobs` for a guest). The guest router
(`routers/export.py`) is then only its decorators, its credential and a call. The contract each body
implements is documented on the guest route.

**Nothing here reads a credential**, so this module adds nothing to the AST scan's transfer-route
set, which is exactly `{POST /api/me/guest-work/claim}` since 2.4 (AC-25).
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

from tailorcraft.application.export.download_export_file import DownloadExportFile
from tailorcraft.application.export.get_export_job import GetExportJob
from tailorcraft.application.export.list_exports_for_run import ListExportsForRun
from tailorcraft.application.export.render_document_inline import (
    RenderDocumentInline,
    RenderDocumentInlineCommand,
)
from tailorcraft.application.export.request_export import RequestExport, RequestExportCommand
from tailorcraft.domain.export.errors import ExportNotQueued
from tailorcraft.domain.export.export_job import ExportJob
from tailorcraft.domain.export.ports import ExportJobRepository, ExportQueuePort
from tailorcraft.domain.export.value_objects import (
    ExportFailureReason,
    ExportFormat,
    ExportJobId,
    ExportJobStatus,
    download_filename,
)
from tailorcraft.domain.identity.ownership import Owner
from tailorcraft.domain.shared.clock import Clock
from tailorcraft.domain.shared.errors import DomainError
from tailorcraft.domain.shared.events import EventPublisherPort
from tailorcraft.domain.tailoring.value_objects import TailoredDocumentKind, TailoringRunId
from tailorcraft.infrastructure.api.errors import domain_error_to_http_exception
from tailorcraft.infrastructure.api.schemas.export import (
    CreateExportRequest,
    ExportJobListResponse,
    ExportJobResponse,
)
from tailorcraft.infrastructure.rate_limit import (
    RateLimitDecision,
    RateLimitScope,
    RedisFixedWindowRateLimiter,
    client_ip,
)
from tailorcraft.infrastructure.settings import Settings

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------------------------
# Boundary helpers. Pure functions over a loaded aggregate — no I/O, so they need no fixture to
# reason about — plus the one second-transaction coroutine X-22 needs.
# `routers/tailoring.py` carries the same three shapes one context over.
# ---------------------------------------------------------------------------------------------


def is_retryable(reason: ExportFailureReason | None) -> bool:
    """Whether *Export again* is worth offering for a job that failed for `reason` (AC-24).

    **A business rule, and the API is its only authority** (Constitution §4.5): the React client
    branches on the boolean and never re-derives it. `render_failed`, `output_too_large` and
    `source_unavailable` are the three `False` answers because the same input renders to the same
    refusal — offering the button would be selling that refusal twice. The other six are transient
    (a timeout, a store that was briefly unreadable, a broker that was down, a swept worker, a
    document that has since changed, an unnamed residual) and a new job may well succeed.

    `None` — a job that has not failed — is `False`, which is not a claim that it is unretryable:
    a `queued`, `rendering` or `ready` job has nothing to retry, and the client reads this field
    only when `status == "failed"`.

    **A `match` closed by `assert_never`, not a set membership test**, for the reason
    `_tailoring_handlers.is_retryable` gives at length: `reason in {…}` would compile today and
    silently answer `True` for a tenth reason added next month — which may well be one that must
    never be retried. Here that reason is a `mypy --strict` error naming the member, at the one line
    where somebody has to decide.
    """
    match reason:
        case None:
            return False
        case (
            ExportFailureReason.RENDER_FAILED
            | ExportFailureReason.OUTPUT_TOO_LARGE
            | ExportFailureReason.SOURCE_UNAVAILABLE
        ):
            return False
        case (
            ExportFailureReason.RENDER_TIMED_OUT
            | ExportFailureReason.FILE_STORE_UNAVAILABLE
            | ExportFailureReason.SOURCE_CHANGED
            | ExportFailureReason.NOT_QUEUED
            | ExportFailureReason.ABANDONED
            | ExportFailureReason.RENDER_ERROR
        ):
            return True
        case _:
            assert_never(reason)


def to_response(
    job: ExportJob, *, run_version_now: int | None, expires_at: datetime | None, jobs_prefix: str
) -> ExportJobResponse:
    """One `ExportJob` as the wire sees it — the body of the `POST`, of the poll, and of every row
    of the listing, so the client has one parser and one renderer.

    Built by keyword, never `model_validate(job)`: three of the sixteen fields exist on no aggregate
    (`ExportJobResponse`'s docstring says so, which is why it sets no `from_attributes`).

    **`current` is the cross-aggregate comparison, and this is the one place it is made.** Neither
    aggregate can answer it alone, which is why `GetExportJob` hands back
    `run_version_now` beside the job and `ListExportsForRun` hands back one `run_version` for the
    whole listing. `None` — the run is gone entirely — is `false`: a job whose source no longer
    exists is certainly not current. `was_requested_for` rather than `==` written out here, because
    the comparison is the aggregate's to define and this function only supplies the operand.

    **`file_url` is `null` until `ready`**, and it is a path: the client is same-origin. Serving it
    before the bytes exist would be an invitation to a 409, and building it in TypeScript would put
    the URL grammar in two places.

    `expires_at` is the **guest session's** (ADR-0006) — the session owns the 24-hour promise, and
    `BaseCvResponse`, `JobPostingResponse` and `TailoringRunResponse` all carry it the same way.

    `jobs_prefix` is the job collection's URL (`/api/export-jobs` for a guest) — the one place the
    download URL is built, so a twin's jobs link to its own `…/file`.
    """
    return ExportJobResponse(
        id=job.id.value,
        tailoring_run_id=job.tailoring_run_id.value,
        document=job.document,
        format=job.format,
        status=job.status,
        failure_reason=job.failure_reason,
        retryable=is_retryable(job.failure_reason),
        run_version=job.run_version,
        current=run_version_now is not None and job.was_requested_for(run_version_now),
        byte_size=job.byte_size,
        render_duration_ms=job.render_duration_ms,
        file_url=(
            f"{jobs_prefix}/{job.id.value}/file" if job.status is ExportJobStatus.READY else None
        ),
        requested_at=job.requested_at,
        started_at=job.started_at,
        completed_at=job.completed_at,
        expires_at=expires_at,
    )


def no_store(response: Response) -> None:
    """`Cache-Control: no-store` on the two reads that answer a *model*.

    The two binary routes do **not** use this: they build their own `Response` and set the header on
    it, because an injected sub-response's headers are merged only when the endpoint returns a model
    (`download_document_inline`'s docstring has the measurement). Sharing one helper across both
    shapes would be the trap, not the tidiness.

    An `ExportJobResponse` carries no document text, but it still enumerates what a named session is
    preparing and when — and an intermediary caching it would hand the next visitor on a shared
    address a list of somebody's job applications. 1.2 established the header; 1.4 carried it.
    """
    response.headers["Cache-Control"] = "no-store"


def download_headers(document: TailoredDocumentKind, format: ExportFormat) -> dict[str, str]:
    """The three headers both binary routes set, in the order they are written here (AC-8, AC-25).

    **`no-store` and `nosniff` come first, before the filename** — literally, in this dict — because
    they are the two that must never be lost to an edit that reorders the ones around them. The body
    below them is a stranger's employment history: without `no-store` it can land in a shared cache,
    and without `nosniff` a browser may re-decide the type of a document it was handed as an
    attachment.

    **`Content-Disposition` comes from the domain's `download_filename`, which is a constant keyed
    on (document, format) and never touches the user's text** (AC-27). That is a rule rather than a
    formatting choice: a tailored CV's first line is whatever the model wrote from whatever the user
    uploaded, and a header assembled from it is a header an attacker gets to write —
    `'"; filename="evil.exe'` as the document's opening line is exactly the fixture AC-27 exports.
    Nothing here interpolates anything but that constant, so there is no injection to escape.

    `Content-Type` is not set here: it rides as the `Response`'s `media_type`, from
    `ExportFormat.media_type`, so Starlette owns the one header it also owns the charset rule for.
    `Content-Length` is Starlette's too, computed from the bytes it is handed rather than from a
    number we claim.
    """
    return {
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
        "Content-Disposition": f'attachment; filename="{download_filename(document, format)}"',
    }


async def record_not_queued(
    job_id: ExportJobId,
    jobs: ExportJobRepository,
    events: EventPublisherPort,
    clock: Clock,
    db: AsyncSession,
) -> None:
    """X-22's **second transaction**: record a committed job the broker refused as `failed` /
    `not_queued`, so the client is not left polling a job that can never run.

    `_tailoring_handlers.record_not_queued`'s shape, copied deliberately rather than generalised
    into a shared helper over two aggregates — the two differ in the enum they write and in nothing
    else today, and CLAUDE.md's rule is that shared shape is not shared behaviour.

    A separate transaction from the one that created the row, and it has to be: that one is already
    committed — on purpose, *before* the enqueue (ADR-0014 §5) — so there is nothing left to roll
    back into "no job". What remains is to tell the truth about the row that exists. `mark_failed`
    is legal from `queued` for exactly this case (`ExportNotRendering` is deliberately not applied
    to it), and it leaves `started_at` `None`, because no worker ever began a render.

    **If this write fails too, the job stays `queued`, and that is the chosen survivor rather than a
    gap** — ADR-0006 §2's rule: of the crash windows available, pick the one whose survivor is
    recoverable. A `queued` job with no task is visible in the database, reads to the user as a
    render still waiting for a worker, and is either re-enqueued by hand or swept. Nothing is raised
    from here in that branch: the caller answers 503 `queue_unavailable` either way, which is the
    true answer to what the user asked for whichever of the two states the row ended in.

    The event is published only **after** the commit, so a log line never announces a `failed` state
    that a rollback is about to un-happen.
    """
    try:
        job = await jobs.get(job_id)
        job.mark_failed(ExportFailureReason.NOT_QUEUED, clock.now())
        await jobs.save(job)
        await db.commit()
    except SQLAlchemyError as exc:
        await db.rollback()
        # The job stays `queued` — the recoverable survivor. The id and the exception's type name
        # only: no message (a driver error can quote bound parameters) and no client address.
        log.warning(
            "export.not_queued_unrecorded",
            export_job_id=str(job_id.value),
            error_type=type(exc).__name__,
        )
        return
    except Exception as exc:
        # **THE FLOOR**, and it has a concrete scenario rather than a hypothetical one, the same one
        # `routers/tailoring.py` met: the publish the queue reported as refused actually *reached*
        # the broker (a timeout after the write), a worker picked the task up, and by the time
        # `mark_failed` runs here the job is already decided — so the aggregate refuses the
        # transition with a `DomainError`, which is not a `SQLAlchemyError`. Without this branch that
        # escapes as a bare 500 and breaks X-22's promise that every way this second write can fail
        # still answers 503 `queue_unavailable`. `ExportJobConcurrentlyModified` from `save` lands
        # here too, and so does a driver error outside SQLAlchemy's own tree.
        #
        # The fully-qualified TYPE, never `str(exc)` (a domain error is harmless, a driver's is not,
        # and this branch cannot tell which it holds) and never `exc_info`. Nothing is re-raised, so
        # there is no chain to cut. `Exception`, never `BaseException`: a cancelled request must
        # still cancel.
        await db.rollback()
        log.warning(
            "export.not_queued_unrecorded",
            export_job_id=str(job_id.value),
            error_type=f"{type(exc).__module__}.{type(exc).__qualname__}",
        )
        return

    await events.publish(*job.release_events())


# ---------------------------------------------------------------------------------------------
# Handler bodies
# ---------------------------------------------------------------------------------------------


async def download_document_inline(
    *,
    run_id: TailoringRunId,
    kind: TailoredDocumentKind,
    format: str,
    requester: Owner,
    render_inline: RenderDocumentInline,
) -> Response:
    """The inline `md`/`txt` download's body. `format` is the route's inline literal, widened to
    `ExportFormat` here, once. Headers go on the returned `Response` (see `download_headers`)."""
    try:
        rendered = await render_inline(
            RenderDocumentInlineCommand(
                requester=requester,
                tailoring_run_id=run_id,
                document=kind,
                # The query parameter is the inline literal; widening it back to the domain enum
                # happens **here, once** (the schema module's note), so nothing downstream has to
                # know that this endpoint's type is a subset. `RenderDocumentInline` re-checks
                # `format.delivery` anyway — that is the lock a non-HTTP caller cannot route around.
                format=ExportFormat(format),
            )
        )
    except DomainError as exc:
        # `TailoringRunNotExportable` -> 409 with `status` (X-4); `DocumentRenderTimedOut` -> 503
        # (X-6); every other `DocumentRenderFailed` -> the deliberate 500 (X-5). All of it in
        # `errors.py`, so the one mapping stays the one mapping.
        #
        # **`SQLAlchemyError` is deliberately not caught here** (X-7): it is not a `DomainError`, so
        # it passes straight through to the app-level handler that answers 503 `service_unavailable`
        # without this frame — whose locals would otherwise hold the document — in the chain.
        raise domain_error_to_http_exception(exc) from exc

    return Response(
        content=rendered.data,
        media_type=rendered.format.media_type,
        headers=download_headers(rendered.document, rendered.format),
    )


async def request_export(
    *,
    run_id: TailoringRunId,
    request: Request,
    response: Response,
    body: CreateExportRequest,
    requester: Owner,
    expires_at: datetime | None,
    principal: tuple[RateLimitScope, str],
    jobs_prefix: str,
    settings: Settings,
    rate_limiter: RedisFixedWindowRateLimiter,
    request_export_job: RequestExport,
    jobs: ExportJobRepository,
    queue: ExportQueuePort,
    events: EventPublisherPort,
    clock: Clock,
    db: AsyncSession,
    translate: Callable[[DomainError], HTTPException] = domain_error_to_http_exception,
) -> ExportJobResponse:
    """`POST …/exports`'s body: the fail-open limiter (principal and IP), the use case, the commit,
    then the enqueue for a created job, then `Location: {jobs_prefix}/{id}` and 202 or 200.

    `translate` maps a `DomainError` from the use case to a response; the account twin passes one
    whose cap messages speak to a signed-in user (`errors.account_error_to_http_exception`).
    """
    # -- 1. The rate limiter, both scopes, FAIL-OPEN (X-19, X-20). --------------------------------
    #
    # **Open, where `request_tailoring_run`'s twin is closed**, and the difference is the invoice
    # (1.1's OQ-7 rule). A tailoring run spends money at a third party, so its limiter refuses the
    # request when it cannot count — an endpoint that spends money with its only backstop switched
    # off is a funded denial-of-wallet. An export spends worker seconds and disk of *ours*, both
    # already bounded by `max_export_jobs_per_session`, the 20 MiB output cap and the 24-hour purge.
    # With no invoice on the other side, refusing a legitimate export because Redis blinked costs
    # the visitor their download and costs an attacker nothing.
    #
    # So there is deliberately **no `RateLimiterUnavailable` branch here**, and its absence is the
    # decision rather than an omission: `deps.get_export_rate_limiter` constructs this limiter with
    # `fail_open=True`, so `check` returns `allowed=True` and logs one warning (scope, namespace and
    # the error type — never the identifier, which for the IP scope IS the client's address) instead
    # of raising. A `try/except` for the exception it cannot raise would be dead code that also reads
    # as though this endpoint might answer 503 `rate_limit_unavailable`, which it never does.
    #
    # Before the use case, so a 429 creates no row and spends nothing (ADR-0014 §2). After FastAPI's
    # own body validation, so a malformed request never consumes a counter. Both scopes are always
    # checked, never short-circuited on the first: a client over its session budget has still made
    # an attempt from its IP, and that attempt counts.
    principal_scope, principal_id = principal
    checks: tuple[tuple[RateLimitScope, str, int], ...] = (
        (principal_scope, principal_id, settings.export_rate_limit_per_hour),
        (
            "ip",
            client_ip(request, settings.trusted_proxy_hops),
            settings.export_rate_limit_per_ip_per_hour,
        ),
    )
    decisions: list[tuple[RateLimitScope, RateLimitDecision]] = [
        (scope, await rate_limiter.check(scope, identifier, limit))
        for scope, identifier, limit in checks
    ]
    denied = [(scope, decision) for scope, decision in decisions if not decision.allowed]
    if denied:
        for scope, _decision in denied:
            # `scope` and `namespace` only. No identifier, and no job id — there is no job yet.
            log.info("rate_limit.exceeded", scope=scope, namespace=rate_limiter.namespace)
        retry_after = max(decision.retry_after_seconds for _scope, decision in denied)
        # **Seconds, and X-19's "Too many exports — try again in N minutes" is a different string.**
        # /verify flagged the two as disagreeing; the seconds won, and here is why rather than a
        # rewording. `message` is never shown to anyone: `exportCopy.ts` keys its sentences on
        # `code` on purpose ("`code` is the contract, `message` is prose for a human and can be
        # reworded without notice"), so the minutes in the spec's *User sees* column are the
        # frontend's copy to write, exactly as G-11's are in `apiErrorCopy.ts`. What this string
        # does have to agree with is `Retry-After`, which HTTP defines in seconds and which is the
        # only number a client can act on — and with the four sibling routers, which all say
        # seconds. Rounding it to minutes here would put a second unit on one fact.
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "code": "rate_limited",
                "message": f"Too many exports. Try again in {retry_after} seconds.",
            },
            headers={"Retry-After": str(retry_after)},
        )

    # -- 2. The use case (X-13, X-14, X-15, X-16, X-18). ------------------------------------------
    try:
        result = await request_export_job(
            RequestExportCommand(
                requester=requester,
                tailoring_run_id=run_id,
                document=body.document,
                format=ExportFormat(body.format),
            )
        )
    except DomainError as exc:
        raise translate(exc) from exc

    job = result.export_job
    # **Read the id into a local, and build the wire shape, BEFORE the commit** (1.4's lesson,
    # CLAUDE.md): a failed flush expires the whole identity map *inside* the flush, so every
    # attribute read after it is a lazy load, which on an `AsyncSession` is a `MissingGreenlet` —
    # and X-21's 503 would arrive as a 500 from the error path itself.
    job_id = job.id
    # `run_version_now=job.run_version`, and the tautology is the honest shape rather than a shortcut.
    # Both branches of `RequestExport` guarantee the equality: step 3 hands back an existing job only
    # if `was_requested_for(run.version)` held, and step 5 reads `run_version` off the run it just
    # authorized. `current` is therefore `true` on every 202 and every 200 this handler can send.
    # The alternative — a second read of the run here purely to compare it with itself — would buy a
    # primary-key lookup per export and could only ever disagree by racing the revision endpoint,
    # in which case the *poll* (which does compare against a fresh read) is the one telling the truth.
    wire = to_response(
        job, run_version_now=job.run_version, expires_at=expires_at, jobs_prefix=jobs_prefix
    )

    # -- 3. Commit HERE, inside this handler's own error boundary (X-21). -------------------------
    #
    # Not left to `get_session`'s teardown: FastAPI runs the exit half of a yield-dependency AFTER
    # the response is sent, so a commit failing there would fire with the 202 already on the wire and
    # a task already published for a row that then never existed. X-21 is only reachable because the
    # commit is here.
    try:
        await db.commit()
    except SQLAlchemyError as exc:
        await db.rollback()
        # Nothing survives the rollback and — the point of commit-then-enqueue — nothing was
        # enqueued either, so no task anywhere names an id with no row. The type only, never the
        # message: a driver error quotes bound parameters.
        log.error("export.request_not_committed", error_type=type(exc).__name__)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "service_unavailable",
                "message": "Something went wrong starting your export. Please try again.",
            },
        ) from exc

    # -- 4. THEN enqueue, and only for a job this request created (ADR-0014 §5, X-16, X-22). ------
    #
    # Commit-then-enqueue, never the other way round: the worker is a separate process picking tasks
    # up in milliseconds, so enqueue-then-commit is a race that fires under ordinary load — the task
    # reads an id whose row is not committed, returns MISSING, and the job sits `queued` for ever.
    # This order instead leaves a window whose survivor is a `queued` job with no task: visible, and
    # recoverable by re-enqueuing.
    #
    # `result.created` is the flag, and it exists because the router cannot re-derive it — a `queued`
    # job looks identical whether this request made it or found it. A second publish for a job
    # already in flight would be a second render and a second file for one click.
    if result.created:
        try:
            await queue.enqueue(job_id)
        except ExportNotQueued as exc:
            # X-22. The row is committed; record the truth about it in a second transaction, then
            # answer 503 whichever way that write went (`record_not_queued` covers both branches).
            await record_not_queued(job_id, jobs, events, clock, db)
            raise domain_error_to_http_exception(exc) from exc

    # -- 5. Tell the client where to poll, and which of the two answers this is. -------------------
    #
    # `Location` on **both** branches: the question "where do I poll for this?" has the same answer
    # whether the job was created now or found already running.
    #
    # The declared status code is the 202; the 200 branch sets `response.status_code` itself, which
    # FastAPI honours precisely because this handler returns a *model*. The two binary routes in
    # this file cannot do that and say so at length — see `download_document_inline`.
    response.headers["Location"] = f"{jobs_prefix}/{job_id.value}"
    if not result.created:
        response.status_code = status.HTTP_200_OK
    return wire


async def list_exports_for_run(
    *,
    run_id: TailoringRunId,
    response: Response,
    requester: Owner,
    expires_at: datetime | None,
    jobs_prefix: str,
    list_exports: ListExportsForRun,
) -> ExportJobListResponse:
    """`GET …/exports`'s body."""
    no_store(response)
    try:
        listing = await list_exports(run_id, requester)
    except DomainError as exc:
        raise domain_error_to_http_exception(exc) from exc

    # One `run_version` for every row: the listing is a single instant's answer, and judging half
    # the rows against one fact and half against another read a moment later is how a client ends
    # up rendering *Download* beside *Your document changed*.
    return ExportJobListResponse(
        items=[
            to_response(
                job,
                run_version_now=listing.run_version,
                expires_at=expires_at,
                jobs_prefix=jobs_prefix,
            )
            for job in listing.jobs
        ]
    )


async def get_export_job(
    *,
    export_job_id: ExportJobId,
    response: Response,
    requester: Owner,
    expires_at: datetime | None,
    jobs_prefix: str,
    get_job: GetExportJob,
) -> ExportJobResponse:
    """The poll's body."""
    no_store(response)
    try:
        lookup = await get_job(export_job_id, requester)
    except DomainError as exc:
        raise domain_error_to_http_exception(exc) from exc

    # A `failed` job is a **200** with `status` and `failure_reason` (AC-19), never a 5xx: the render
    # reached a worker and the worker recorded an outcome, which is a state of this resource. A 5xx
    # would make a poller retry a decision that is already final.
    return to_response(
        lookup.job,
        run_version_now=lookup.run_version_now,
        expires_at=expires_at,
        jobs_prefix=jobs_prefix,
    )


async def download_export_file(
    *,
    export_job_id: ExportJobId,
    requester: Owner,
    download: DownloadExportFile,
) -> Response:
    """The file download's body. Headers go on the returned `Response` (see `download_headers`)."""
    try:
        job, data = await download(export_job_id, requester)
    except DomainError as exc:
        # `ExportJobNotFound` -> 404 (X-43); `ExportNotReady` -> 409 carrying `status` and, when it
        # failed, `failure_reason` (X-44, X-45); `StoredFileMissing` -> **410** and
        # `FileStoreUnavailable` -> 503 `storage_unavailable` (X-47, X-48) — the narrow one pinned
        # above the wide one in `errors.py`, where the comment between them explains why the order
        # is the branch. A stale `ready` job is not an error at all and never reaches here (X-46):
        # the file is the user's own, and the UI is what declines to offer it.
        raise domain_error_to_http_exception(exc) from exc

    return Response(
        content=data,
        media_type=job.format.media_type,
        headers=download_headers(job.document, job.format),
    )
