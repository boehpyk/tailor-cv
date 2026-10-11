"""The administrator's user directory and account actions (slice 4.2, technical plan §0.7, §0.8,
§3 "Router", §4; ADR-0033).

Built red-first (sdlc.md §2): **SKELETON** (T13), **RED** (T14, `qa`), **GREEN** (T15).

**Its own router, the same firewall** (§0.8): `require_admin` is a router-level dependency here as
in `admin.py`, and 4.1's structural walk pins it on every route under `/api/admin`, whichever router
holds it. So the firewall's refusals come first on every route, before any path or query parameter
is validated (AC-19): a non-admin gets the 404 byte-identical to an unmatched path, never a 422.

**A missing target is 404 `user_not_found`, translated by `_admin_responses.user_not_found`, not
in `errors.py`** (§0.7). The global map turns `UserNotFound` and `AccountNotFound` into 401
`not_signed_in` — "*your* account is gone". Here the account that is gone is someone else's, and a
401 would sign the administrator out. The *actor's* `UserNotFound` never reaches a handler:
`require_admin` turns it into the 401 first.

Every response a handler builds carries `Cache-Control: no-store`. No `Origin` check (bearer-only)
and no rate limit (OQ-10).
"""

from __future__ import annotations

from typing import Annotated, Any, Final
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, Query, Response, status

from tailorcraft.application.identity.results import AccountStatusChange
from tailorcraft.domain.identity.errors import InvalidUserDirectoryCursor, UserNotFound
from tailorcraft.domain.identity.user import User
from tailorcraft.domain.identity.user_directory import (
    PAGE_SIZE_DEFAULT,
    PAGE_SIZE_MAX,
    EmailSearch,
    UserDirectoryCursor,
)
from tailorcraft.domain.identity.value_objects import UserId
from tailorcraft.domain.retention.errors import AccountNotFound
from tailorcraft.domain.shared.errors import DomainError
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
from tailorcraft.infrastructure.api.routers._admin_responses import (
    FIREWALL,
    USER_NOT_FOUND,
    refusal,
    set_no_store,
    user_not_found,
)
from tailorcraft.infrastructure.api.routers._erasure_log import log_erasure
from tailorcraft.infrastructure.api.routers._keyset_cursor import decode_keyset, encode_keyset
from tailorcraft.infrastructure.api.routers._me_responses import VALIDATION_ERROR
from tailorcraft.infrastructure.api.schemas.admin import (
    AccountErasureResponse,
    AccountStatusChangeResponse,
    AdminAccountCounts,
    AdminUserDetailResponse,
    AdminUserPageResponse,
    AdminUserSummary,
)
from tailorcraft.infrastructure.api.schemas.intake import ErrorResponse

log = structlog.get_logger(__name__)

EVENT_ADMIN_ACCOUNT_ACTION: Final = "admin.account_action"
_CURSOR_REFUSAL: Final = "not a user directory cursor"

router = APIRouter(prefix="/api/admin/users", tags=["admin"], dependencies=[Depends(require_admin)])

