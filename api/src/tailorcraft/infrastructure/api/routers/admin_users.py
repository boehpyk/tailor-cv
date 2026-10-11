"""The administrator's user directory and account actions (slice 4.2, technical plan §0.7, §0.8,
§3 "Router", §4; ADR-0033).

Built red-first (sdlc.md §2): **SKELETON** (T13), **RED** (T14, `qa`), **GREEN** (T15).

**Its own router, the same firewall** (§0.8): `require_admin` is a router-level dependency here as
in `admin.py`, and 4.1's structural walk pins it on every route under `/api/admin`, whichever router
holds it. So the firewall's refusals come first on every route, before any path or query parameter
is validated (AC-19): a non-admin gets the 404 byte-identical to an unmatched path, never a 422.

**A missing target is 404 `user_not_found`, translated here, not in `errors.py`** (§0.7). The global
map turns `UserNotFound` and `AccountNotFound` into 401 `not_signed_in` — "*your* account is gone".
Here the account that is gone is someone else's, and a 401 would sign the administrator out. The
*actor's* `UserNotFound` never reaches a handler: `require_admin` turns it into the 401 first.

Every response a handler builds carries `Cache-Control: no-store`. No `Origin` check (bearer-only)
and no rate limit (OQ-10).
"""

from __future__ import annotations

from typing import Annotated, Any, Final
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response, status

from tailorcraft.domain.identity.user_directory import PAGE_SIZE_DEFAULT, PAGE_SIZE_MAX
from tailorcraft.infrastructure.api.deps import (
    AdminDep,
    DisableAccountDep,
    EnableAccountDep,
    EraseAccountDep,
    GetAccountOverviewDep,
    ListUsersDep,
    SessionDep,
    require_admin,
)
from tailorcraft.infrastructure.api.routers._me_responses import (
    NOT_SIGNED_IN,
    SERVICE_UNAVAILABLE,
    VALIDATION_ERROR,
)
from tailorcraft.infrastructure.api.schemas.admin import (
    AccountErasureResponse,
    AccountStatusChangeResponse,
    AdminUserDetailResponse,
    AdminUserPageResponse,
)
from tailorcraft.infrastructure.api.schemas.intake import ErrorResponse

router = APIRouter(prefix="/api/admin/users", tags=["admin"], dependencies=[Depends(require_admin)])

# Raw bounds at the boundary (plan §4). `q` is parsed to `EmailSearch`, whose own bound (254) is
# the one a person meets as 422 `invalid_search`; this one only stops an absurd query string early.
_SEARCH_MAX_LENGTH: Final = 512
_CURSOR_MAX_LENGTH: Final = 256

ACCOUNT_DISABLED: dict[int | str, dict[str, Any]] = {
    status.HTTP_403_FORBIDDEN: {
        "model": ErrorResponse,
        "description": "account_disabled — the administrator's own account is disabled.",
    },
}

USER_NOT_FOUND: dict[int | str, dict[str, Any]] = {
    status.HTTP_404_NOT_FOUND: {
        "model": ErrorResponse,
        "description": (
            "user_not_found — no account with that id (including one erased concurrently)."
        ),
    },
}

_FIREWALL: dict[int | str, dict[str, Any]] = {
    **NOT_SIGNED_IN,
    **ACCOUNT_DISABLED,
    **SERVICE_UNAVAILABLE,
}


@router.get(
    "",
    response_model=AdminUserPageResponse,
    responses={
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ErrorResponse,
            "description": (
                "invalid_search — `q` over 254 characters once normalized, or a control character "
                "| invalid_cursor — a malformed or tampered cursor | validation_error — `limit` "
                "outside 1…50, or `q`/`cursor` over the raw bound. Neither `q` nor the cursor is "
                "ever echoed or logged."
            ),
        },
        **_FIREWALL,
    },
)
async def list_users(
    response: Response,
    actor: AdminDep,
    list_users: ListUsersDep,
    db: SessionDep,
    q: Annotated[str | None, Query(max_length=_SEARCH_MAX_LENGTH)] = None,
    cursor: Annotated[str | None, Query(max_length=_CURSOR_MAX_LENGTH)] = None,
    limit: Annotated[int, Query(ge=1, le=PAGE_SIZE_MAX)] = PAGE_SIZE_DEFAULT,
) -> AdminUserPageResponse:
    """One keyset page of every account, newest first, optionally filtered by an email substring.
    `next_cursor` is `null` on the last page; no match is `{"items": [], "next_cursor": null}`."""
    raise NotImplementedError


@router.get(
    "/{user_id}",
    response_model=AdminUserDetailResponse,
    responses={**USER_NOT_FOUND, **VALIDATION_ERROR, **_FIREWALL},
)
async def get_user(
    user_id: UUID,
    response: Response,
    actor: AdminDep,
    overview: GetAccountOverviewDep,
    db: SessionDep,
) -> AdminUserDetailResponse:
    """One account and what erasing it would delete."""
    raise NotImplementedError


@router.post(
    "/{user_id}/disable",
    response_model=AccountStatusChangeResponse,
    responses={**USER_NOT_FOUND, **VALIDATION_ERROR, **_FIREWALL},
)
async def disable_user(
    user_id: UUID,
    response: Response,
    actor: AdminDep,
    disable_account: DisableAccountDep,
    db: SessionDep,
) -> AccountStatusChangeResponse:
    """Disable the account and delete every login it holds; idempotent (`changed: false`)."""
    raise NotImplementedError


@router.post(
    "/{user_id}/enable",
    response_model=AccountStatusChangeResponse,
    responses={**USER_NOT_FOUND, **VALIDATION_ERROR, **_FIREWALL},
)
async def enable_user(
    user_id: UUID,
    response: Response,
    actor: AdminDep,
    enable_account: EnableAccountDep,
    db: SessionDep,
) -> AccountStatusChangeResponse:
    """Enable the account; resurrects no login. Idempotent; `logins_revoked` is always 0."""
    raise NotImplementedError


@router.delete(
    "/{user_id}",
    response_model=AccountErasureResponse,
    responses={**USER_NOT_FOUND, **VALIDATION_ERROR, **_FIREWALL},
)
async def erase_user(
    user_id: UUID,
    response: Response,
    actor: AdminDep,
    erase_account: EraseAccountDep,
    db: SessionDep,
) -> AccountErasureResponse:
    """Erase the account: rows committed, then files (2.2's `EraseAccount`). A failed unlink is
    counted in `unlink_failures` and left for the orphan sweep; it never fails the request."""
    raise NotImplementedError
