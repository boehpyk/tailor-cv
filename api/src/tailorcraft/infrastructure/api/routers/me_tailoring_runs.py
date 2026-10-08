"""A signed-in user's tailoring runs — the history collection — and everything that hangs off one
(slice 2.3, technical plan §4, ADR-0023, ADR-0024).

Built red-first (sdlc.md §2): **SKELETON** (T20 — real paths, real schemas, every documented status
in each `responses=` map), **RED** (T21, `qa`), **GREEN** (T23, this file — thin calls into
`_tailoring_handlers.py` and `_export_handlers.py`, T19's shared bodies, with the account's principal
and `expires_at: null`; the history list and `DELETE` are this module's own).

**`/api/me/tailoring-runs` is the history** (plan §4): the resource *is* the user's runs; "history"
is the page that lists them. One collection, one noun. Its list is a different shape from the guest
list (`HistoryPageResponse`, keyset-paged, with the posting and the CV a user needs to recognise an
entry) because the guest list is 1.3's, for a 24-hour workspace.

**One credential: the bearer** (`require_user`); a `__Host-tc_guest` cookie is ignored and nothing
here reads it. There is no transfer route: an account run's inputs are all account data (plan
§0.1(a)), so the AST scan's exception set gains nothing here; since 2.4 it is exactly
`{POST /api/me/guest-work/claim}` (AC-25). A guest-owned id is a 404 here,
byte-identical to one that does not exist, as a user-owned id is on every guest route.

**Why this router also carries the run's download and export routes**, where the guest surface
splits them into `routers/export.py`: the prefix is the resource's owner. Everything under
`/api/me/tailoring-runs/{id}/…` answers to the same bearer and the same run, so it is one router;
the job resource, once it exists, is `routers/me_export_jobs.py`, as the guest one is.

Every response carries `Cache-Control: no-store`, and every `expires_at` is `null` (OQ-8).
"""

from __future__ import annotations

from functools import partial
from typing import Annotated, Final, Literal
from uuid import UUID

import structlog
from fastapi import APIRouter, Body, Query, Request, Response, status
from fastapi.exceptions import HTTPException
from sqlalchemy.exc import SQLAlchemyError

from tailorcraft.domain.identity.ownership import UserOwner
from tailorcraft.domain.identity.value_objects import UserId
from tailorcraft.domain.retention.value_objects import HistoryEntryErasureReport
from tailorcraft.domain.shared.errors import DomainError
from tailorcraft.domain.tailoring.history import HistoryPageSize, TailoringHistoryEntry
from tailorcraft.domain.tailoring.value_objects import TailoredDocumentKind, TailoringRunId
from tailorcraft.infrastructure.api.deps import (
    ClockDep,
    EraseHistoryEntryDep,
    EventPublisherDep,
    ExportJobRepositoryDep,
    ExportQueueDep,
    ExportRateLimiterDep,
    GetTailoringRunDep,
    ListExportsForRunDep,
    ListTailoringHistoryDep,
    RenderDocumentInlineDep,
    RequestExportDep,
    RequestTailoringRunDep,
    RequireUserDep,
    ReviseTailoredDocumentDep,
    SessionDep,
    SettingsDep,
    TailoringQueueDep,
    TailoringRateLimiterDep,
    TailoringReviseRateLimiterDep,
    TailoringRunRepositoryDep,
)
from tailorcraft.infrastructure.api.errors import (
    account_error_to_http_exception,
    domain_error_to_http_exception,
)
from tailorcraft.infrastructure.api.routers import _export_handlers as export_handlers
from tailorcraft.infrastructure.api.routers import _tailoring_handlers as handlers
from tailorcraft.infrastructure.api.routers._history_cursor import decode_cursor, encode_cursor
from tailorcraft.infrastructure.api.routers._me_responses import (
    NOT_SIGNED_IN,
    RATE_LIMITED,
    REQUEST_TOO_LARGE,
    SERVICE_UNAVAILABLE,
    TAILORING_RUN_NOT_FOUND,
    VALIDATION_ERROR,
)
from tailorcraft.infrastructure.api.schemas.export import (
    CreateExportRequest,
    ExportJobListResponse,
    ExportJobResponse,
)
from tailorcraft.infrastructure.api.schemas.intake import ErrorResponse
from tailorcraft.infrastructure.api.schemas.tailoring import (
    CreateTailoringRunRequest,
    HistoryBaseCvResponse,
    HistoryEntryResponse,
    HistoryPageResponse,
    HistoryPostingResponse,
    ReviseDocumentRequest,
    TailoringRunResponse,
)

