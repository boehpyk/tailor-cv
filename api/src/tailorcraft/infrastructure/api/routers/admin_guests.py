"""Live guest sessions and their content, as an administrator sees them (slice 4.4, technical plan
§0.2, §0.6, §3, §4; ADR-0035, ADR-0008 amendment (l)).

Built red-first (sdlc.md §2): **SKELETON** (T13: real paths, real signatures, every documented
status in each `responses=` map), **RED** (T14, `qa`), **GREEN** (T15, the handlers).

**The requester is the path's session** (§0.2, OQ-1 = C): each read builds
`GuestOwner(GuestSessionId(session_id))` and calls the guest's own, unchanged use case, whose
equality decides whether a row is that session's. A session that is gone or expired is 404
`guest_session_not_found` by `_admin_responses.guest_session_not_found`, never the global map's 401
(which would trip the administrator's refresh-on-401).

**Its own router, the same firewall** (§0.6): `require_admin` is router-level and every handler also
takes `actor: AdminDep`. Every response a handler builds carries `Cache-Control: no-store`. No
`Origin` check, no rate limit, and **no guest cookie is read or minted** (§0.8).
"""

from __future__ import annotations

from typing import Annotated, Any, Final, Literal
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, Query, Response, status

from tailorcraft.domain.retention.guest_sessions import PAGE_SIZE_MAX
from tailorcraft.domain.tailoring.value_objects import TailoredDocumentKind
from tailorcraft.infrastructure.api.deps import (
    AdminDep,
    DownloadExportFileDep,
    EraseGuestSessionDep,
    GetBaseCvDep,
    GetGuestSessionOverviewDep,
    GetJobPostingDep,
    GetTailoringRunDep,
    ListBaseCvsDep,
    ListExportsForRunDep,
    ListJobPostingsDep,
    ListLiveGuestSessionsDep,
    ListTailoringRunsDep,
    RenderDocumentInlineDep,
    SessionDep,
    SettingsDep,
    require_admin,
)
from tailorcraft.infrastructure.api.routers._admin_responses import (
    FIREWALL,
    GUEST_SESSION_NOT_FOUND,
)
from tailorcraft.infrastructure.api.routers._me_responses import (
    EXPORT_JOB_NOT_FOUND,
    TAILORING_RUN_NOT_FOUND,
    VALIDATION_ERROR,
)
from tailorcraft.infrastructure.api.schemas.admin import (
    AdminGuestBaseCvResponse,
    AdminGuestSessionDetailResponse,
    AdminGuestSessionErasureResponse,
    AdminGuestSessionPageResponse,
)
from tailorcraft.infrastructure.api.schemas.export import ExportJobListResponse
from tailorcraft.infrastructure.api.schemas.intake import ErrorResponse
from tailorcraft.infrastructure.api.schemas.posting import JobPostingResponse
from tailorcraft.infrastructure.api.schemas.tailoring import TailoringRunResponse

log = structlog.get_logger(__name__)

_ADMIN_PREFIX: Final = "/api/admin/guest-sessions"

router = APIRouter(prefix=_ADMIN_PREFIX, tags=["admin"], dependencies=[Depends(require_admin)])

# A keyset cursor is about 70 characters; anything far longer is not one (plan §4).
_CURSOR_MAX_LENGTH: Final = 256
_PAGE_SIZE_DEFAULT: Final = 20

_GUEST_404: Final = "guest_session_not_found — no live guest session with that id"


def _not_found(code_and_meaning: str) -> dict[int | str, dict[str, Any]]:
    """A 404 row naming both the session's refusal and the item's."""
    # `dict[str, Any]` is FastAPI's own type for a `responses=` entry; the `Any` is its API.
    return {
        status.HTTP_404_NOT_FOUND: {
            "model": ErrorResponse,
            "description": f"{_GUEST_404} | {code_and_meaning}",
        },
    }


_BASE_CV_NOT_FOUND = _not_found(
    "base_cv_not_found — byte-identical for an unknown id, another session's CV and a user's."
)
_JOB_POSTING_NOT_FOUND = _not_found(
    "job_posting_not_found — byte-identical for an unknown id, another session's posting and a "
    "user's."
)
_RUN_NOT_FOUND = _not_found(TAILORING_RUN_NOT_FOUND[status.HTTP_404_NOT_FOUND]["description"])
_JOB_NOT_FOUND = _not_found(EXPORT_JOB_NOT_FOUND[status.HTTP_404_NOT_FOUND]["description"])
_BINARY_HEADERS: Final = (
    "`Cache-Control: no-store`, `X-Content-Type-Options: nosniff` and the domain's constant "
    "`Content-Disposition` — 1.5's headers."
)


# --- The directory ------------------------------------------------------------------------------


@router.get(
    "",
    response_model=AdminGuestSessionPageResponse,
    responses={
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ErrorResponse,
            "description": (
                "invalid_cursor — a malformed or tampered cursor (never echoed or logged) | "
                "validation_error — `limit` outside 1…50, or a cursor over the raw bound."
            ),
        },
        **FIREWALL,
    },
)
async def list_guest_sessions(
    response: Response,
    actor: AdminDep,
    list_sessions: ListLiveGuestSessionsDep,
    db: SessionDep,
    cursor: Annotated[str | None, Query(max_length=_CURSOR_MAX_LENGTH)] = None,
    limit: Annotated[int, Query(ge=1, le=PAGE_SIZE_MAX)] = _PAGE_SIZE_DEFAULT,
) -> AdminGuestSessionPageResponse:
    """One keyset page of **live** guest sessions, latest `expires_at` first (AC-9, AC-10)."""
    raise NotImplementedError


