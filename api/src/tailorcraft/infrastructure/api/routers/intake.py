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
from typing import Annotated, Any, Final
from uuid import UUID

import structlog
from fastapi import APIRouter, File, Request, Response, UploadFile, status

from tailorcraft.application.identity.resolve_existing_user import resolve_existing_user
from tailorcraft.application.intake.owned_saved_base_cv import get_owned_saved_base_cv
from tailorcraft.application.intake.upload_base_cv import UploadBaseCvCommand
from tailorcraft.domain.identity.errors import UserNotFound
from tailorcraft.domain.identity.ownership import GuestOwner
from tailorcraft.domain.intake.base_cv import BaseCv
from tailorcraft.domain.intake.errors import SavedBaseCvFileMissing
from tailorcraft.domain.intake.value_objects import BaseCvId
from tailorcraft.domain.shared.errors import DomainError
from tailorcraft.infrastructure.api.deps import (
    BaseCvRepositoryDep,
    ClockDep,
    CopySavedBaseCvDep,
    GetBaseCvDep,
    GuestSessionRepositoryDep,
    ListBaseCvsDep,
    RateLimiterDep,
    RequireGuestSessionDep,
    RequireUserDep,
    SessionDep,
    SettingsDep,
    StartGuestSessionDep,
    UploadBaseCvDep,
    UserRepositoryDep,
    resolve_or_start_guest_session,
)
from tailorcraft.infrastructure.api.errors import domain_error_to_http_exception
from tailorcraft.infrastructure.api.routers._upload import (
    commit_or_503,
    enforce_upload_limit,
    failure_message,
    read_validated_upload,
)
from tailorcraft.infrastructure.api.schemas.intake import (
    BaseCvListResponse,
    BaseCvResponse,
    CopySavedBaseCvRequest,
    ErrorResponse,
)
from tailorcraft.infrastructure.settings import Settings

router = APIRouter(prefix="/api/base-cvs", tags=["intake"])
log = structlog.get_logger(__name__)

EVENT_USER_MISSING: Final = "identity.user_missing"
EVENT_SAVED_BASE_CV_FILE_MISSING: Final = "intake.saved_base_cv_file_missing"

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
        origin=cv.origin,
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
    await enforce_upload_limit(request, rate_limiter, "session", str(session.id.value), settings)

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


