"""The `intake` HTTP surface: upload a base CV, list a guest session's base CVs, read one.

This router was built in three passes (CLAUDE.md, sdlc.md §2): **SKELETON** (T24 — real signatures,
real schemas, `NotImplementedError` bodies), **RED** (T25, `qa` — 41 failing tests against those exact
signatures), **GREEN** (T26, this file — boundary validation, the rate limit and the `DomainError` ->
status translation, until those tests pass without a single edit to them).

Every documented failure mode is declared in each handler's `responses=` map, even though the OpenAPI
schema alone cannot enforce it — the schema is the frontend's contract (ADR-0001), and this router's
job is to make it true.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, File, Request, Response, UploadFile, status
from fastapi.exceptions import HTTPException

from tailorcraft.application.intake.upload_base_cv import UploadBaseCvCommand
from tailorcraft.domain.identity.ownership import GuestOwner
from tailorcraft.domain.intake.base_cv import BaseCv
from tailorcraft.domain.intake.value_objects import BaseCvId
from tailorcraft.domain.shared.errors import DomainError
from tailorcraft.infrastructure.api.deps import (
    BaseCvRepositoryDep,
    ClockDep,
    GetBaseCvDep,
    GuestSessionRepositoryDep,
    ListBaseCvsDep,
    RateLimiterDep,
    RequireGuestSessionDep,
    SessionDep,
    SettingsDep,
    StartGuestSessionDep,
    UploadBaseCvDep,
    resolve_or_start_guest_session,
)
from tailorcraft.infrastructure.api.errors import domain_error_to_http_exception
from tailorcraft.infrastructure.api.routers._upload import (
    commit_or_503,
    failure_message,
    read_validated_upload,
)
from tailorcraft.infrastructure.api.schemas.intake import (
    BaseCvListResponse,
    BaseCvResponse,
    ErrorResponse,
)
from tailorcraft.infrastructure.rate_limit import client_ip
from tailorcraft.infrastructure.settings import Settings

router = APIRouter(prefix="/api/base-cvs", tags=["intake"])

# 1.1's name for `failure_message`, bound here as a module attribute (an import alias would not be
# one under mypy's `implicit_reexport = False`): 1.1's API tests import `_failure_message` from this
# module, and T17 moved the function without editing them (R-6).
_failure_message = failure_message

# Shared `responses=` fragments, so the same failure mode is documented with the same shape at every
# handler that can produce it, rather than four independently-typed copies of `{"model":
# ErrorResponse}` drifting apart over the life of the router. Typed `dict[str, Any]` on the value
# because that is FastAPI's own type for one entry of its `responses=` mapping (it accepts a
# `type[BaseModel]` under "model" and a `str` under "description" in the same dict) — the `Any` is
# FastAPI's API, not a shortcut taken here.
_GUEST_SESSION_EXPIRED: dict[int | str, dict[str, Any]] = {
    status.HTTP_401_UNAUTHORIZED: {
        "model": ErrorResponse,
        "description": "guest_session_expired — missing, unknown or expired `tc_guest` cookie.",
    },
}


# ---------------------------------------------------------------------------------------------
# Response shaping. A pure function, no I/O. The upload boundary itself — the capped read, the
# threaded sniff, the in-handler commit, the failure messages — lives in `routers/_upload.py`, shared
# with `routers/saved_base_cvs.py` (T17).
# ---------------------------------------------------------------------------------------------


def _to_response(cv: BaseCv, expires_at: datetime, settings: Settings) -> BaseCvResponse:
    """Build the wire shape from a saved `BaseCv` plus the *session's* `expires_at` — the session,
    not the row, owns the 24-hour promise (technical-plan.md's API contract)."""
    return BaseCvResponse(
        id=cv.id.value,
        original_filename=cv.original_filename.value,
        content_type=cv.content_type,
        size_bytes=cv.size_bytes,
        status=cv.status,
        character_count=cv.extracted_text.character_count
        if cv.extracted_text is not None
        else None,
        failure_reason=cv.failure_reason,
        failure_message=(
            failure_message(cv.failure_reason, settings) if cv.failure_reason is not None else None
        ),
        uploaded_at=cv.uploaded_at,
        expires_at=expires_at,
    )


# ---------------------------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------------------------


@router.post(
    "",
    response_model=BaseCvResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ErrorResponse,
            "description": "missing_file | empty_file | invalid_filename",
        },
        status.HTTP_413_CONTENT_TOO_LARGE: {
            "model": ErrorResponse,
            "description": "file_too_large — at 10,485,760 bytes. Answered by "
            "`MaxBodySizeMiddleware` before the body is read for a client that declares "
            "`Content-Length`; by `read_capped` in-handler otherwise (chunked transfer-encoding).",
        },
        status.HTTP_415_UNSUPPORTED_MEDIA_TYPE: {
            "model": ErrorResponse,
            "description": "unsupported_format — decided by the file's own bytes, never the "
            "filename or the client's Content-Type.",
        },
        status.HTTP_409_CONFLICT: {
            "model": ErrorResponse,
            "description": "too_many_base_cvs — the session already owns the per-session cap.",
        },
        status.HTTP_429_TOO_MANY_REQUESTS: {
            "model": ErrorResponse,
            "description": "rate_limited — carries a Retry-After header.",
        },
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ErrorResponse,
            "description": "storage_unavailable | service_unavailable",
        },
    },
)
async def upload_base_cv(
    request: Request,
    response: Response,
    file: Annotated[UploadFile, File(description="The CV file: PDF, DOCX or TXT, at most 10 MB.")],
    settings: SettingsDep,
    clock: ClockDep,
    sessions: GuestSessionRepositoryDep,
    start_guest_session: StartGuestSessionDep,
    rate_limiter: RateLimiterDep,
    upload_use_case: UploadBaseCvDep,
    cvs: BaseCvRepositoryDep,
    db: SessionDep,
) -> BaseCvResponse:
    """Upload one base CV for the caller's guest session.

    Multipart only, one part named `file`, no JSON body. A missing `file` part never reaches this
    function at all: FastAPI raises `RequestValidationError` first, and `main.py`'s handler for it is
    what renders F-1's 422 `missing_file` — see that handler's docstring for why this router cannot
    do it here (the two known conflicts this task's brief calls out).

    Tolerates a missing, unknown or expired `tc_guest` cookie by minting a fresh `GuestSession` and
    returning it via `Set-Cookie` (F-17/F-18) — unlike the two read endpoints below, this never
    answers 401 for a bad cookie.
    """
    # `resolve_or_start_guest_session` is called directly, not injected via `Depends()` — see its
    # docstring in `deps.py` for the empirically-verified reason (F-1: this must not run, and must
    # not mutate anything, on a request with no `file` part; calling it here guarantees it only runs
    # once FastAPI has already confirmed `file` is present, because otherwise this function body
    # would never have started executing at all).
    session = await resolve_or_start_guest_session(
        request, response, sessions, clock, settings, start_guest_session
    )

    # Rate limiting gates the expensive part of this request (reading, sniffing and extracting the
    # file) — checked, and both counters incremented, before any of that work starts (F-24). Both
    # scopes are always checked, not short-circuited on the first: a client that is over its
    # per-session limit has still made an attempt from its IP, and that attempt should count.
    session_decision = await rate_limiter.check(
        "session", str(session.id.value), settings.upload_rate_limit_per_hour
    )
    ip_decision = await rate_limiter.check(
        "ip",
        client_ip(request, settings.trusted_proxy_hops),
        settings.upload_rate_limit_per_ip_per_hour,
    )
    if not session_decision.allowed or not ip_decision.allowed:
        retry_after = max(session_decision.retry_after_seconds, ip_decision.retry_after_seconds)
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "code": "rate_limited",
                "message": f"Too many uploads. Try again in {retry_after} seconds.",
            },
            headers={"Retry-After": str(retry_after)},
        )

    # Filename → capped read → empty → sniff (in a worker thread), 1.1's order and codes. After the
    # limiter above, so the limit gates the expensive part. See `routers/_upload.py`.
    upload = await read_validated_upload(file, settings)

    command = UploadBaseCvCommand(
        owner=GuestOwner(session.id),
        original_filename=upload.original_filename,
        content_type=upload.content_type,
        content=upload.content,
    )
    try:
        result = await upload_use_case(command)
    except DomainError as exc:
        raise domain_error_to_http_exception(exc) from exc

    # `UploadBaseCvResult` deliberately does not carry every field `BaseCvResponse` needs
    # (application/intake/upload_base_cv.py is not this task's to change) — fetching the saved
    # aggregate back through the repository is infrastructure's business and gives an answer that is
    # guaranteed to match exactly what was persisted, rather than a second, hand-assembled copy of
    # the same facts this function already computed locally.
    saved_cv = await cvs.get(result.base_cv_id)
    wire = _to_response(saved_cv, session.expires_at, settings)

    # Commit HERE, not in `get_session`'s teardown — which FastAPI 0.141 runs after the response is
    # sent, so only an in-handler commit can still become F-15's 503. See `commit_or_503`.
    await commit_or_503(db)

    return wire


