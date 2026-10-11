"""An account's content, as an administrator sees it (slice 4.3, technical plan §0, §3, §4;
ADR-0034, ADR-0008 amendment (k)).

Built red-first (sdlc.md §2): **SKELETON** (T4: real paths, real signatures, every documented
status in each `responses=` map), **RED** (T5, `qa`), **GREEN** (T6, the handlers).

**A second driving adapter over unchanged use cases** (§0.1): every route calls the use case its
`/api/me/` twin calls, with the same provider, and the shared handler body where one exists
(`_tailoring_handlers`, `_export_handlers`). **The requester is the path's owner** (§0.2):
`UserOwner(UserId(user_id))`, and the use case's own equality decides whether the row is that
account's — no new code decides who may read what.

**Its own router, the same firewall** (§0.10): `require_admin` is router-level, as in `admin.py`
and `admin_users.py`, and 4.1's structural walk pins it on every route under `/api/admin`. A
non-admin gets the 404 byte-identical to an unmatched path, before any parameter is validated.

**A missing target is 404 `user_not_found`** (§0.4), by `_admin_responses.user_not_found`: the
global map's 401 would sign the administrator out. Every response a handler builds carries
`Cache-Control: no-store`. No `Origin` check, no rate limit, no guest cookie (§0.9).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any, Final, Literal
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.exc import SQLAlchemyError

from tailorcraft.domain.export.value_objects import ExportJobId
from tailorcraft.domain.identity.errors import UserNotFound
from tailorcraft.domain.identity.ownership import UserOwner
from tailorcraft.domain.identity.user import User
from tailorcraft.domain.identity.value_objects import UserId
from tailorcraft.domain.intake.value_objects import BaseCvId
from tailorcraft.domain.posting.value_objects import JobPostingId
from tailorcraft.domain.retention.errors import AccountNotFound
from tailorcraft.domain.shared.errors import DomainError
from tailorcraft.domain.tailoring.history import HistoryPageSize
from tailorcraft.domain.tailoring.value_objects import TailoredDocumentKind, TailoringRunId
from tailorcraft.infrastructure.api.deps import (
    AdminDep,
    DeleteSavedBaseCvDep,
    DownloadExportFileDep,
    EraseHistoryEntryDep,
    GetBaseCvDep,
    GetJobPostingDep,
    GetTailoringRunDep,
    ListExportsForRunDep,
    ListRecentJobPostingsDep,
    ListSavedBaseCvsDep,
    ListTailoringHistoryDep,
    RenderDocumentInlineDep,
    SessionDep,
    SettingsDep,
    require_admin,
)
from tailorcraft.infrastructure.api.routers import _export_handlers as export_handlers
from tailorcraft.infrastructure.api.routers import _posting_handlers as posting_handlers
from tailorcraft.infrastructure.api.routers import _tailoring_handlers as tailoring_handlers
from tailorcraft.infrastructure.api.routers._admin_responses import (
    FIREWALL,
    NO_STORE,
    USER_NOT_FOUND,
    refusal,
    set_no_store,
    user_not_found,
)
from tailorcraft.infrastructure.api.routers._erasure_log import log_history_entry_erasure
from tailorcraft.infrastructure.api.routers._history_cursor import decode_cursor, encode_cursor
from tailorcraft.infrastructure.api.routers._me_responses import (
    EXPORT_JOB_NOT_FOUND,
    TAILORING_RUN_NOT_FOUND,
    VALIDATION_ERROR,
)
from tailorcraft.infrastructure.api.routers._saved_base_cv_handlers import (
    aggregate_to_response,
    log_unlink_failure,
    summary_to_response,
)
from tailorcraft.infrastructure.api.routers.me_job_postings import RECENT_POSTINGS_MAX
from tailorcraft.infrastructure.api.schemas.admin import (
    AdminBaseCvDeletionResponse,
    AdminHistoryEntryErasureResponse,
    AdminSavedBaseCvResponse,
)
from tailorcraft.infrastructure.api.schemas.export import ExportJobListResponse
from tailorcraft.infrastructure.api.schemas.intake import ErrorResponse, SavedBaseCvListResponse
from tailorcraft.infrastructure.api.schemas.posting import (
    JobPostingListResponse,
    JobPostingResponse,
)
from tailorcraft.infrastructure.api.schemas.tailoring import (
    HistoryPageResponse,
    TailoringRunResponse,
)

log = structlog.get_logger(__name__)

EVENT_ADMIN_CONTENT_ACCESS: Final = "admin.content_access"
_ADMIN_PREFIX: Final = "/api/admin/users"

router = APIRouter(
    prefix="/api/admin/users/{user_id}", tags=["admin"], dependencies=[Depends(require_admin)]
)

# A history cursor is about 70 characters; anything far longer is not one (plan §4).
_CURSOR_MAX_LENGTH: Final = 256

# `dict[str, Any]` is FastAPI's own type for a `responses=` entry; the `Any` is its API.
_BASE_CV_NOT_FOUND: dict[int | str, dict[str, Any]] = {
    status.HTTP_404_NOT_FOUND: {
        "model": ErrorResponse,
        "description": (
            "user_not_found — no account with that id | base_cv_not_found — byte-identical for "
            "an id that does not exist, another account's CV and a guest-owned CV."
        ),
    },
}
_JOB_POSTING_NOT_FOUND: dict[int | str, dict[str, Any]] = {
    status.HTTP_404_NOT_FOUND: {
        "model": ErrorResponse,
        "description": (
            "user_not_found — no account with that id | job_posting_not_found — byte-identical "
            "for an id that does not exist, another account's posting and a guest-owned one."
        ),
    },
}
# The run's and the job's 404 descriptions are the `/api/me/` ones, with `user_not_found` added.
_RUN_NOT_FOUND: dict[int | str, dict[str, Any]] = {
    status.HTTP_404_NOT_FOUND: {
        **TAILORING_RUN_NOT_FOUND[status.HTTP_404_NOT_FOUND],
        "description": "user_not_found | "
        + TAILORING_RUN_NOT_FOUND[status.HTTP_404_NOT_FOUND]["description"],
    },
}
_JOB_NOT_FOUND: dict[int | str, dict[str, Any]] = {
    status.HTTP_404_NOT_FOUND: {
        **EXPORT_JOB_NOT_FOUND[status.HTTP_404_NOT_FOUND],
        "description": "user_not_found | "
        + EXPORT_JOB_NOT_FOUND[status.HTTP_404_NOT_FOUND]["description"],
    },
}
_BINARY_HEADERS: Final = (
    "`Cache-Control: no-store`, `X-Content-Type-Options: nosniff` and the domain's constant "
    "`Content-Disposition` — 1.5's headers."
)


# --- Saved CVs ----------------------------------------------------------------------------------


@router.get(
    "/base-cvs",
    response_model=SavedBaseCvListResponse,
    responses={**USER_NOT_FOUND, **VALIDATION_ERROR, **FIREWALL},
)
async def list_user_base_cvs(
    user_id: UUID,
    response: Response,
    actor: AdminDep,
    list_saved: ListSavedBaseCvsDep,
    settings: SettingsDep,
    db: SessionDep,
) -> SavedBaseCvListResponse:
    """The account's saved CVs, newest first — the owner's list shape, **no text** (AC-3)."""
    set_no_store(response)
    access = _Access(actor, user_id, "base_cv", "list")
    try:
        summaries = await list_saved(UserId(user_id))
    except DomainError as exc:
        raise access.refuse(_translate(exc)) from None

    wire = SavedBaseCvListResponse(
        items=[summary_to_response(summary, settings) for summary in summaries]
    )
    await db.commit()
    access.log("ok")
    return wire


@router.get(
    "/base-cvs/{base_cv_id}",
    response_model=AdminSavedBaseCvResponse,
    responses={**_BASE_CV_NOT_FOUND, **VALIDATION_ERROR, **FIREWALL},
)
async def get_user_base_cv(
    user_id: UUID,
    base_cv_id: UUID,
    response: Response,
    actor: AdminDep,
    get_base_cv: GetBaseCvDep,
    settings: SettingsDep,
    db: SessionDep,
) -> AdminSavedBaseCvResponse:
    """One of the account's saved CVs **with its extracted text** (`null` while not extracted)."""
    set_no_store(response)
    access = _Access(actor, user_id, "base_cv", "read", base_cv_id)
    try:
        cv = await get_base_cv(BaseCvId(base_cv_id), _owner(user_id))
    except DomainError as exc:
        raise access.refuse(_translate(exc)) from None

    wire = AdminSavedBaseCvResponse(
        **aggregate_to_response(cv, settings).model_dump(),
        extracted_text=cv.extracted_text.value if cv.extracted_text is not None else None,
    )
    await db.commit()
    access.log("ok")
    return wire


