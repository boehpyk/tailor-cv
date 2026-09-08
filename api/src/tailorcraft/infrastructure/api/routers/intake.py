"""The `intake` HTTP surface: upload a base CV, list a guest session's base CVs, read one.

**This router is red-first** (CLAUDE.md, sdlc.md §2), unlike the rest of `infrastructure`: its
contract — paths, status codes, request/response schemas, and every row of
`docs/specs/intake-base-cv-upload/feature-spec.md`'s failure contract — comes from the plan, not from
FastAPI. So this module is built in three passes: **SKELETON** (this one — real signatures, real
schemas, `NotImplementedError` bodies), **RED** (`qa` writes the failing API tests against these
exact signatures), **GREEN** (the boundary validation, the rate limit and the `DomainError` -> status
translation, until those tests pass without editing them).

Every documented failure mode is declared in each handler's `responses=` map, even though nothing
raises it yet. The OpenAPI schema this produces is the frontend's contract (ADR-0001) — leaving a
415 or a 429 out of it here would make that contract quietly wrong for as long as this skeleton
stands, and `qa`'s tests are what SKELETON exists to make honest.
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, File, UploadFile, status

from tailorcraft.infrastructure.api.schemas.intake import (
    BaseCvListResponse,
    BaseCvResponse,
    ErrorResponse,
)

router = APIRouter(prefix="/api/base-cvs", tags=["intake"])

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


@router.post(
    "",
    response_model=BaseCvResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        status.HTTP_422_UNPROCESSABLE_ENTITY: {
            "model": ErrorResponse,
            "description": "missing_file | empty_file | invalid_filename",
        },
        status.HTTP_413_CONTENT_TOO_LARGE: {
            "model": ErrorResponse,
            "description": "file_too_large — aborted while streaming, at 10,485,760 bytes.",
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
    file: Annotated[UploadFile, File(description="The CV file: PDF, DOCX or TXT, at most 10 MB.")],
) -> BaseCvResponse:
    """Upload one base CV for the caller's guest session.

    Multipart only, one part named `file`, no JSON body. Tolerates a missing, unknown or expired
    `tc_guest` cookie by minting a fresh `GuestSession` and returning it via `Set-Cookie` (F-17/F-18)
    — unlike the two read endpoints below, a `POST` never answers 401 for a bad cookie.

    Boundary validation (size cap enforced while streaming, content sniffing, filename
    sanitization), the rate limit, and cookie resolution are **not implemented here** — that is T26.
    """
    raise NotImplementedError


@router.get(
    "",
    response_model=BaseCvListResponse,
    responses=_GUEST_SESSION_EXPIRED,
)
async def list_base_cvs() -> BaseCvListResponse:
    """Every `BaseCv` the caller's guest session owns, or `items: []` — never a 404 for "none yet".

    Unlike `POST`, a missing, unknown or expired `tc_guest` cookie is **not** forgiven here: it is a
    401 `guest_session_expired` (F-19), so the client can tell "you have no CVs" from "your session
    is gone" and react to each differently.
    """
    raise NotImplementedError


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
async def get_base_cv(base_cv_id: UUID) -> BaseCvResponse:
    """One `BaseCv`, authorized by the link to the caller's guest session.

    `base_cv_id` is typed `UUID` so a malformed id is FastAPI's own 422 rather than something this
    handler has to reject by hand. A well-formed id that names another session's CV is
    indistinguishable from one that does not exist at all: both are 404 (F-20/AC-8).
    """
    raise NotImplementedError