@router.get(
    "",
    response_model=BaseCvListResponse,
    responses=_GUEST_SESSION_EXPIRED,
)
async def list_base_cvs(
    session: RequireGuestSessionDep,
    list_use_case: ListBaseCvsDep,
    settings: SettingsDep,
) -> BaseCvListResponse:
    """Every `BaseCv` the caller's guest session owns, or `items: []` — never a 404 for "none yet".

    Unlike `POST`, a missing, unknown or expired `tc_guest` cookie is **not** forgiven here: it is a
    401 `guest_session_expired` (F-19), so the client can tell "you have no CVs" from "your session
    is gone" and react to each differently.
    """
    try:
        cvs = await list_use_case(session.id)
    except DomainError as exc:
        raise domain_error_to_http_exception(exc) from exc

    return BaseCvListResponse(items=[_to_response(cv, session.expires_at, settings) for cv in cvs])


@router.get(
    "/{base_cv_id}",
    response_model=BaseCvResponse,
    responses={
        status.HTTP_404_NOT_FOUND: {
            "model": ErrorResponse,
            "description": "base_cv_not_found — also returned for a CV owned by a different "
            "session (F-20); never a 403, which would confirm the id exists (ADR-0008).",
        },
        **_GUEST_SESSION_EXPIRED,
    },
)
async def get_base_cv(
    base_cv_id: UUID,
    session: RequireGuestSessionDep,
    get_use_case: GetBaseCvDep,
    settings: SettingsDep,
) -> BaseCvResponse:
    """One `BaseCv`, authorized by the link to the caller's guest session.

    `base_cv_id` is typed `UUID` so a malformed id is FastAPI's own 422 rather than something this
    handler has to reject by hand. A well-formed id that names another session's CV is
    indistinguishable from one that does not exist at all: both are 404 (F-20/AC-8).
    """
    try:
        cv = await get_use_case(BaseCvId(base_cv_id), session.id)
    except DomainError as exc:
        raise domain_error_to_http_exception(exc) from exc

    return _to_response(cv, session.expires_at, settings)