@router.delete(
    "/base-cvs/{base_cv_id}",
    response_model=AdminBaseCvDeletionResponse,
    responses={**_BASE_CV_NOT_FOUND, **VALIDATION_ERROR, **FIREWALL},
)
async def delete_user_base_cv(
    user_id: UUID,
    base_cv_id: UUID,
    response: Response,
    actor: AdminDep,
    delete_saved: DeleteSavedBaseCvDep,
    db: SessionDep,
) -> AdminBaseCvDeletionResponse:
    """Delete one of the account's saved CVs: the row, **committed**, then the file (the user's
    `DeleteSavedBaseCv`, bound to `CommittingBaseCvRemoval`). A failed unlink is still a 200 with
    `file_unlinked: false`; the bytes are an orphan for the sweep."""
    set_no_store(response)
    access = _Access(actor, user_id, "base_cv", "delete", base_cv_id)
    target = UserId(user_id)
    try:
        result = await delete_saved(BaseCvId(base_cv_id), target)
    except DomainError as exc:
        raise access.refuse(_translate(exc)) from None

    if not result.file_unlinked:
        # The user route's own warning line, byte-identical (AC-20).
        log_unlink_failure(base_cv_id, target, result.unlink_error_type)
    await db.commit()
    access.log("ok")
    return AdminBaseCvDeletionResponse(file_unlinked=result.file_unlinked)


