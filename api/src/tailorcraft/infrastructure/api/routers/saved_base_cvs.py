"""The saved-base-CV HTTP surface: a registered user's own CVs, kept until they delete them (slice 2.2).

Built red-first (sdlc.md §2): **SKELETON** (T18, this file — real paths, real schemas, every documented
status in each `responses=` map, handlers raising `NotImplementedError`), **RED** (T19, `qa`), **GREEN**
(T21 — boundary validation, the rate limit, the domain-error translation and the in-handler commit,
until T19 passes without an edit to it).

**One credential: the bearer** (`require_user`). A `tc_guest` cookie riding along changes nothing
(AC-21), and nothing in this module reads it — the one route that answers to both credentials is the
copy, `POST /api/base-cvs/copies` in `routers/intake.py` (ADR-0008 (f), AC-24).

**Why `/api/me/…`** (technical plan §4): the path says whose resource it is. `/api/base-cvs` stays "this
browser's workspace"; `/api/me/base-cvs` is "my account's CVs". Two resources, two owners, two
credentials.

Every response carries `Cache-Control: no-store` (technical plan §4): these bodies describe a person's
documents under an authenticated identity, and no shared cache should ever hold one.

The upload shares 1.1's boundary through `routers/_upload.py` (T17) — the capped read, the threaded
sniff, the in-handler commit and the failure messages — rather than a copy of it.
"""

from __future__ import annotations

from typing import Annotated, Any, Final
from uuid import UUID

import structlog
from fastapi import APIRouter, File, Request, Response, UploadFile, status
from fastapi.exceptions import HTTPException

from tailorcraft.application.intake.upload_base_cv import UploadBaseCvCommand
from tailorcraft.domain.identity.errors import UserNotFound
from tailorcraft.domain.identity.ownership import UserOwner
from tailorcraft.domain.identity.value_objects import UserId
from tailorcraft.domain.intake.base_cv import BaseCv
from tailorcraft.domain.intake.errors import TooManySavedBaseCvs
from tailorcraft.domain.intake.saved_base_cv_summary import SavedBaseCvSummary
from tailorcraft.domain.intake.value_objects import BaseCvId, BaseCvLabel
from tailorcraft.domain.shared.errors import DomainError
from tailorcraft.infrastructure.api.deps import (
    BaseCvRepositoryDep,
    DeleteSavedBaseCvDep,
    ListSavedBaseCvsDep,
    RateLimiterDep,
    RenameSavedBaseCvDep,
    RequireUserDep,
    SessionDep,
    SettingsDep,
    UploadBaseCvDep,
)
from tailorcraft.infrastructure.api.errors import domain_error_to_http_exception
from tailorcraft.infrastructure.api.routers._upload import (
    commit_or_503,
    enforce_upload_limit,
    failure_message,
    read_validated_upload,
)
from tailorcraft.infrastructure.api.schemas.intake import (
    ErrorResponse,
    RenameSavedBaseCvRequest,
    SavedBaseCvListResponse,
    SavedBaseCvResponse,
)
from tailorcraft.infrastructure.settings import Settings

router = APIRouter(prefix="/api/me/base-cvs", tags=["intake"])
log = structlog.get_logger(__name__)

EVENT_USER_MISSING: Final = "identity.user_missing"
EVENT_UPLOAD_REFUSED: Final = "intake.upload_refused"
EVENT_FILE_UNLINK_FAILED: Final = "intake.saved_base_cv_file_unlink_failed"

_NO_STORE: Final = "no-store"

