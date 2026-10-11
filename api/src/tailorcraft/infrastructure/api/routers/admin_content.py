"""An account's content, as an administrator sees it (slice 4.3, technical plan §0, §3, §4;
ADR-0034, ADR-0008 amendment (k)).

Built red-first (sdlc.md §2): **SKELETON** (T4, this file — real paths, real signatures, every
documented status in each `responses=` map, handlers raising `NotImplementedError`), **RED** (T5,
`qa`), **GREEN** (T6).

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

from typing import Annotated, Any, Final, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response, status

from tailorcraft.domain.tailoring.history import HistoryPageSize
from tailorcraft.domain.tailoring.value_objects import TailoredDocumentKind
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
from tailorcraft.infrastructure.api.routers._admin_responses import FIREWALL, USER_NOT_FOUND
from tailorcraft.infrastructure.api.routers._me_responses import (
    EXPORT_JOB_NOT_FOUND,
    TAILORING_RUN_NOT_FOUND,
    VALIDATION_ERROR,
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
    raise NotImplementedError


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
    raise NotImplementedError


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
    raise NotImplementedError


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
    raise NotImplementedError


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
    raise NotImplementedError


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
    raise NotImplementedError


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
    raise NotImplementedError


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
    raise NotImplementedError


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
    raise NotImplementedError


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
    raise NotImplementedError


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
    raise NotImplementedError