# --- Job postings -------------------------------------------------------------------------------


@router.get(
    "/job-postings",
    response_model=JobPostingListResponse,
    responses={**USER_NOT_FOUND, **VALIDATION_ERROR, **FIREWALL},
)
async def list_user_job_postings(
    user_id: UUID,
    response: Response,
    actor: AdminDep,
    list_recent: ListRecentJobPostingsDep,
    db: SessionDep,
    limit: Annotated[int, Query(ge=1, le=RECENT_POSTINGS_MAX)] = RECENT_POSTINGS_MAX,
) -> JobPostingListResponse:
    """The account's most recent postings, newest first, as summaries (a preview, never `text`);
    `limit` 1…20, default 20 (AC-5)."""
    set_no_store(response)
    access = _Access(actor, user_id, "job_posting", "list")
    try:
        postings = await list_recent(UserId(user_id), limit)
    except DomainError as exc:
        raise access.refuse(_translate(exc)) from None

    wire = JobPostingListResponse(
        items=[posting_handlers.to_summary(posting, None) for posting in postings]
    )
    await db.commit()
    access.log("ok")
    return wire


@router.get(
    "/job-postings/{job_posting_id}",
    response_model=JobPostingResponse,
    responses={**_JOB_POSTING_NOT_FOUND, **VALIDATION_ERROR, **FIREWALL},
)
async def get_user_job_posting(
    user_id: UUID,
    job_posting_id: UUID,
    response: Response,
    actor: AdminDep,
    get_job_posting: GetJobPostingDep,
    db: SessionDep,
) -> JobPostingResponse:
    """One of the account's postings in full, with its text; `expires_at: null`."""
    set_no_store(response)
    access = _Access(actor, user_id, "job_posting", "read", job_posting_id)
    try:
        posting = await get_job_posting(JobPostingId(job_posting_id), _owner(user_id))
    except DomainError as exc:
        raise access.refuse(_translate(exc)) from None

    wire = posting_handlers.to_response(posting, None)
    await db.commit()
    access.log("ok")
    return wire


# --- Tailoring runs (history) -------------------------------------------------------------------


@router.get(
    "/tailoring-runs",
    response_model=HistoryPageResponse,
    responses={
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ErrorResponse,
            "description": (
                "validation_error — `limit` outside 1…50, or a malformed id or an over-long "
                "cursor | invalid_cursor — a malformed, tampered or wrong-shape cursor; the "
                "cursor is never echoed or logged."
            ),
        },
        **USER_NOT_FOUND,
        **FIREWALL,
    },
)
async def list_user_tailoring_history(
    user_id: UUID,
    response: Response,
    actor: AdminDep,
    list_history: ListTailoringHistoryDep,
    db: SessionDep,
    limit: Annotated[
        int, Query(ge=HistoryPageSize.MINIMUM, le=HistoryPageSize.MAXIMUM)
    ] = HistoryPageSize.DEFAULT,
    cursor: Annotated[str | None, Query(max_length=_CURSOR_MAX_LENGTH)] = None,
) -> HistoryPageResponse:
    """One keyset page of the account's history, newest first — the user's own shape (AC-6)."""
    set_no_store(response)
    access = _Access(actor, user_id, "history", "list")
    try:
        after = decode_cursor(cursor) if cursor is not None else None
        page = await list_history(UserId(user_id), after, HistoryPageSize(limit))
    except DomainError as exc:
        raise access.refuse(_translate(exc)) from None

    wire = HistoryPageResponse(
        items=[tailoring_handlers.to_history_entry(entry) for entry in page.entries],
        next_cursor=encode_cursor(page.next_cursor) if page.next_cursor is not None else None,
    )
    await db.commit()
    access.log("ok")
    return wire


