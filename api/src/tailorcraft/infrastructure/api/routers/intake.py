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

import asyncio
from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, File, Request, Response, UploadFile, status
from fastapi.exceptions import HTTPException
from sqlalchemy.exc import SQLAlchemyError

from tailorcraft.application.intake.upload_base_cv import UploadBaseCvCommand
from tailorcraft.domain.intake.base_cv import BaseCv
from tailorcraft.domain.intake.value_objects import (
    BaseCvId,
    ExtractionFailureReason,
    OriginalFilename,
)
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
from tailorcraft.infrastructure.api.schemas.intake import (
    BaseCvListResponse,
    BaseCvResponse,
    ErrorResponse,
)
from tailorcraft.infrastructure.intake.sniffing import sniff_cv_content_type
from tailorcraft.infrastructure.rate_limit import client_ip
from tailorcraft.infrastructure.settings import Settings

router = APIRouter(prefix="/api/base-cvs", tags=["intake"])

# Read the already-received upload in fixed-size chunks and count as we go, rather than joining it
# into one second copy first and measuring that — a memory bound within this handler, not a network
# one. `MaxBodySizeMiddleware` (infrastructure/api/middleware.py) is what aborts a streaming request
# before it lands; see `_read_capped`'s own docstring for why the two are not the same guarantee.
_UPLOAD_CHUNK_BYTES = 64 * 1024

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
# Boundary helpers — response shaping and failure messages. Pure functions, no I/O, so they need no
# fixture of their own to reason about.
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
            _failure_message(cv.failure_reason, settings) if cv.failure_reason is not None else None
        ),
        uploaded_at=cv.uploaded_at,
        expires_at=expires_at,
    )


def _failure_message(reason: ExtractionFailureReason, settings: Settings) -> str:
    """One user-facing sentence per `ExtractionFailureReason` — server-owned, so the client never
    re-implements this mapping (technical-plan.md's API contract).

    `NO_TEXT_LAYER`'s message names OCR as **absent, not as coming** (F-9): feature-spec.md's
    non-goals are explicit that OCR is "not a feature request answered here", so a message that reads
    as a promise would be a claim this product does not intend to keep.
    """
    if reason is ExtractionFailureReason.ENCRYPTED:
        return (
            "This PDF is password-protected, so we could not read it. "
            "Remove the password and upload it again."
        )
    if reason is ExtractionFailureReason.CORRUPT:
        return (
            "This file looks damaged or incomplete and could not be read. "
            "Try re-exporting it and uploading it again."
        )
    if reason is ExtractionFailureReason.NO_TEXT_LAYER:
        return (
            "We saved your file, but couldn't find any text in it — it looks like a scan. "
            "We don't support OCR, so try a text-based PDF, or paste your CV as a .txt file instead."
        )
    if reason is ExtractionFailureReason.TOO_SHORT:
        return (
            "We could only read a small amount of text from this file — not enough to work with. "
            "Make sure the file contains your full CV."
        )
    if reason is ExtractionFailureReason.TOO_MANY_PAGES:
        return (
            f"This PDF has more than {settings.max_cv_pages} pages, which is more than we can "
            "process. Try a shorter version of your CV."
        )
    # ExtractionFailureReason.EXTRACTOR_ERROR — the catch-all, reached two ways: the 10 s timeout
    # (F-12) and, since `PypdfDocxTextExtractor` grew its catch-all, any library failure we have no
    # better name for (a `KeyError` out of `pypdf` on a mangled cross-reference table, say). The
    # earlier wording here — "we couldn't read this file in time" — described only the first of
    # those, and would have been a confidently wrong diagnosis for the second, which is now the
    # commoner one. It names both causes rather than retreating into "something went wrong":
    # "too long" and "gave up part-way" are different things a user can act on differently, and
    # "part-way" is also what distinguishes this sentence from CORRUPT's "looks damaged" — that one
    # is a verdict on the file, this one is an admission about us.
    return (
        "We couldn't read this file — it either took too long or our reader gave up part-way "
        "through. Try again, or use a different PDF, DOCX or TXT file."
    )