# `dict[str, Any]` is FastAPI's own type for a `responses=` entry (see `routers/intake.py`); the `Any`
# is FastAPI's API, not a shortcut.
_NOT_SIGNED_IN: dict[int | str, dict[str, Any]] = {
    status.HTTP_401_UNAUTHORIZED: {
        "model": ErrorResponse,
        "description": (
            'invalid_access_token (+ WWW-Authenticate: Bearer error="invalid_token") — missing, '
            "expired or forged bearer | not_signed_in — a valid bearer whose account is gone."
        ),
    },
}
_SERVICE_UNAVAILABLE: dict[int | str, dict[str, Any]] = {
    status.HTTP_503_SERVICE_UNAVAILABLE: {
        "model": ErrorResponse,
        "description": "service_unavailable",
    },
}
_NOT_FOUND: dict[int | str, dict[str, Any]] = {
    status.HTTP_404_NOT_FOUND: {
        "model": ErrorResponse,
        "description": (
            "base_cv_not_found — also for another user's CV and for a guest-owned CV, "
            "byte-identical to a nonexistent id (AC-22); never a 403, which would confirm the id "
            "exists (ADR-0008)."
        ),
    },
}


# ---------------------------------------------------------------------------------------------
# Response shaping — pure functions, no I/O. Two sources, one key set (AC-20).
# ---------------------------------------------------------------------------------------------


def _summary_to_response(summary: SavedBaseCvSummary, settings: Settings) -> SavedBaseCvResponse:
    """The list's entry, from the read model (technical plan §3 amendment): no text was loaded."""
    return SavedBaseCvResponse(
        id=summary.id.value,
        label=summary.label.value if summary.label is not None else None,
        original_filename=summary.original_filename.value,
        content_type=summary.content_type,
        size_bytes=summary.size_bytes,
        status=summary.status,
        character_count=summary.character_count,
        failure_reason=summary.failure_reason,
        failure_message=(
            failure_message(summary.failure_reason, settings)
            if summary.failure_reason is not None
            else None
        ),
        uploaded_at=summary.uploaded_at,
    )


def _aggregate_to_response(cv: BaseCv, settings: Settings) -> SavedBaseCvResponse:
    """After an upload or a rename, from the aggregate the use case returned or saved."""
    return SavedBaseCvResponse(
        id=cv.id.value,
        label=cv.label.value if cv.label is not None else None,
        original_filename=cv.original_filename.value,
        content_type=cv.content_type,
        size_bytes=cv.size_bytes,
        status=cv.status,
        character_count=(
            cv.extracted_text.character_count if cv.extracted_text is not None else None
        ),
        failure_reason=cv.failure_reason,
        failure_message=(
            failure_message(cv.failure_reason, settings) if cv.failure_reason is not None else None
        ),
        uploaded_at=cv.uploaded_at,
    )


def _no_store(response: Response) -> None:
    """`Cache-Control: no-store` (technical plan §4, AC-51): a person's documents under an
    authenticated identity, which no shared or browser cache should keep."""
    response.headers["Cache-Control"] = _NO_STORE


def _translate(exc: DomainError, user_id: UserId) -> HTTPException:
    """A domain refusal as this router's `HTTPException`, plus the one line S-2 names.

    `UserNotFound` is a valid token for an account that is gone (S-2, and AC-30 after an erasure):
    2.1's 401 `not_signed_in`, logged as 2.1 logs it on `/me` — the id, nothing else. It is also
    what the repository raises when the account is erased between the upload's user check and its
    INSERT (S-12's FK refusal): the same fact, the same answer, and a line that cannot tell the two
    apart, deliberately — the error carries nothing that could, and a message is never parsed.
    """
    if isinstance(exc, UserNotFound):
        log.info(EVENT_USER_MISSING, user_id=str(user_id.value))
    return domain_error_to_http_exception(exc)


def _too_many_saved_base_cvs(settings: Settings) -> HTTPException:
    """S-4's 409, naming the cap. The shared mapping in `errors.py` cannot: the error carries no
    limit, and this handler is the one place holding the `Settings` that decided it."""
    cap = settings.max_saved_base_cvs_per_user
    noun = "CV" if cap == 1 else "CVs"
    return HTTPException(
        status.HTTP_409_CONFLICT,
        detail={
            "code": "too_many_saved_base_cvs",
            "message": f"You can keep up to {cap} saved {noun}. Delete one to upload another.",
        },
    )