log = structlog.get_logger(__name__)

EVENT_HISTORY_ENTRY_ERASED: Final = "retention.history_entry_erased"
EVENT_HISTORY_ENTRY_FILE_UNLINK_FAILED: Final = "retention.history_entry_file_unlink_failed"

router = APIRouter(prefix="/api/me/tailoring-runs", tags=["tailoring"])

# Where a user's export jobs live (`routers/me_export_jobs.py`): the `Location` and `file_url` this
# router's export bodies build.
_JOBS_PREFIX: Final = "/api/me/export-jobs"

# A cursor is `base64url("<epoch>.<uuid>")` — about 70 characters. Anything far longer is not one of
# ours, and FastAPI refuses it as `validation_error` before the codec is asked (plan §0.5).
_CURSOR_MAX_LENGTH = 128


def _no_store(response: Response) -> None:
    """`Cache-Control: no-store` on every model-returning route (plan §4, AC-51). The two binary
    routes set it on the `Response` they build (`_export_handlers.download_headers`)."""
    response.headers["Cache-Control"] = "no-store"


def _refusal_no_store(exc: DomainError, *, max_job_postings_per_user: int) -> HTTPException:
    """The account translation, plus `no-store` on the refusal: the handler raises it, so it is a
    response the handler builds (slice 3.2 plan §4, as 3.1's `_refuse` and 2.4's claim do)."""
    refusal = account_error_to_http_exception(
        exc, max_job_postings_per_user=max_job_postings_per_user
    )
    refusal.headers = {**(refusal.headers or {}), "Cache-Control": "no-store"}
    return refusal


def _principal(user_id: UserId) -> tuple[Literal["user"], str]:
    """The per-principal rate-limit key for an account: every budget is the user's (plan §3)."""
    return ("user", str(user_id.value))


def _to_history_entry(entry: TailoringHistoryEntry) -> HistoryEntryResponse:
    """One read-model entry on the wire. `retryable` is the API's rule, as on the run."""
    base_cv = entry.base_cv
    posting = entry.posting
    return HistoryEntryResponse(
        id=entry.tailoring_run_id.value,
        status=entry.status,
        failure_reason=entry.failure_reason,
        retryable=handlers.is_retryable(entry.failure_reason),
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


def _log_entry_erasure(
    user_id: UserId, run_id: TailoringRunId, report: HistoryEntryErasureReport
) -> None:
    """One `retention.history_entry_erased` line with the counts, and one
    `retention.history_entry_file_unlink_failed` per failed unlink — ids, counts and class names,
    never a storage key or a path. `EraseHistoryEntry` returns its failures rather than logging them
    (the application layer does not log); `routers/auth.py::_log_erasure` is the same shape."""
    log.info(
        EVENT_HISTORY_ENTRY_ERASED,
        user_id=str(user_id.value),
        tailoring_run_id=str(run_id.value),
        export_jobs=report.export_jobs,
        files_unlinked=report.files_unlinked,
        files_failed=len(report.unlink_failures),
        posting_deleted=report.posting_deleted,
        # Slice 3.1 (T-32, AC-24): whether the entry's card went with it. A bool.
        tracked_application_deleted=report.tracked_application_deleted,
    )
    for error_type in report.unlink_failures:
        log.warning(
            EVENT_HISTORY_ENTRY_FILE_UNLINK_FAILED,
            tailoring_run_id=str(run_id.value),
            error_type=error_type,
        )


@router.post(
    "",
    response_model=TailoringRunResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        status.HTTP_202_ACCEPTED: {
            "description": (
                "Accepted for processing: the run is committed `queued` and handed to a worker. "
                "Poll the `Location` URL. `expires_at: null`."
            ),
            "headers": {
                "Location": {
                    "description": "`/api/me/tailoring-runs/{id}` — the run to poll.",
                    "schema": {"type": "string"},
                },
            },
        },
        status.HTTP_404_NOT_FOUND: {
            "model": ErrorResponse,
            "description": (
                "base_cv_not_found | job_posting_not_found — also for another user's or a guest's "
                "object, byte-identical to a nonexistent id."
            ),
        },
        status.HTTP_409_CONFLICT: {
            "model": ErrorResponse,
            "description": (
                "base_cv_not_extracted | tailoring_already_running (+ `active_tailoring_run_id`) "
                "| too_many_tailoring_runs"
            ),
        },
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ErrorResponse,
            "description": (
                "rate_limit_unavailable (the tailoring limiter fails closed — this endpoint "
                "spends money) | queue_unavailable (the row is committed and recorded "
                "`failed`/`not_queued` first) | service_unavailable"
            ),
        },
        **REQUEST_TOO_LARGE,
        **VALIDATION_ERROR,
        **RATE_LIMITED,
        **NOT_SIGNED_IN,
    },
)
async def request_my_tailoring_run(
    request: Request,
    response: Response,
    body: Annotated[CreateTailoringRunRequest, Body()],
    user_id: RequireUserDep,
    settings: SettingsDep,
    clock: ClockDep,
    rate_limiter: TailoringRateLimiterDep,
    request_run: RequestTailoringRunDep,
    runs: TailoringRunRepositoryDep,
    queue: TailoringQueueDep,
    events: EventPublisherDep,
    db: SessionDep,
) -> TailoringRunResponse:
    """Request one tailoring run from the account's own saved CV and posting — 1.3's contract, the
    principal `("user", <id>)` plus the client IP on the fail-closed budget, checked before the use
    case; commit, then enqueue (the shared body)."""
    _no_store(response)
    return await handlers.request_tailoring_run(
        request=request,
        response=response,
        body=body,
        requester=UserOwner(user_id),
        expires_at=None,
        principal=_principal(user_id),
        location_prefix=router.prefix,
        settings=settings,
        clock=clock,
        rate_limiter=rate_limiter,
        request_run=request_run,
        runs=runs,
        queue=queue,
        events=events,
        db=db,
        translate=partial(
            account_error_to_http_exception,
            max_job_postings_per_user=settings.max_job_postings_per_user,
        ),
    )


