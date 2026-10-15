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
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.exc import SQLAlchemyError

from tailorcraft.application.identity.resolve_guest_session import resolve_active_guest_session
from tailorcraft.domain.export.value_objects import ExportJobId
from tailorcraft.domain.identity.errors import GuestSessionExpired, GuestSessionNotFound
from tailorcraft.domain.identity.guest_session import GuestSession
from tailorcraft.domain.identity.ownership import GuestOwner
from tailorcraft.domain.identity.ports import GuestSessionRepository
from tailorcraft.domain.identity.user import User
from tailorcraft.domain.identity.value_objects import GuestSessionId
from tailorcraft.domain.intake.value_objects import BaseCvId
from tailorcraft.domain.posting.value_objects import JobPostingId
from tailorcraft.domain.retention.errors import InvalidGuestSessionCursor
from tailorcraft.domain.retention.guest_sessions import (
    PAGE_SIZE_MAX,
    GuestSessionCursor,
    GuestSessionSummary,
)
from tailorcraft.domain.shared.clock import Clock
from tailorcraft.domain.shared.errors import DomainError
from tailorcraft.domain.tailoring.value_objects import TailoredDocumentKind, TailoringRunId
from tailorcraft.infrastructure.api.deps import (
    AdminDep,
    ClockDep,
    DownloadExportFileDep,
    EraseGuestSessionDep,
    GetBaseCvDep,
    GetGuestSessionOverviewDep,
    GetJobPostingDep,
    GetTailoringRunDep,
    GuestSessionRepositoryDep,
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
from tailorcraft.infrastructure.api.routers import _export_handlers as export_handlers
from tailorcraft.infrastructure.api.routers import _posting_handlers as posting_handlers
from tailorcraft.infrastructure.api.routers import _tailoring_handlers as tailoring_handlers
from tailorcraft.infrastructure.api.routers._admin_access import AdminAccess, RetentionAction
from tailorcraft.infrastructure.api.routers._admin_responses import (
    FIREWALL,
    GUEST_SESSION_NOT_FOUND,
    NO_STORE,
    guest_session_not_found,
    refusal,
    set_no_store,
)
from tailorcraft.infrastructure.api.routers._keyset_cursor import decode_keyset, encode_keyset
from tailorcraft.infrastructure.api.routers._me_responses import (
    EXPORT_JOB_NOT_FOUND,
    TAILORING_RUN_NOT_FOUND,
    VALIDATION_ERROR,
)
from tailorcraft.infrastructure.api.routers.intake import to_response as cv_to_response
from tailorcraft.infrastructure.api.schemas.admin import (
    AdminGuestBaseCvResponse,
    AdminGuestSessionDetailResponse,
    AdminGuestSessionErasureResponse,
    AdminGuestSessionPageResponse,
    AdminGuestSessionSummary,
)
from tailorcraft.infrastructure.api.schemas.export import ExportJobListResponse
from tailorcraft.infrastructure.api.schemas.intake import ErrorResponse
from tailorcraft.infrastructure.api.schemas.posting import JobPostingResponse
from tailorcraft.infrastructure.api.schemas.tailoring import TailoringRunResponse
from tailorcraft.infrastructure.retention.log_events import EVENT_FILE_UNLINK_FAILED

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
    set_no_store(response)
    with RetentionAction(actor, "guest_list") as action:
        try:
            after = _decode_cursor(cursor) if cursor is not None else None
            page = await list_sessions(after, limit)
        except DomainError as exc:
            raise action.refuse(refusal(exc)) from None

        wire = AdminGuestSessionPageResponse(
            items=[_summary(item) for item in page.items],
            next_cursor=(
                encode_keyset(page.next_cursor.expires_at, page.next_cursor.session_id.value)
                if page.next_cursor is not None
                else None
            ),
        )
        await db.commit()
        action.log("ok", items=len(wire.items))
        return wire


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
    set_no_store(response)
    with RetentionAction(actor, "guest_overview", session_id) as action:
        sid = GuestSessionId(session_id)
        try:
            summary = await overview(sid)
            cvs = await list_base_cvs(sid)
            postings = await list_job_postings(sid)
            runs = await list_tailoring_runs(sid)
        except DomainError as exc:
            raise action.refuse(_translate(exc)) from None

        expires_at = summary.expires_at
        wire = AdminGuestSessionDetailResponse(
            session=_summary(summary),
            base_cvs=[cv_to_response(cv, expires_at, settings) for cv in cvs],
            job_postings=[posting_handlers.to_summary(p, expires_at) for p in postings],
            tailoring_runs=[tailoring_handlers.to_summary(r, expires_at) for r in runs],
        )
        await db.commit()
        action.log("ok")
        return wire


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
    set_no_store(response)
    with RetentionAction(actor, "guest_erase", session_id) as action:
        try:
            report = await erase(GuestSessionId(session_id))
        except DomainError as exc:
            raise action.refuse(_translate(exc)) from None
        except SQLAlchemyError:
            # The `DELETE` (or its commit) was refused: nothing was deleted and no file was
            # touched (F-13). Rolled back here, not left to the session's teardown.
            await db.rollback()
            raise action.refuse(
                HTTPException(
                    status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail={
                        "code": "service_unavailable",
                        "message": "Could not delete that guest session just now. Please try again.",
                    },
                    headers=dict(NO_STORE),
                )
            ) from None

        # The purge's own per-failure line (F-12): the row is gone, the bytes are an orphan.
        for failure in report.file_unlink_failures:
            log.warning(EVENT_FILE_UNLINK_FAILED, error_type=failure.error_type)
        # Rows committed and files unlinked: ok before the trailing commit (4.3's AC-20).
        action.log(
            "ok",
            files_unlinked=report.files_unlinked,
            unlink_failures=len(report.file_unlink_failures),
        )
        await db.commit()
        return AdminGuestSessionErasureResponse(
            files_unlinked=report.files_unlinked,
            unlink_failures=len(report.file_unlink_failures),
        )


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
    sessions: GuestSessionRepositoryDep,
    clock: ClockDep,
) -> AdminGuestBaseCvResponse:
    """One of the session's CVs **with its extracted text** (`null` while not extracted)."""
    set_no_store(response)
    with _access(actor, session_id, "base_cv", "read", base_cv_id) as access:
        try:
            live = await _live(session_id, sessions, clock)
            cv = await get_base_cv(BaseCvId(base_cv_id), _owner(session_id))
        except DomainError as exc:
            raise access.refuse(_translate(exc)) from None

        wire = AdminGuestBaseCvResponse(
            **cv_to_response(cv, live.expires_at, settings).model_dump(),
            extracted_text=cv.extracted_text.value if cv.extracted_text is not None else None,
        )
        await db.commit()
        access.log("ok")
        return wire


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
    sessions: GuestSessionRepositoryDep,
    clock: ClockDep,
) -> JobPostingResponse:
    """One of the session's postings in full, with its text."""
    set_no_store(response)
    with _access(actor, session_id, "job_posting", "read", job_posting_id) as access:
        try:
            live = await _live(session_id, sessions, clock)
            posting = await get_job_posting(JobPostingId(job_posting_id), _owner(session_id))
        except DomainError as exc:
            raise access.refuse(_translate(exc)) from None

        wire = posting_handlers.to_response(posting, live.expires_at)
        await db.commit()
        access.log("ok")
        return wire


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
    sessions: GuestSessionRepositoryDep,
    clock: ClockDep,
) -> TailoringRunResponse:
    """One of the session's runs, as the guest's own route returns it."""
    with _access(actor, session_id, "tailoring_run", "read", tailoring_run_id) as access:
        try:
            live = await _live(session_id, sessions, clock)
        except DomainError as exc:
            raise access.refuse(_translate(exc)) from None
        try:
            wire = await tailoring_handlers.get_tailoring_run(
                tailoring_run_id=TailoringRunId(tailoring_run_id),
                response=response,
                requester=_owner(session_id),
                expires_at=live.expires_at,
                get_use_case=get_use_case,
                translate=_translate,
            )
        except HTTPException as http:
            raise access.refuse(http) from None

        await db.commit()
        access.log("ok")
        return wire


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
    with _access(actor, session_id, "tailoring_run", "download", tailoring_run_id) as access:
        try:
            wire = await export_handlers.download_document_inline(
                run_id=TailoringRunId(tailoring_run_id),
                kind=kind,
                format=format,
                requester=_owner(session_id),
                render_inline=render_inline,
                translate=_translate,
            )
        except HTTPException as http:
            raise access.refuse(http) from None

        await db.commit()
        access.log("ok")
        return wire


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
    sessions: GuestSessionRepositoryDep,
    clock: ClockDep,
) -> ExportJobListResponse:
    """Every export job of one of the session's runs; `file_url` points under
    `/api/admin/guest-sessions/{session_id}/export-jobs/` when ready."""
    with _access(actor, session_id, "export_job", "list") as access:
        try:
            live = await _live(session_id, sessions, clock)
        except DomainError as exc:
            raise access.refuse(_translate(exc)) from None
        try:
            wire = await export_handlers.list_exports_for_run(
                run_id=TailoringRunId(tailoring_run_id),
                response=response,
                requester=_owner(session_id),
                expires_at=live.expires_at,
                jobs_prefix=f"{_ADMIN_PREFIX}/{session_id}/export-jobs",
                list_exports=list_exports,
                translate=_translate,
            )
        except HTTPException as http:
            raise access.refuse(http) from None

        await db.commit()
        access.log("ok")
        return wire


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
    with _access(actor, session_id, "export_job", "download", export_job_id) as access:
        try:
            wire = await export_handlers.download_export_file(
                export_job_id=ExportJobId(export_job_id),
                requester=_owner(session_id),
                download=download,
                translate=_translate,
            )
        except HTTPException as http:
            raise access.refuse(http) from None

        await db.commit()
        access.log("ok")
        return wire


