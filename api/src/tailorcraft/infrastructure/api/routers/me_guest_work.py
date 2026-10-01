"""Claiming a guest's work into the signed-in account (slice 2.4, ADR-0025, technical plan §3).

Built red-first (sdlc.md §2): **SKELETON** (T19, this file — the real path, the real schema, every
documented status in `responses=`, a handler raising `NotImplementedError`), **RED** (T20, `qa`),
**GREEN** (T22 — the limiter, the in-body cookie read, the domain-error translation, the in-handler
end of the unit of work and the cookie clear, until T20 passes without an edit to it).

**A transfer route, reversed** (ADR-0008 amendment (g)): the **bearer** authorizes the destination and
is a dependency (`require_user` — stateless, runs first, so a bad bearer is a 401 before anything
touches the guest session table); the **`tc_guest` cookie** names the source and is read **in the
handler body**, never through `require_guest_session` (its 401 would break idempotency) and never
through `resolve_or_start_guest_session` (which mints). **This route never mints a guest session.**

**The skeleton calls nothing on purpose**: the AST scan over `routers/*.py` pins the set of routes
whose bodies touch the guest cookie to `{POST /api/base-cvs/copies}` until T20 amends it, so a body
that already called `read_guest_token` or `clear_guest_cookie` would turn this commit red.

No body, no query parameters: nothing to validate, so no 422 is possible (C-19). Every response
carries `Cache-Control: no-store`.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request, Response, status

from tailorcraft.domain.identity.value_objects import UserId
from tailorcraft.infrastructure.api.deps import (
    ClaimGuestWorkDep,
    ClaimRateLimiterDep,
    RequireUserDep,
    SessionDep,
    SettingsDep,
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
    open** (the limiter is built with `fail_open=True`), or 429 `rate_limited` + `Retry-After`."""
    raise NotImplementedError


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
    `service_unavailable` — none of which sets or clears a cookie."""
    raise NotImplementedError