@router.get(
    "",
    response_model=HistoryPageResponse,
    responses={
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ErrorResponse,
            "description": (
                "validation_error — `limit` outside 1…50 (H-27) | invalid_cursor — a malformed, "
                "tampered or wrong-shape cursor (H-26); the cursor is never echoed or logged."
            ),
        },
        **NOT_SIGNED_IN,
        **SERVICE_UNAVAILABLE,
    },
)
async def list_my_tailoring_history(
    response: Response,
    user_id: RequireUserDep,
    list_history: ListTailoringHistoryDep,
    db: SessionDep,
    limit: Annotated[
        int, Query(ge=HistoryPageSize.MINIMUM, le=HistoryPageSize.MAXIMUM)
    ] = HistoryPageSize.DEFAULT,
    cursor: Annotated[str | None, Query(max_length=_CURSOR_MAX_LENGTH)] = None,
) -> HistoryPageResponse:
    """One keyset page of the account's history, newest first (ADR-0024). `next_cursor` is `null`
    on the last page; an empty history is `{"items": [], "next_cursor": null}`. No document body
    is ever in it (AC-55). The cursor is decoded here, at the boundary, and never logged."""
    _no_store(response)
    try:
        after = decode_cursor(cursor) if cursor is not None else None
        page = await list_history(user_id, after, HistoryPageSize(limit))
    except DomainError as exc:
        raise domain_error_to_http_exception(exc) from None

    wire = HistoryPageResponse(
        items=[_to_history_entry(entry) for entry in page.entries],
        next_cursor=encode_cursor(page.next_cursor) if page.next_cursor is not None else None,
    )
    await db.commit()
    return wire


@router.get(
    "/{tailoring_run_id}",
    response_model=TailoringRunResponse,
    responses={
        **TAILORING_RUN_NOT_FOUND,
        **VALIDATION_ERROR,
        **NOT_SIGNED_IN,
        **SERVICE_UNAVAILABLE,
    },
)
async def get_my_tailoring_run(
    tailoring_run_id: UUID,
    response: Response,
    user_id: RequireUserDep,
    get_use_case: GetTailoringRunDep,
    db: SessionDep,
) -> TailoringRunResponse:
    """One run in full with its **current** documents — 1.4's shape, `expires_at: null`."""
    wire = await handlers.get_tailoring_run(
        tailoring_run_id=TailoringRunId(tailoring_run_id),
        response=response,
        requester=UserOwner(user_id),
        expires_at=None,
        get_use_case=get_use_case,
    )
    await db.commit()
    return wire