# --- Helpers ------------------------------------------------------------------------------------

_CURSOR_REFUSAL: Final = "not a guest session cursor"


def _owner(session_id: UUID) -> GuestOwner:
    """The requester is the path's session (§0.2): the use case's own equality decides the row."""
    return GuestOwner(GuestSessionId(session_id))


async def _live(session_id: UUID, sessions: GuestSessionRepository, clock: Clock) -> GuestSession:
    """The path's session, live — for the `expires_at` the guest's own response carries (AC-13).
    A gone or expired session raises, and `_translate` makes it the local 404."""
    return await resolve_active_guest_session(sessions, clock, GuestSessionId(session_id))


def _access(
    actor: User,
    session_id: UUID,
    kind: Literal["base_cv", "job_posting", "tailoring_run", "export_job"],
    action: Literal["list", "read", "download"],
    item_id: UUID | None = None,
) -> AdminAccess:
    return AdminAccess(
        actor, session_id, kind, action, item_id, target_key="target_guest_session_id"
    )


def _summary(summary: GuestSessionSummary) -> AdminGuestSessionSummary:
    return AdminGuestSessionSummary(
        id=summary.session_id.value,
        created_at=summary.created_at,
        expires_at=summary.expires_at,
        base_cvs=summary.base_cvs,
        job_postings=summary.job_postings,
        tailoring_runs=summary.tailoring_runs,
        export_jobs=summary.export_jobs,
    )


def _decode_cursor(raw: str) -> GuestSessionCursor:
    """The directory's cursor from `?cursor=`; `InvalidGuestSessionCursor` on any failure. A fixed
    sentence, raised `from None`: the cursor is never echoed or logged."""
    try:
        expires_at, session_id = decode_keyset(raw)
    except ValueError:
        raise InvalidGuestSessionCursor(_CURSOR_REFUSAL) from None
    return GuestSessionCursor(expires_at=expires_at, session_id=GuestSessionId(session_id))


def _translate(exc: DomainError) -> HTTPException:
    """A gone or expired **target** session is 404 `guest_session_not_found`, never the global
    map's 401, which would trip the administrator's refresh-on-401 (§0.2); every other refusal is
    the global map's, plus `no-store`."""
    if isinstance(exc, GuestSessionNotFound | GuestSessionExpired):
        return guest_session_not_found()
    return refusal(exc)