# ---------------------------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------------------------


@router.get(
    "",
    response_model=SavedBaseCvListResponse,
    status_code=status.HTTP_200_OK,
    responses={**_NOT_SIGNED_IN, **_SERVICE_UNAVAILABLE},
)
async def list_saved_base_cvs(
    response: Response,
    user_id: RequireUserDep,
    list_use_case: ListSavedBaseCvsDep,
    settings: SettingsDep,
    db: SessionDep,
) -> SavedBaseCvListResponse:
    """Every saved CV the bearer's account owns, newest first; `items: []` for none (S-13).

    Built from `SavedBaseCvSummary` (technical plan §3 amendment): no CV text is ever loaded to
    answer this. Writes nothing and commits anyway — 2.1's `/me` reason: `get_session`'s commit runs
    after the response is on the wire, so a failing database would otherwise answer 200.
    """
    try:
        summaries = await list_use_case(user_id)
    except DomainError as exc:
        raise _translate(exc, user_id) from None

    wire = SavedBaseCvListResponse(
        items=[_summary_to_response(summary, settings) for summary in summaries]
    )
    await db.commit()
    _no_store(response)
    return wire


@router.post(
    "",
    response_model=SavedBaseCvResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        **_NOT_SIGNED_IN,
        status.HTTP_409_CONFLICT: {
            "model": ErrorResponse,
            "description": (
                "too_many_saved_base_cvs — the account already keeps the per-user cap; the message "
                "names it. Checked before the file is written."
            ),
        },
        status.HTTP_413_CONTENT_TOO_LARGE: {
            "model": ErrorResponse,
            "description": "file_too_large — 1.1's boundary, unchanged.",
        },
        status.HTTP_415_UNSUPPORTED_MEDIA_TYPE: {
            "model": ErrorResponse,
            "description": (
                "unsupported_format — decided by the file's own bytes, never the filename or the "
                "client's Content-Type."
            ),
        },
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ErrorResponse,
            "description": "missing_file | empty_file | invalid_filename",
        },
        status.HTTP_429_TOO_MANY_REQUESTS: {
            "model": ErrorResponse,
            "description": (
                "rate_limited — the upload limiter's `user` and `ip` scopes (fails open); carries "
                "a Retry-After header."
            ),
        },
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ErrorResponse,
            "description": "storage_unavailable | service_unavailable",
        },
    },
)
async def upload_saved_base_cv(
    request: Request,
    response: Response,
    file: Annotated[UploadFile, File(description="The CV file: PDF, DOCX or TXT, at most 10 MB.")],
    user_id: RequireUserDep,
    settings: SettingsDep,
    rate_limiter: RateLimiterDep,
    upload_use_case: UploadBaseCvDep,
    cvs: BaseCvRepositoryDep,
    db: SessionDep,
) -> SavedBaseCvResponse:
    """Upload one base CV to the bearer's account: 1.1's boundary, a `UserOwner`, the per-user cap.

    201 with extraction already decided — `extracted`, or `extraction_failed` with the server-owned
    `failure_message` (AC-25, S-8).

    The order is 1.1's, with the owner swapped: the bearer (a dependency) → the upload limiter's
    `user` and `ip` scopes (fail open) → 1.1's boundary (filename, capped read, empty, the threaded
    sniff) → `UploadBaseCv` with a `UserOwner` (account gone → 401 `not_signed_in`; cap → 409,
    checked before the file is written) → the persisted aggregate re-read → **the commit, here**.
    """
    await enforce_upload_limit(request, rate_limiter, "user", str(user_id.value), settings)

    upload = await read_validated_upload(file, settings)

    command = UploadBaseCvCommand(
        owner=UserOwner(user_id),
        original_filename=upload.original_filename,
        content_type=upload.content_type,
        content=upload.content,
    )
    try:
        result = await upload_use_case(command)
    except TooManySavedBaseCvs:
        # S-4. `reason=cap` and the id — never the filename the user was trying to add.
        log.info(EVENT_UPLOAD_REFUSED, reason="cap", user_id=str(user_id.value))
        raise _too_many_saved_base_cvs(settings) from None
    except DomainError as exc:
        raise _translate(exc, user_id) from None

    saved_cv = await cvs.get(result.base_cv_id)
    wire = _aggregate_to_response(saved_cv, settings)

    # FastAPI 0.141 runs `get_session`'s teardown after the response is sent: only this commit can
    # still become S-10's 503. The file is already on disk; on failure it is the sweep's orphan.
    await commit_or_503(db)

    _no_store(response)
    return wire


