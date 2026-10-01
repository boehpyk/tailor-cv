"""Claiming a guest's work into the signed-in account (slice 2.4, ADR-0025, technical plan §3).

Built red-first (sdlc.md §2): **SKELETON** (T19), **RED** (T20, `qa`), **GREEN** (T22 — the limiter,
the in-body cookie read, the domain-error translation, the in-handler end of the unit of work and the
cookie clear).

**A transfer route, reversed** (ADR-0008 amendment (g)): the **bearer** authorizes the destination and
is a dependency (`require_user` — stateless, runs first, so a bad bearer is a 401 before anything
touches the guest session table); the **`tc_guest` cookie** names the source and is read **in the
handler body**, never through `require_guest_session` (its 401 would break idempotency) and never
through `resolve_or_start_guest_session` (which mints). **This route never mints a guest session.**
The AST scan over `routers/*.py` pins the routes whose bodies touch the guest cookie to
`{POST /api/base-cvs/copies, POST /api/me/guest-work/claim}`.

No body, no query parameters: nothing to validate, so no 422 is possible (C-19). Every response the
handler builds carries `Cache-Control: no-store`.

**Logging** (AC-46): one `identity.guest_work_claimed` line per claim, the user id and the five counts
— never the guest session id, the token, its hash, a file key or a path — and one
`identity.guest_work_file_unlink_failed` warning per failed unlink, by exception class name (C-31).
"""

from __future__ import annotations

from typing import Any, Final

import structlog
from fastapi import APIRouter, Request, Response, status
from fastapi.exceptions import HTTPException
from sqlalchemy.exc import SQLAlchemyError

from tailorcraft.domain.identity.errors import UserNotFound
from tailorcraft.domain.identity.value_objects import UserId
from tailorcraft.infrastructure.api.deps import (
    ClaimGuestWorkDep,
    ClaimRateLimiterDep,
    RequireUserDep,
    SessionDep,
    SettingsDep,
)
from tailorcraft.infrastructure.api.errors import domain_error_to_http_exception
from tailorcraft.infrastructure.api.guest_session import (
    clear_guest_cookie,
    hash_guest_token,
    read_guest_token,
)
from tailorcraft.infrastructure.api.routers._me_responses import (
    NOT_SIGNED_IN,
    RATE_LIMITED,
    SERVICE_UNAVAILABLE,
)
from tailorcraft.infrastructure.api.schemas.guest_work import GuestWorkClaimResponse
from tailorcraft.infrastructure.rate_limit import RedisFixedWindowRateLimiter
from tailorcraft.infrastructure.settings import Settings

router = APIRouter(prefix="/api/me/guest-work", tags=["identity"])
log = structlog.get_logger(__name__)

EVENT_USER_MISSING: Final = "identity.user_missing"
EVENT_GUEST_WORK_CLAIMED: Final = "identity.guest_work_claimed"
EVENT_FILE_UNLINK_FAILED: Final = "identity.guest_work_file_unlink_failed"

_NO_STORE: Final = "no-store"

# `dict[str, Any]` is FastAPI's own type for a `responses=` entry (see `_me_responses.py`); the `Any`
# is FastAPI's API, not a shortcut.
_CLAIM_RESPONSES: dict[int | str, dict[str, Any]] = {
    **NOT_SIGNED_IN,
    **RATE_LIMITED,
    **SERVICE_UNAVAILABLE,
}


async def enforce_claim_limit(
    limiter: RedisFixedWindowRateLimiter, user_id: UserId, settings: Settings
) -> None:
    """The claim limiter: `user` scope at `guest_work_claim_rate_limit_per_hour` (10/h), **fails
    open** (the limiter is built with `fail_open=True`), or 429 `rate_limited` + `Retry-After`.

    Keyed on the user **id**, never an email. Called before anything touches the database."""
    decision = await limiter.check(
        "user", str(user_id.value), settings.guest_work_claim_rate_limit_per_hour
    )
    if not decision.allowed:
        retry_after = decision.retry_after_seconds
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "code": "rate_limited",
                "message": f"Too many claims. Try again in {retry_after} seconds.",
            },
            headers={"Retry-After": str(retry_after), "Cache-Control": _NO_STORE},
        )


@router.post(
    "/claim",
    response_model=GuestWorkClaimResponse,
    status_code=status.HTTP_200_OK,
    responses=_CLAIM_RESPONSES,
)
async def claim_guest_work(
    request: Request,
    response: Response,
    user_id: RequireUserDep,
    limiter: ClaimRateLimiterDep,
    claim: ClaimGuestWorkDep,
    settings: SettingsDep,
    db: SessionDep,
) -> GuestWorkClaimResponse:
    """Move this browser's guest work into the account: **200** with the counts (all zeros when there
    was nothing to claim — no cookie, unknown, expired, already claimed), `tc_guest` cleared iff the
    request carried one; 401 `invalid_access_token` / `not_signed_in`; 429 `rate_limited`; 503
    `service_unavailable` — none of which sets or clears a cookie.

    **The unit of work ends in the handler** (FastAPI 0.141 runs a dependency's teardown after the
    response is sent): the bound `transfer` commits inside the use case, and `db.rollback()` here
    ends the transaction on every path — a no-op after that commit, and the release of the session
    row's `FOR UPDATE` on the early paths (expired, nothing to claim) and on a refusal. A failed
    commit is a `SQLAlchemyError`: rolled back here, then `main.py`'s 503 — before any cookie is
    touched, so a claim that did not land leaves the browser's guest session usable."""
    await enforce_claim_limit(limiter, user_id, settings)
    token = read_guest_token(request)  # in the body: ADR-0008 (g), never a dependency
    try:
        report = await claim(user_id, hash_guest_token(token) if token is not None else None)
    except UserNotFound as exc:
        log.info(EVENT_USER_MISSING, user_id=str(user_id.value))
        await db.rollback()
        refusal = domain_error_to_http_exception(exc)
        refusal.headers = {**(refusal.headers or {}), "Cache-Control": _NO_STORE}
        raise refusal from None
    except SQLAlchemyError:
        await db.rollback()
        raise
    await db.rollback()

    if token is not None:
        clear_guest_cookie(response, settings)
    log.info(
        EVENT_GUEST_WORK_CLAIMED,
        user_id=str(user_id.value),
        base_cvs=report.base_cvs,
        job_postings=report.job_postings,
        tailoring_runs=report.tailoring_runs,
        export_jobs=report.export_jobs,
        working_copies_dropped=report.working_copies_dropped,
    )
    for error_type in report.unlink_failures:
        log.warning(EVENT_FILE_UNLINK_FAILED, user_id=str(user_id.value), error_type=error_type)
    response.headers["Cache-Control"] = _NO_STORE
    return GuestWorkClaimResponse(
        base_cvs=report.base_cvs,
        job_postings=report.job_postings,
        tailoring_runs=report.tailoring_runs,
        export_jobs=report.export_jobs,
        working_copies_dropped=report.working_copies_dropped,
    )