@router.post(
    "/copies",
    response_model=BaseCvResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        status.HTTP_401_UNAUTHORIZED: {
            "model": ErrorResponse,
            "description": (
                'invalid_access_token (+ WWW-Authenticate: Bearer error="invalid_token") | '
                "not_signed_in — the bearer is required; on either, no guest session is minted "
                "and no cookie is set."
            ),
        },
        status.HTTP_404_NOT_FOUND: {
            "model": ErrorResponse,
            "description": (
                "base_cv_not_found — the source is not the bearer's (another user's, a guest's) or "
                "does not exist; byte-identical either way."
            ),
        },
        status.HTTP_409_CONFLICT: {
            "model": ErrorResponse,
            "description": (
                "base_cv_not_extracted — the source's extraction did not succeed | "
                "too_many_base_cvs — the guest session is at its cap."
            ),
        },
        status.HTTP_410_GONE: {
            "model": ErrorResponse,
            "description": (
                "saved_base_cv_file_gone — the saved CV's row exists but its file does not; delete "
                "it and upload it again."
            ),
        },
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ErrorResponse,
            "description": "validation_error — a malformed body; no guest session is minted.",
        },
        status.HTTP_429_TOO_MANY_REQUESTS: {
            "model": ErrorResponse,
            "description": (
                "rate_limited — the upload limiter's `session` and `ip` scopes; carries a "
                "Retry-After header."
            ),
        },
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ErrorResponse,
            "description": "storage_unavailable | service_unavailable",
        },
    },
)
async def copy_saved_base_cv(
    body: CopySavedBaseCvRequest,
    request: Request,
    response: Response,
    user_id: RequireUserDep,
    users: UserRepositoryDep,
    settings: SettingsDep,
    clock: ClockDep,
    sessions: GuestSessionRepositoryDep,
    start_guest_session: StartGuestSessionDep,
    rate_limiter: RateLimiterDep,
    copy_use_case: CopySavedBaseCvDep,
    cvs: BaseCvRepositoryDep,
    db: SessionDep,
) -> BaseCvResponse:
    """Copy one of the bearer's saved CVs into this browser's workspace as a guest working copy.

    **The transfer route** (ADR-0008 (f), AC-24): the one route that answers to the bearer **and**
    reaches `tc_guest`. The bearer is a `Depends` — it runs before the body, so a bad one mints
    nothing (S-25). The guest session is **not**: `resolve_or_start_guest_session` is called in this
    handler's body, after the source is authorized, never as a sibling dependency — 1.1's T26 lesson,
    siblings run even when the body fails validation (S-26, S-27). A missing or expired `tc_guest` is
    forgiven by minting one (1.1's POST rule, S-29).

    201 with the guest `BaseCvResponse`: `status: "extracted"`, `expires_at` the session's. The copy
    never re-runs the extractor (AC-9) and is deliberately not idempotent — two clicks, two copies
    (S-36).

    **Order, and why each step sits where it does** (technical plan §0.3):

    1. The bearer (`require_user`, a dependency) — before the body: a bad one mints nothing.
    2. **The source is authorized here, before any guest session exists**: the account still exists
       (a token outliving an erasure is 401 `not_signed_in`, AC-30) and the saved CV is the
       bearer's (404 otherwise, byte-identical for "not yours" and "not there"). The application's
       own two helpers, not a second copy of the rule — the use case repeats them inside its own
       unit of work, which is cheap and keeps the use case whole for any other entry point.
    3. `resolve_or_start_guest_session` — only now, so a 401 or a 404 never mints a session and
       never sets a cookie (S-25, S-27).
    4. The upload limiter's `session` and `ip` scopes (fail open): a copy creates a guest CV, as an
       upload does, and the session scope needs the session from step 3.
    5. The use case, then the persisted aggregate re-read for the response, then **the commit,
       inside this handler** (FastAPI 0.141 runs `get_session`'s teardown after the response is
       sent, so only an in-handler commit can still become S-35's 503).
    """
    # (2) Authorize the source. Never the guest half yet — see the docstring.
    source_id = BaseCvId(body.saved_base_cv_id)
    try:
        await resolve_existing_user(users, user_id)
        await get_owned_saved_base_cv(cvs, source_id, user_id)
    except UserNotFound as exc:
        log.info(EVENT_USER_MISSING, user_id=str(user_id.value))
        raise domain_error_to_http_exception(exc) from None
    except DomainError as exc:
        raise domain_error_to_http_exception(exc) from None

    # (3) The transfer route's one direct read of `tc_guest` (ADR-0008 (f), AC-24's AST scan).
    session = await resolve_or_start_guest_session(
        request, response, sessions, clock, settings, start_guest_session
    )

    # (4) Both scopes are always checked, never short-circuited — `upload_base_cv`'s reason.
    await enforce_upload_limit(request, rate_limiter, "session", str(session.id.value), settings)

    # (5)
    try:
        result = await copy_use_case(source_id, user_id, session.id)
    except SavedBaseCvFileMissing as exc:
        # S-32: a row that points at nothing is a bug somewhere, so it is a warning — ids only.
        log.warning(
            EVENT_SAVED_BASE_CV_FILE_MISSING,
            base_cv_id=str(source_id.value),
            user_id=str(user_id.value),
        )
        raise domain_error_to_http_exception(exc) from None
    except DomainError as exc:
        raise domain_error_to_http_exception(exc) from None

    saved_copy = await cvs.get(result.base_cv_id)
    wire = _to_response(saved_copy, session.expires_at, settings)

    await commit_or_503(db)

    response.headers["Cache-Control"] = "no-store"
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
        cv = await get_use_case(BaseCvId(base_cv_id), GuestOwner(session.id))
    except DomainError as exc:
        raise domain_error_to_http_exception(exc) from exc

    return _to_response(cv, session.expires_at, settings)