@router.put(
    "/{tailoring_run_id}/documents/{kind}",
    response_model=TailoringRunResponse,
    responses={
        status.HTTP_409_CONFLICT: {
            "model": ErrorResponse,
            "description": (
                "document_version_conflict (+ `current_version`) | tailoring_run_not_editable "
                "(+ `status`) — 1.4's codes."
            ),
        },
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ErrorResponse,
            "description": "validation_error | document_invalid (+ `problem`) — 1.4's codes.",
        },
        **TAILORING_RUN_NOT_FOUND,
        **REQUEST_TOO_LARGE,
        **RATE_LIMITED,
        **NOT_SIGNED_IN,
        **SERVICE_UNAVAILABLE,
    },
)
async def revise_my_tailored_document(
    tailoring_run_id: UUID,
    kind: TailoredDocumentKind,
    response: Response,
    body: Annotated[ReviseDocumentRequest, Body()],
    user_id: RequireUserDep,
    settings: SettingsDep,
    rate_limiter: TailoringReviseRateLimiterDep,
    revise: ReviseTailoredDocumentDep,
    db: SessionDep,
) -> TailoringRunResponse:
    """Replace one of a `succeeded` run's current documents — 1.4's contract; the save budget is
    keyed on the principal `("user", <id>)`, fails open, and is checked before the use case. The
    shared body sets `no-store` and commits."""
    return await handlers.revise_tailored_document(
        run_id=TailoringRunId(tailoring_run_id),
        kind=kind,
        response=response,
        body=body,
        requester=UserOwner(user_id),
        expires_at=None,
        principal=_principal(user_id),
        settings=settings,
        rate_limiter=rate_limiter,
        revise=revise,
        db=db,
    )


@router.delete(
    "/{tailoring_run_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    responses={
        status.HTTP_204_NO_CONTENT: {
            "description": (
                "The entry is gone: its run, its export jobs and — if no other run uses it — its "
                "posting, committed, then the export files unlinked (plan §0.6). A failed unlink "
                "is still a 204: the rows are what the user sees."
            ),
        },
        status.HTTP_409_CONFLICT: {
            "model": ErrorResponse,
            "description": (
                "tailoring_run_in_progress (+ `status`) — the run is `queued` or `running`; "
                "nothing was deleted (H-42). There is no cancellation: a paid call in flight "
                "completes."
            ),
        },
        **TAILORING_RUN_NOT_FOUND,
        **VALIDATION_ERROR,
        **NOT_SIGNED_IN,
        **SERVICE_UNAVAILABLE,
    },
)
async def delete_my_history_entry(
    tailoring_run_id: UUID,
    response: Response,
    user_id: RequireUserDep,
    erase_entry: EraseHistoryEntryDep,
    db: SessionDep,
) -> None:
    """Delete one history entry (`EraseHistoryEntry`). A second delete of the same id is a 404.

    **Rows first, committed, then files** (plan §0.6): the bound `CommittingHistoryEntryData`
    commits the three `DELETE`s before the use case unlinks anything. A failed commit is therefore
    the only way this answers 503 with work in flight — and it is caught **here** and rolled back,
    rather than left to the session dependency's teardown, so nothing is deleted and no file was
    touched (H-46). An unlink that fails after the commit is still a 204: the rows are what the user
    sees, and the file is an orphan for the sweep (H-48).
    """
    _no_store(response)
    run_id = TailoringRunId(tailoring_run_id)
    try:
        report = await erase_entry(user_id, run_id)
    except DomainError as exc:
        raise domain_error_to_http_exception(exc) from None
    except SQLAlchemyError:
        await db.rollback()
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "service_unavailable",
                "message": "Could not delete that entry just now. Please try again.",
            },
        ) from None

    _log_entry_erasure(user_id, run_id, report)
    await db.commit()


@router.get(
    "/{tailoring_run_id}/documents/{kind}/download",
    response_class=Response,
    responses={
        status.HTTP_200_OK: {
            "description": (
                "The current document's bytes, with 1.5's headers: `Content-Type` from the "
                "format, `Content-Disposition` from the domain's constant filename, "
                "`Cache-Control: no-store`, `X-Content-Type-Options: nosniff`."
            ),
            "content": {"text/markdown": {}, "text/plain": {}},
        },
        status.HTTP_409_CONFLICT: {
            "model": ErrorResponse,
            "description": "tailoring_run_not_exportable (+ `status`)",
        },
        status.HTTP_500_INTERNAL_SERVER_ERROR: {
            "model": ErrorResponse,
            "description": "render_failed — 1.5's one deliberate 500.",
        },
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ErrorResponse,
            "description": "render_timed_out | service_unavailable",
        },
        **TAILORING_RUN_NOT_FOUND,
        **VALIDATION_ERROR,
        **NOT_SIGNED_IN,
    },
)
async def download_my_document_inline(
    tailoring_run_id: UUID,
    kind: TailoredDocumentKind,
    format: Annotated[
        Literal["md", "txt"],
        Query(
            description=(
                "`md` or `txt` only. PDF and DOCX are prepared by a worker — "
                "`POST /api/me/tailoring-runs/{tailoring_run_id}/exports`."
            ),
        ),
    ],
    user_id: RequireUserDep,
    render_inline: RenderDocumentInlineDep,
) -> Response:
    """Render a current document to Markdown or plain text inside the request — 1.5's contract.
    Returns its own `Response` (no injected one: its headers would be dropped, see 1.5), which
    carries `no-store`. Writes nothing, so there is nothing to commit."""
    return await export_handlers.download_document_inline(
        run_id=TailoringRunId(tailoring_run_id),
        kind=kind,
        format=format,
        requester=UserOwner(user_id),
        render_inline=render_inline,
    )