@router.patch(
    "/{base_cv_id}",
    response_model=SavedBaseCvResponse,
    status_code=status.HTTP_200_OK,
    responses={
        **_NOT_SIGNED_IN,
        **_NOT_FOUND,
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ErrorResponse,
            "description": (
                "invalid_label — every `BaseCvLabel` refusal (empty after trimming, over 80 "
                "characters, a control character) | validation_error — a malformed id or body, or "
                "a label over the 200-character wire cap."
            ),
        },
        **_SERVICE_UNAVAILABLE,
    },
)
async def rename_saved_base_cv(
    base_cv_id: UUID,
    body: RenameSavedBaseCvRequest,
    response: Response,
    user_id: RequireUserDep,
    rename_use_case: RenameSavedBaseCvDep,
    settings: SettingsDep,
    db: SessionDep,
) -> SavedBaseCvResponse:
    """Set or clear (`null`) a saved CV's label. Two renames at once: the later one stands (S-17).

    The label is parsed into `BaseCvLabel` here, at the boundary, so every refusal (empty after
    trimming, over 80, a control character) is the domain's own 422 `invalid_label` with a fixed
    sentence — never the text the user typed. The schema's looser 200 cap is FastAPI's
    `validation_error`, before this body runs.
    """
    try:
        label = BaseCvLabel(body.label) if body.label is not None else None
        cv = await rename_use_case(BaseCvId(base_cv_id), user_id, label)
    except DomainError as exc:
        raise _translate(exc, user_id) from None

    wire = _aggregate_to_response(cv, settings)
    await db.commit()
    _no_store(response)
    return wire


@router.delete(
    "/{base_cv_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    responses={**_NOT_SIGNED_IN, **_NOT_FOUND, **_SERVICE_UNAVAILABLE},
)
async def delete_saved_base_cv(
    base_cv_id: UUID,
    response: Response,
    user_id: RequireUserDep,
    delete_use_case: DeleteSavedBaseCvDep,
    db: SessionDep,
) -> None:
    """Delete a saved CV: the row, **committed**, then the file (AC-27, technical plan §0.4).

    204 even when the unlink fails after the commit (S-24): the row — the only thing the user can see
    or reach — is gone, and a retry could only 404. That case is a warning line for the operator.

    The row's commit is not this handler's: the composition root binds the delete to
    `CommittingBaseCvRemoval`, whose `remove` commits **before** the use case unlinks — a failure
    there is a `SQLAlchemyError`, `main.py`'s 503, and the unlink never ran (S-20). The commit below
    only closes the unit of work inside the handler, for the teardown-after-response reason.
    """
    try:
        result = await delete_use_case(BaseCvId(base_cv_id), user_id)
    except DomainError as exc:
        raise _translate(exc, user_id) from None

    if not result.file_unlinked:
        # S-24 / R-5: the row is gone, the bytes are not. An orphan for the operator's sweep, and
        # this line is how the operator learns there is one. Ids and a class name — never the key,
        # never the path, never the exception's message.
        log.warning(
            EVENT_FILE_UNLINK_FAILED,
            base_cv_id=str(base_cv_id),
            user_id=str(user_id.value),
            error_type=result.unlink_error_type,
        )

    await db.commit()
    _no_store(response)