@router.get(
    "/tailoring-runs/{tailoring_run_id}",
    response_model=TailoringRunResponse,
    responses={**_RUN_NOT_FOUND, **VALIDATION_ERROR, **FIREWALL},
)
async def get_user_tailoring_run(
    user_id: UUID,
    tailoring_run_id: UUID,
    response: Response,
    actor: AdminDep,
    get_use_case: GetTailoringRunDep,
    db: SessionDep,
) -> TailoringRunResponse:
    """One of the account's runs in full with its **current** documents; `expires_at: null`."""
    access = _Access(actor, user_id, "tailoring_run", "read", tailoring_run_id)
    try:
        wire = await tailoring_handlers.get_tailoring_run(
            tailoring_run_id=TailoringRunId(tailoring_run_id),
            response=response,
            requester=_owner(user_id),
            expires_at=None,
            get_use_case=get_use_case,
            translate=_translate,
        )
    except HTTPException as http:
        raise access.refuse(http) from None

    await db.commit()
    access.log("ok")
    return wire


@router.get(
    "/tailoring-runs/{tailoring_run_id}/documents/{kind}/download",
    response_class=Response,
    responses={
        status.HTTP_200_OK: {
            "description": f"The current document's bytes, with {_BINARY_HEADERS}",
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
        **_RUN_NOT_FOUND,
        **VALIDATION_ERROR,
        **FIREWALL,
        # After the firewall, so the route's own 503 rows are the ones documented.
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ErrorResponse,
            "description": "render_timed_out | service_unavailable",
        },
    },
)
async def download_user_document_inline(
    user_id: UUID,
    tailoring_run_id: UUID,
    kind: TailoredDocumentKind,
    format: Annotated[Literal["md", "txt"], Query(description="`md` or `txt` only.")],
    actor: AdminDep,
    render_inline: RenderDocumentInlineDep,
    db: SessionDep,
) -> Response:
    """A current document of one of the account's runs as Markdown or plain text, rendered in the
    request. Returns its own `Response` (an injected one's headers would be dropped)."""
    access = _Access(actor, user_id, "tailoring_run", "download", tailoring_run_id)
    try:
        wire = await export_handlers.download_document_inline(
            run_id=TailoringRunId(tailoring_run_id),
            kind=kind,
            format=format,
            requester=_owner(user_id),
            render_inline=render_inline,
            translate=_translate,
        )
    except HTTPException as http:
        raise access.refuse(http) from None

    await db.commit()
    access.log("ok")
    return wire


@router.get(
    "/tailoring-runs/{tailoring_run_id}/exports",
    response_model=ExportJobListResponse,
    responses={**_RUN_NOT_FOUND, **VALIDATION_ERROR, **FIREWALL},
)
async def list_user_exports_for_run(
    user_id: UUID,
    tailoring_run_id: UUID,
    response: Response,
    actor: AdminDep,
    list_exports: ListExportsForRunDep,
    db: SessionDep,
) -> ExportJobListResponse:
    """Every export job of one of the account's runs, newest first; `file_url` points under
    `/api/admin/users/{user_id}/export-jobs/` when ready."""
    access = _Access(actor, user_id, "export_job", "list")
    try:
        wire = await export_handlers.list_exports_for_run(
            run_id=TailoringRunId(tailoring_run_id),
            response=response,
            requester=_owner(user_id),
            expires_at=None,
            jobs_prefix=f"{_ADMIN_PREFIX}/{user_id}/export-jobs",
            list_exports=list_exports,
            translate=_translate,
        )
    except HTTPException as http:
        raise access.refuse(http) from None

    await db.commit()
    access.log("ok")
    return wire