@router.post(
    "/{tailoring_run_id}/exports",
    response_model=ExportJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        status.HTTP_200_OK: {
            "model": ExportJobResponse,
            "description": (
                "The current job for this (run, document, format) already exists and is returned "
                "unchanged; `Location` points at it."
            ),
        },
        status.HTTP_202_ACCEPTED: {
            "description": "A new job, committed `queued` and handed to a worker.",
            "headers": {
                "Location": {
                    "description": "`/api/me/export-jobs/{id}` — the job to poll.",
                    "schema": {"type": "string"},
                },
            },
        },
        status.HTTP_409_CONFLICT: {
            "model": ErrorResponse,
            "description": (
                "tailoring_run_not_exportable (+ `status`) | too_many_export_jobs — the run "
                "already has `max_export_jobs_per_user_run` jobs (a user's cap is per run)."
            ),
        },
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ErrorResponse,
            "description": (
                "queue_unavailable | service_unavailable. Never rate_limit_unavailable: the "
                "export limiter fails open, as 1.5's does."
            ),
        },
        **TAILORING_RUN_NOT_FOUND,
        **REQUEST_TOO_LARGE,
        **VALIDATION_ERROR,
        **RATE_LIMITED,
        **NOT_SIGNED_IN,
    },
)
async def request_my_export(
    tailoring_run_id: UUID,
    request: Request,
    response: Response,
    body: Annotated[CreateExportRequest, Body()],
    user_id: RequireUserDep,
    settings: SettingsDep,
    clock: ClockDep,
    rate_limiter: ExportRateLimiterDep,
    request_export_job: RequestExportDep,
    jobs: ExportJobRepositoryDep,
    queue: ExportQueueDep,
    events: EventPublisherDep,
    db: SessionDep,
) -> ExportJobResponse:
    """Ask for a PDF or DOCX of one current document — 1.5's contract (202 new / 200 existing), the
    principal `("user", <id>)` plus the client IP on the fail-open budget, checked before the use
    case. The cap is per run for a user (`max_export_jobs_per_user_run`). The shared body commits,
    then enqueues a created job only."""
    _no_store(response)
    return await export_handlers.request_export(
        run_id=TailoringRunId(tailoring_run_id),
        request=request,
        response=response,
        body=body,
        requester=UserOwner(user_id),
        expires_at=None,
        principal=_principal(user_id),
        jobs_prefix=_JOBS_PREFIX,
        settings=settings,
        rate_limiter=rate_limiter,
        request_export_job=request_export_job,
        jobs=jobs,
        queue=queue,
        events=events,
        clock=clock,
        db=db,
        translate=partial(
            _refusal_no_store,
            max_job_postings_per_user=settings.max_job_postings_per_user,
        ),
    )


@router.get(
    "/{tailoring_run_id}/exports",
    response_model=ExportJobListResponse,
    responses={
        **TAILORING_RUN_NOT_FOUND,
        **VALIDATION_ERROR,
        **NOT_SIGNED_IN,
        **SERVICE_UNAVAILABLE,
    },
)
async def list_my_exports_for_run(
    tailoring_run_id: UUID,
    response: Response,
    user_id: RequireUserDep,
    list_exports: ListExportsForRunDep,
    db: SessionDep,
) -> ExportJobListResponse:
    """Every export job of one of the account's runs, newest first; `items: []` for none."""
    wire = await export_handlers.list_exports_for_run(
        run_id=TailoringRunId(tailoring_run_id),
        response=response,
        requester=UserOwner(user_id),
        expires_at=None,
        jobs_prefix=_JOBS_PREFIX,
        list_exports=list_exports,
    )
    await db.commit()
    return wire
