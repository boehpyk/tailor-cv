"""The `identity` HTTP surface: register, log in, refresh, log out, and "who am I" (technical plan §4).

**SKELETON (T28).** Real paths, real status codes, real schemas and real dependencies; every handler
body raises `NotImplementedError` until T30 makes `qa`'s T29 tests pass.

**One credential per route (AC-30).** The four `POST`s answer to the refresh cookie and a trusted
`Origin`; `/me` answers to a bearer token. **None of them depends on `require_guest_session`**, and
none reads or writes `tc_guest` (AC-29) — a guest session is not a weak login, and a login is not a
guest session. A test walks the dependency graph to keep that true.

**The order of checks in every `POST` is the contract's, and it is load-bearing** (T30):

1. `require_trusted_origin` — in the decorator's `dependencies=`, which FastAPI resolves before the
   route's own parameters and before body validation, so a foreign `Origin` is 403 before the
   limiter, the database or the hasher is touched (AC-25). **One measured exception:** bytes that
   are not JSON at all are refused 422 `validation_error` by FastAPI *before* any dependency runs —
   it decodes the body first and raises on a `JSONDecodeError` immediately. A well-formed body of
   the wrong shape, an empty body and a non-JSON content type all get the 403 first. Nothing is
   touched on either path, so AC-25's guarantee holds; only the status differs.
2. The rate limiter (login and register only), **before** the use case, so a 429 or a 503 computes
   no hash (AC-27).
3. The use case — the only step that hashes, reads or writes.
4. The commit, **inside the handler**, and only then the cookie: a commit that fails after the
   response is on the wire would hand the browser a cookie for a login that does not exist (the
   `posting` router's reason for committing in the handler), and a logout must not clear a cookie
   the server still honours (I-31).
5. `Cache-Control: no-store` on every response that carries a token or sets the cookie (AC-26).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request, Response, status

from tailorcraft.infrastructure.api.deps import (
    ClockDep,
    GetCurrentUserDep,
    LogInDep,
    LoginEmailRateLimiterDep,
    LoginIpRateLimiterDep,
    LogOutDep,
    RefreshLoginDep,
    RegisterRateLimiterDep,
    RegisterUserDep,
    RequireUserDep,
    SessionDep,
    SettingsDep,
    require_trusted_origin,
)
from tailorcraft.infrastructure.api.schemas.auth import (
    AuthenticatedResponse,
    CredentialsRequest,
    UserResponse,
)
from tailorcraft.infrastructure.api.schemas.intake import ErrorResponse

router = APIRouter(prefix="/api/auth", tags=["identity"])

# `dict[str, Any]` is FastAPI's own type for a `responses=` entry (see `routers/posting.py`); the
# `Any` is FastAPI's API, not a shortcut.
_ORIGIN_NOT_ALLOWED: dict[int | str, dict[str, Any]] = {
    status.HTTP_403_FORBIDDEN: {
        "model": ErrorResponse,
        "description": "origin_not_allowed — missing or foreign `Origin` header (AC-25).",
    },
}
_RATE_LIMITED: dict[int | str, dict[str, Any]] = {
    status.HTTP_429_TOO_MANY_REQUESTS: {
        "model": ErrorResponse,
        "description": "rate_limited — carries a Retry-After header.",
    },
}
_SERVICE_UNAVAILABLE_LIMITED: dict[int | str, dict[str, Any]] = {
    status.HTTP_503_SERVICE_UNAVAILABLE: {
        "model": ErrorResponse,
        "description": (
            "rate_limit_unavailable (the limiter fails closed: Redis down, no hash computed) | "
            "service_unavailable"
        ),
    },
}
_SERVICE_UNAVAILABLE: dict[int | str, dict[str, Any]] = {
    status.HTTP_503_SERVICE_UNAVAILABLE: {
        "model": ErrorResponse,
        "description": "service_unavailable",
    },
}


@router.post(
    "/register",
    response_model=AuthenticatedResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_trusted_origin)],
    responses={
        **_ORIGIN_NOT_ALLOWED,
        status.HTTP_409_CONFLICT: {
            "model": ErrorResponse,
            "description": "email_already_registered (conceded enumeration, OQ-1)",
        },
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ErrorResponse,
            "description": (
                "invalid_email | password_too_short (+min_length) | password_too_long "
                "(+max_length) | password_matches_email | validation_error"
            ),
        },
        **_RATE_LIMITED,
        **_SERVICE_UNAVAILABLE_LIMITED,
    },
)
async def register(
    body: CredentialsRequest,
    request: Request,
    response: Response,
    register_user: RegisterUserDep,
    rate_limiter: RegisterRateLimiterDep,
    session: SessionDep,
    clock: ClockDep,
    settings: SettingsDep,
) -> AuthenticatedResponse:
    """Create an account and sign its owner in: 201 + `Set-Cookie: tc_refresh`."""
    raise NotImplementedError


@router.post(
    "/login",
    response_model=AuthenticatedResponse,
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(require_trusted_origin)],
    responses={
        **_ORIGIN_NOT_ALLOWED,
        status.HTTP_401_UNAUTHORIZED: {
            "model": ErrorResponse,
            "description": (
                "invalid_credentials — an unknown email and a wrong password are byte-identical "
                "(AC-28)."
            ),
        },
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ErrorResponse,
            "description": "invalid_email | validation_error",
        },
        **_RATE_LIMITED,
        **_SERVICE_UNAVAILABLE_LIMITED,
    },
)
async def login(
    body: CredentialsRequest,
    request: Request,
    response: Response,
    log_in: LogInDep,
    ip_rate_limiter: LoginIpRateLimiterDep,
    email_rate_limiter: LoginEmailRateLimiterDep,
    session: SessionDep,
    clock: ClockDep,
    settings: SettingsDep,
) -> AuthenticatedResponse:
    """Check a password and start a new login: 200 + `Set-Cookie: tc_refresh`."""
    raise NotImplementedError


@router.post(
    "/refresh",
    response_model=AuthenticatedResponse,
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(require_trusted_origin)],
    responses={
        **_ORIGIN_NOT_ALLOWED,
        status.HTTP_401_UNAUTHORIZED: {
            "model": ErrorResponse,
            "description": (
                "not_signed_in (+ clears the cookie, unless there was none) | "
                "refresh_token_reused (+ clears the cookie; the login is revoked)"
            ),
        },
        status.HTTP_409_CONFLICT: {
            "model": ErrorResponse,
            "description": "refresh_in_progress — another tab won the race; cookie untouched.",
        },
        **_SERVICE_UNAVAILABLE,
    },
)
async def refresh(
    request: Request,
    response: Response,
    refresh_login: RefreshLoginDep,
    session: SessionDep,
    clock: ClockDep,
    settings: SettingsDep,
) -> AuthenticatedResponse:
    """Rotate the refresh cookie and mint a new access token: 200 + rotated `Set-Cookie`. No body,
    and **no rate limiter and no Redis** — a Redis outage must never sign anybody out (I-18)."""
    raise NotImplementedError


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    dependencies=[Depends(require_trusted_origin)],
    responses={**_ORIGIN_NOT_ALLOWED, **_SERVICE_UNAVAILABLE},
)
async def logout(
    request: Request,
    response: Response,
    log_out: LogOutDep,
    session: SessionDep,
    settings: SettingsDep,
) -> None:
    """End the login the cookie names, if any: 204 + a cleared cookie, idempotently. **No access
    token required** — logging out must work precisely when the access token has expired."""
    raise NotImplementedError


@router.get(
    "/me",
    response_model=UserResponse,
    status_code=status.HTTP_200_OK,
    responses={
        status.HTTP_401_UNAUTHORIZED: {
            "model": ErrorResponse,
            "description": (
                'invalid_access_token (+ WWW-Authenticate: Bearer error="invalid_token") | '
                "not_signed_in (the user no longer exists)"
            ),
        },
        **_SERVICE_UNAVAILABLE,
    },
)
async def me(
    response: Response,
    user_id: RequireUserDep,
    get_current_user: GetCurrentUserDep,
) -> UserResponse:
    """The account the bearer token speaks for. No `Origin` check and no limiter: it is authorized
    by a header a cross-site request cannot attach, and it is one stateless verify and one read."""
    raise NotImplementedError