async def _read_capped(file: UploadFile, max_bytes: int) -> bytes:
    """Read `file` back from FastAPI's already-parsed `UploadFile` in `_UPLOAD_CHUNK_BYTES` chunks,
    aborting as soon as the running total exceeds `max_bytes`, rather than joining the whole thing
    into one `bytes` object first and measuring it afterward.

    **This does not bound what crosses the network** — despite what an earlier version of this
    docstring claimed. Declaring `file: Annotated[UploadFile, File(...)]` on the handler above hands
    multipart parsing to the framework *before this function, or even the handler body, starts
    running*: FastAPI resolves that parameter by awaiting `request.form()` during dependency
    resolution, and Starlette's `MultiPartParser` drains the entire request body into a
    `SpooledTemporaryFile` while doing it. By the time this loop's first `file.read()` returns, the
    whole upload has already been received and spooled — this function is reading bytes back off
    disk/memory, not bytes still arriving on the wire. Verified empirically: an 11 MB upload against
    a 10 MB cap transferred all 11,000,202 bytes before this loop's 413 fired.

    So what actually guarantees what:

    * `MaxBodySizeMiddleware` (`infrastructure/api/middleware.py`), reading `Content-Length` before
      FastAPI ever calls `receive()`, is what rejects an honest client's request while it is still
      streaming — the guarantee this docstring used to (wrongly) claim for this function.
    * nginx's `client_max_body_size` bounds a client that lies about `Content-Length` or omits it
      outright, upstream of this process entirely.
    * **This function's actual job** is narrower: bound how much of an *already-received* body this
      handler holds in memory at once, rather than materializing a second full copy via
      `await file.read()` with no size argument. It also remains the only line of defense against a
      chunked-transfer-encoding request, which carries no `Content-Length` for the middleware to see
      coming (F-3/AC-2's "aborted while streaming" is achieved by the middleware for the common case;
      this loop is the fallback for the case the middleware structurally cannot catch).
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(_UPLOAD_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(
                status.HTTP_413_CONTENT_TOO_LARGE,
                detail={
                    "code": "file_too_large",
                    "message": f"The file exceeds the {max_bytes}-byte limit.",
                },
            )
        chunks.append(chunk)
    return b"".join(chunks)


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
            "`Content-Length`; by `_read_capped` in-handler otherwise (chunked transfer-encoding).",
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

    # Filename sanitization (AC-7): `OriginalFilename.__post_init__` reduces to a basename and
    # rejects anything that cannot be a display label — never joined to a path anywhere.
    try:
        original_filename = OriginalFilename(file.filename or "")
    except DomainError as exc:
        raise domain_error_to_http_exception(exc) from exc

    # The streaming abort (F-3/AC-2) already happened, if it was going to: `MaxBodySizeMiddleware`
    # ran ahead of routing and would have answered 413 before this handler was even invoked. This is
    # the in-memory-bound read over a body FastAPI has already received — see `_read_capped`'s own
    # docstring for exactly what it does and does not guarantee.
    data = await _read_capped(file, settings.max_upload_bytes)

    if not data:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": "empty_file", "message": "The uploaded file is empty."},
        )

    # Sniffing decides the type — never the client's Content-Type header, never the filename
    # extension (F-4/F-5/F-6/AC-3/AC-4).
    #
    # In a worker thread, for the same reason `LocalFileStore` and `PypdfDocxTextExtractor` use one
    # (Constitution §1, ADR-0009): `sniff_cv_content_type` is synchronous and CPU-bound, and its cost
    # is chosen by the *uploader*. Opening a zip reads its whole central directory, so an archive of
    # 100,000 tiny entries — 8.6 MB, comfortably inside the 10 MB cap — measured 340 ms in the
    # container, and 120,000 entries 410 ms. On the event loop that is a stall for every concurrent
    # user of every endpoint, and it presents as "the app is slow" rather than as an error, which is
    # why §1 grades it CRITICAL rather than as a style note.
    #
    # The 30/hour/IP limiter above already ran, so the damage is bounded rather than unbounded. That
    # is a cost ceiling, not a licence: a rate limit bounds how *often* the loop is stalled, never
    # whether it is stalled, and thirty 400 ms stalls per IP per hour are still thirty stalls.
    content_type = await asyncio.to_thread(sniff_cv_content_type, data)
    if content_type is None:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail={
                "code": "unsupported_format",
                "message": "Unsupported file format. Please upload a PDF, DOCX or TXT file.",
            },
        )

    command = UploadBaseCvCommand(
        guest_session_id=session.id,
        original_filename=original_filename,
        content_type=content_type,
        content=data,
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

    # Commit HERE rather than leaving it to `get_session`'s teardown, and this is not belt-and-braces
    # -- it is the only place a failed commit can still change the answer.
    #
    # FastAPI runs the exit half of a yield-dependency AFTER the response has been sent (documented
    # behaviour since 0.106). So `get_session`'s post-yield `commit()` fires with the 201 already on
    # the wire: if it raises, Starlette finds `response_started == True` and the client keeps the
    # 201 it was already given. F-15 -- "the commit fails after the file was written, the API answers
    # 503" -- is unreachable from there, no matter what exception handler is installed.
    #
    # Committing inside the handler's own error boundary puts the failure back where it can be
    # translated. The teardown commit still runs and is then a no-op on an already-committed session,
    # so nothing about the unit-of-work boundary changes for any other route.
    try:
        await db.commit()
    except SQLAlchemyError as exc:
        await db.rollback()
        # The row is gone; the file that step 5 of the use case already wrote is not. That orphan is
        # the deliberate survivor of this crash window (ADR-0006 §2) -- a directory sweep can reclaim
        # it, whereas the alternative ordering leaves a row pointing at bytes that never arrived.
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "service_unavailable",
                "message": "Could not save your CV just now. Please try again.",
            },
        ) from exc

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
