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

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, File, Request, Response, UploadFile, status

from tailorcraft.domain.intake.base_cv import BaseCv
from tailorcraft.domain.intake.saved_base_cv_summary import SavedBaseCvSummary
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
from tailorcraft.infrastructure.api.routers._upload import failure_message
from tailorcraft.infrastructure.api.schemas.intake import (
    ErrorResponse,
    RenameSavedBaseCvRequest,
    SavedBaseCvListResponse,
    SavedBaseCvResponse,
)
from tailorcraft.infrastructure.settings import Settings

router = APIRouter(prefix="/api/me/base-cvs", tags=["intake"])

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
    """Every saved CV the bearer's account owns, newest first; `items: []` for none (S-13)."""
    raise NotImplementedError


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
    """
    raise NotImplementedError


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
    """Set or clear (`null`) a saved CV's label. Two renames at once: the later one stands (S-17)."""
    raise NotImplementedError


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
    """
    raise NotImplementedError