# Raw bounds at the boundary (plan §4). `q` is parsed to `EmailSearch`, whose own bound (254) is
# the one a person meets as 422 `invalid_search`; this one only stops an absurd query string early.
_SEARCH_MAX_LENGTH: Final = 512
_CURSOR_MAX_LENGTH: Final = 256


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
        **FIREWALL,
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
    `next_cursor` is `null` on the last page; no match is `{"items": [], "next_cursor": null}`.
    Neither `q` nor the cursor is ever logged or echoed (AC-18)."""
    set_no_store(response)
    try:
        search = EmailSearch.parse(q) if q is not None else None
        after = _decode_cursor(cursor) if cursor is not None else None
        page = await list_users(search, after, limit)
    except DomainError as exc:
        raise refusal(exc) from None

    wire = AdminUserPageResponse(
        items=[
            AdminUserSummary(
                id=entry.id.value,
                email=entry.email.value,
                role=entry.role,
                created_at=entry.created_at,
                disabled_at=entry.disabled_at,
            )
            for entry in page.entries
        ],
        next_cursor=(
            encode_keyset(page.next_cursor.created_at, page.next_cursor.user_id.value)
            if page.next_cursor is not None
            else None
        ),
    )
    await db.commit()
    return wire


@router.get(
    "/{user_id}",
    response_model=AdminUserDetailResponse,
    responses={**USER_NOT_FOUND, **VALIDATION_ERROR, **FIREWALL},
)
async def get_user(
    user_id: UUID,
    response: Response,
    actor: AdminDep,
    overview: GetAccountOverviewDep,
    db: SessionDep,
) -> AdminUserDetailResponse:
    """One account and what erasing it would delete."""
    set_no_store(response)
    try:
        found = await overview(UserId(user_id))
    except (UserNotFound, AccountNotFound):
        raise user_not_found() from None
    except DomainError as exc:
        raise refusal(exc) from None

    counts = found.counts
    wire = AdminUserDetailResponse(
        **_summary_fields(found.user),
        counts=AdminAccountCounts(
            base_cvs=counts.base_cvs,
            files=counts.files,
            logins=counts.logins,
            tailoring_runs=counts.tailoring_runs,
            job_postings=counts.job_postings,
            export_jobs=counts.export_jobs,
            tracked_applications=counts.tracked_applications,
        ),
    )
    await db.commit()
    return wire


@router.post(
    "/{user_id}/disable",
    response_model=AccountStatusChangeResponse,
    responses={**USER_NOT_FOUND, **VALIDATION_ERROR, **FIREWALL},
)
async def disable_user(
    user_id: UUID,
    response: Response,
    actor: AdminDep,
    disable_account: DisableAccountDep,
    db: SessionDep,
) -> AccountStatusChangeResponse:
    """Disable the account and delete every login it holds; idempotent (`changed: false`)."""
    set_no_store(response)
    target = UserId(user_id)
    try:
        change = await disable_account(target)
    except (UserNotFound, AccountNotFound):
        _log_action(actor, target, "disable", "not_found")
        raise user_not_found() from None
    except DomainError as exc:
        raise refusal(exc) from None

    await db.commit()
    _log_action(actor, target, "disable", "changed" if change.changed else "unchanged")
    return _status_change(change)


@router.post(
    "/{user_id}/enable",
    response_model=AccountStatusChangeResponse,
    responses={**USER_NOT_FOUND, **VALIDATION_ERROR, **FIREWALL},
)
async def enable_user(
    user_id: UUID,
    response: Response,
    actor: AdminDep,
    enable_account: EnableAccountDep,
    db: SessionDep,
) -> AccountStatusChangeResponse:
    """Enable the account; resurrects no login. Idempotent; `logins_revoked` is always 0."""
    set_no_store(response)
    target = UserId(user_id)
    try:
        change = await enable_account(target)
    except (UserNotFound, AccountNotFound):
        _log_action(actor, target, "enable", "not_found")
        raise user_not_found() from None
    except DomainError as exc:
        raise refusal(exc) from None

    await db.commit()
    _log_action(actor, target, "enable", "changed" if change.changed else "unchanged")
    return _status_change(change)


@router.delete(
    "/{user_id}",
    response_model=AccountErasureResponse,
    responses={**USER_NOT_FOUND, **VALIDATION_ERROR, **FIREWALL},
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
    set_no_store(response)
    target = UserId(user_id)
    try:
        report = await erase_account(target)
    except (UserNotFound, AccountNotFound):
        # Includes the loser of two concurrent erasures (AC-23).
        _log_action(actor, target, "erase", "not_found")
        raise user_not_found() from None
    except DomainError as exc:
        raise refusal(exc) from None

    # 2.2's lines, byte-identical to `delete-account`'s (plan §3).
    log_erasure(target, report)
    await db.commit()
    _log_action(actor, target, "erase", "changed")
    return AccountErasureResponse(
        base_cvs=report.base_cvs,
        files=report.files,
        files_unlinked=report.files_unlinked,
        unlink_failures=len(report.unlink_failures),
        tailoring_runs=report.tailoring_runs,
        job_postings=report.job_postings,
        export_jobs=report.export_jobs,
        tracked_applications=report.tracked_applications,
    )


def _decode_cursor(raw: str) -> UserDirectoryCursor:
    """The directory's cursor from `?cursor=`; `InvalidUserDirectoryCursor` on any failure. A fixed
    sentence, raised `from None`: the cursor is never echoed or logged."""
    try:
        created_at, user_id = decode_keyset(raw)
    except ValueError:
        raise InvalidUserDirectoryCursor(_CURSOR_REFUSAL) from None
    return UserDirectoryCursor(created_at=created_at, user_id=UserId(user_id))


def _summary_fields(user: User) -> dict[str, Any]:
    return {
        "id": user.id.value,
        "email": user.email.value,
        "role": user.role,
        "created_at": user.created_at,
        "disabled_at": user.disabled_at,
    }


def _status_change(change: AccountStatusChange) -> AccountStatusChangeResponse:
    return AccountStatusChangeResponse(
        user_id=change.user_id.value,
        disabled_at=change.disabled_at,
        changed=change.changed,
        logins_revoked=change.logins_revoked,
    )


def _log_action(actor: User, target: UserId, action: str, outcome: str) -> None:
    """AC-27: one line per admin action — two ids and two closed words, never an email."""
    log.info(
        EVENT_ADMIN_ACCOUNT_ACTION,
        actor_id=str(actor.id.value),
        target_user_id=str(target.value),
        action=action,
        outcome=outcome,
    )