@router.get(
    "/{session_id}",
    response_model=AdminGuestSessionDetailResponse,
    responses={**GUEST_SESSION_NOT_FOUND, **VALIDATION_ERROR, **FIREWALL},
)
async def get_guest_session(
    session_id: UUID,
    response: Response,
    actor: AdminDep,
    overview: GetGuestSessionOverviewDep,
    list_base_cvs: ListBaseCvsDep,
    list_job_postings: ListJobPostingsDep,
    list_tailoring_runs: ListTailoringRunsDep,
    settings: SettingsDep,
    db: SessionDep,
) -> AdminGuestSessionDetailResponse:
    """The session's summary and its three lists, in the guest's own item shapes (AC-11)."""
    raise NotImplementedError


@router.delete(
    "/{session_id}",
    response_model=AdminGuestSessionErasureResponse,
    responses={
        **GUEST_SESSION_NOT_FOUND,
        **VALIDATION_ERROR,
        **FIREWALL,
        # After the firewall, so the route's own 503 row is the one documented.
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ErrorResponse,
            "description": (
                "service_unavailable — the `DELETE` could not be committed; nothing was deleted "
                "and no file was touched."
            ),
        },
    },
)
async def erase_guest_session(
    session_id: UUID,
    response: Response,
    actor: AdminDep,
    erase: EraseGuestSessionDep,
    db: SessionDep,
) -> AdminGuestSessionErasureResponse:
    """Delete one session's data now: rows committed, then files (AC-12). A failed unlink is still
    a 200, counted in `unlink_failures`; a second call is 404."""
    raise NotImplementedError


# --- The session's content (4.3's twins) --------------------------------------------------------


@router.get(
    "/{session_id}/base-cvs/{base_cv_id}",
    response_model=AdminGuestBaseCvResponse,
    responses={**_BASE_CV_NOT_FOUND, **VALIDATION_ERROR, **FIREWALL},
)
async def get_guest_base_cv(
    session_id: UUID,
    base_cv_id: UUID,
    response: Response,
    actor: AdminDep,
    get_base_cv: GetBaseCvDep,
    settings: SettingsDep,
    db: SessionDep,
) -> AdminGuestBaseCvResponse:
    """One of the session's CVs **with its extracted text** (`null` while not extracted)."""
    raise NotImplementedError


@router.get(
    "/{session_id}/job-postings/{job_posting_id}",
    response_model=JobPostingResponse,
    responses={**_JOB_POSTING_NOT_FOUND, **VALIDATION_ERROR, **FIREWALL},
)
async def get_guest_job_posting(
    session_id: UUID,
    job_posting_id: UUID,
    response: Response,
    actor: AdminDep,
    get_job_posting: GetJobPostingDep,
    db: SessionDep,
) -> JobPostingResponse:
    """One of the session's postings in full, with its text."""
    raise NotImplementedError


@router.get(
    "/{session_id}/tailoring-runs/{tailoring_run_id}",
    response_model=TailoringRunResponse,
    responses={**_RUN_NOT_FOUND, **VALIDATION_ERROR, **FIREWALL},
)
async def get_guest_tailoring_run(
    session_id: UUID,
    tailoring_run_id: UUID,
    response: Response,
    actor: AdminDep,
    get_use_case: GetTailoringRunDep,
    db: SessionDep,
) -> TailoringRunResponse:
    """One of the session's runs, as the guest's own route returns it."""
    raise NotImplementedError


@router.get(
    "/{session_id}/tailoring-runs/{tailoring_run_id}/documents/{kind}/download",
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
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ErrorResponse,
            "description": "render_timed_out | service_unavailable",
        },
    },
)
async def download_guest_document_inline(
    session_id: UUID,
    tailoring_run_id: UUID,
    kind: TailoredDocumentKind,
    format: Annotated[Literal["md", "txt"], Query(description="`md` or `txt` only.")],
    actor: AdminDep,
    render_inline: RenderDocumentInlineDep,
    db: SessionDep,
) -> Response:
    """A current document of one of the session's runs as Markdown or plain text. Returns its own
    `Response` (an injected one's headers would be dropped)."""
    raise NotImplementedError


@router.get(
    "/{session_id}/tailoring-runs/{tailoring_run_id}/exports",
    response_model=ExportJobListResponse,
    responses={**_RUN_NOT_FOUND, **VALIDATION_ERROR, **FIREWALL},
)
async def list_guest_exports_for_run(
    session_id: UUID,
    tailoring_run_id: UUID,
    response: Response,
    actor: AdminDep,
    list_exports: ListExportsForRunDep,
    db: SessionDep,
) -> ExportJobListResponse:
    """Every export job of one of the session's runs; `file_url` points under
    `/api/admin/guest-sessions/{session_id}/export-jobs/` when ready."""
    raise NotImplementedError


@router.get(
    "/{session_id}/export-jobs/{export_job_id}/file",
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
async def download_guest_export_file(
    session_id: UUID,
    export_job_id: UUID,
    actor: AdminDep,
    download: DownloadExportFileDep,
    db: SessionDep,
) -> Response:
    """The rendered file of one of the session's `ready` export jobs. Returns its own `Response`,
    which carries `no-store`."""
    raise NotImplementedError