@router.delete(
    "/tailoring-runs/{tailoring_run_id}",
    response_model=AdminHistoryEntryErasureResponse,
    responses={
        status.HTTP_409_CONFLICT: {
            "model": ErrorResponse,
            "description": (
                "tailoring_run_in_progress (+ `status`) — the run is `queued` or `running`; "
                "nothing was deleted."
            ),
        },
        **_RUN_NOT_FOUND,
        **VALIDATION_ERROR,
        **FIREWALL,
    },
)
async def delete_user_history_entry(
    user_id: UUID,
    tailoring_run_id: UUID,
    response: Response,
    actor: AdminDep,
    erase_entry: EraseHistoryEntryDep,
    db: SessionDep,
) -> AdminHistoryEntryErasureResponse:
    """Delete one of the account's history entries (the user's `EraseHistoryEntry`, bound to
    `CommittingHistoryEntryData`): rows committed, then files. A failed commit is 503 with nothing
    deleted; a failed unlink is counted in `unlink_failures`."""
    set_no_store(response)
    access = _Access(actor, user_id, "history", "delete", tailoring_run_id)
    target = UserId(user_id)
    run_id = TailoringRunId(tailoring_run_id)
    try:
        report = await erase_entry(target, run_id)
    except DomainError as exc:
        raise access.refuse(_translate(exc)) from None
    except SQLAlchemyError:
        # The user twin's H-46: the committing adapter's commit failed, so nothing was deleted and
        # no file was touched. Rolled back here, not left to the session's teardown.
        await db.rollback()
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "service_unavailable",
                "message": "Could not delete that entry just now. Please try again.",
            },
            headers=dict(NO_STORE),
        ) from None

    # The user route's own lines, byte-identical (AC-20).
    log_history_entry_erasure(target, run_id, report)
    await db.commit()
    access.log("ok")
    return AdminHistoryEntryErasureResponse(
        tailoring_runs=1,
        job_postings=int(report.posting_deleted),
        export_jobs=report.export_jobs,
        tracked_applications=int(report.tracked_application_deleted),
        files_unlinked=report.files_unlinked,
        unlink_failures=len(report.unlink_failures),
    )


# --- Export files -------------------------------------------------------------------------------


@router.get(
    "/export-jobs/{export_job_id}/file",
    response_class=Response,
    responses={
        status.HTTP_200_OK: {
            "description": f"The stored bytes, with {_BINARY_HEADERS}",
            "content": {
                "application/pdf": {},
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document": {},
            },
        },
        status.HTTP_409_CONFLICT: {
            "model": ErrorResponse,
            "description": "export_not_ready (+ `status`, + `failure_reason` when failed)",
        },
        status.HTTP_410_GONE: {
            "model": ErrorResponse,
            "description": "export_file_gone — the row says `ready` and the store has nothing.",
        },
        **_JOB_NOT_FOUND,
        **VALIDATION_ERROR,
        **FIREWALL,
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ErrorResponse,
            "description": "storage_unavailable | service_unavailable",
        },
    },
)
async def download_user_export_file(
    user_id: UUID,
    export_job_id: UUID,
    actor: AdminDep,
    download: DownloadExportFileDep,
    db: SessionDep,
) -> Response:
    """The rendered file of one of the account's `ready` export jobs, any layout or run version.
    Returns its own `Response`, which carries `no-store`."""
    access = _Access(actor, user_id, "export_job", "download", export_job_id)
    try:
        wire = await export_handlers.download_export_file(
            export_job_id=ExportJobId(export_job_id),
            requester=_owner(user_id),
            download=download,
            translate=_translate,
        )
    except HTTPException as http:
        raise access.refuse(http) from None

    await db.commit()
    access.log("ok")
    return wire


# --- Helpers ------------------------------------------------------------------------------------


def _owner(user_id: UUID) -> UserOwner:
    """The requester is the path's account (§0.2): the use case's own equality decides the row."""
    return UserOwner(UserId(user_id))


def _translate(exc: DomainError) -> HTTPException:
    """A missing **target** is 404 `user_not_found`, never the global map's 401, which would sign
    the administrator out (§0.4); every other refusal is the global map's, plus `no-store`."""
    if isinstance(exc, UserNotFound | AccountNotFound):
        return user_not_found()
    return refusal(exc)


@dataclass(frozen=True, slots=True)
class _Access:
    """One `admin.content_access` line per request (§0.7): two ids, the item's id (absent on a
    list) and three closed words — never a body, a filename, a label, a title or an email."""

    actor: User
    target: UUID
    kind: Literal["base_cv", "job_posting", "tailoring_run", "export_job", "history"]
    action: Literal["list", "read", "download", "delete"]
    item_id: UUID | None = None

    def log(self, outcome: Literal["ok", "not_found", "refused"]) -> None:
        fields: dict[str, str] = {
            "actor_id": str(self.actor.id.value),
            "target_user_id": str(self.target),
            "kind": self.kind,
            "action": self.action,
            "outcome": outcome,
        }
        if self.item_id is not None:
            fields["item_id"] = str(self.item_id)
        log.info(EVENT_ADMIN_CONTENT_ACCESS, **fields)

    def refuse(self, http: HTTPException) -> HTTPException:
        """Log the refusal (a 404 is `not_found`, anything else `refused`) and hand it back."""
        self.log("not_found" if http.status_code == status.HTTP_404_NOT_FOUND else "refused")
        return http
