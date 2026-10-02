"""The `identity` HTTP surface: register, log in, refresh, log out, and "who am I" (technical plan §4).

Built red-first: T28's skeleton, `qa`'s T29 tests, then T30's handlers. Slice 2.5 adds three
`POST`s — confirm a registration, request a password reset, confirm it — at the end of this file,
skeleton at its T26 (the section comment there).

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

**Every outcome of a use case is committed before it is answered — refusals included** (`_commit`).
Two refusals *write*: a reuse revocation (I-24) and an expired login deleted on sight (I-21) both
remove a row and then raise. An exception escaping the handler reaches `get_session`'s `except`
branch, which rolls back — the deletion would vanish and the thief's login would survive a 401 that
claimed to have ended it. So those are caught here, **committed here**, and **returned** as a
`JSONResponse` rather than raised. The explicit commit is the mechanism: `get_session`'s own commit
runs only after the response has been sent, too late for a failure to change the answer. Returning
rather than raising keeps the dependency's teardown on its committing branch too, so no later edit
can turn it into a rollback of a revocation by removing the one line above it.

The refusals that write nothing are committed too, so a database that cannot commit answers 503
`service_unavailable` (I-41) rather than a confident 4xx about the user's input. A failed commit is
never caught here: it is a `SQLAlchemyError`, `main.py`'s handler renders the 503, and the dependency
rolls back.
"""

from __future__ import annotations

from typing import Any, Final

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from tailorcraft.application.identity.results import Authenticated
from tailorcraft.domain.identity.errors import (
    EmailAlreadyRegistered,
    InvalidCredentials,
    InvalidEmailAddress,
    LoginNotFound,
    RefreshInProgress,
    RefreshTokenReused,
    UserNotFound,
    WeakPassword,
)
from tailorcraft.domain.identity.login import Login
from tailorcraft.domain.identity.user import User
from tailorcraft.domain.identity.value_objects import (
    EmailAddress,
    LoginId,
    LoginNotFoundReason,
    Password,
    UserId,
)
from tailorcraft.domain.retention.errors import AccountNotFound
from tailorcraft.domain.retention.value_objects import AccountErasureReport
from tailorcraft.domain.shared.errors import DomainError
from tailorcraft.infrastructure.api.deps import (
    ConfirmRegistrationDep,
    DeleteOwnAccountDep,
    GetCurrentUserDep,
    LogInDep,
    LoginEmailRateLimiterDep,
    LoginIpRateLimiterDep,
    LogOutDep,
    PasswordResetEmailRateLimiterDep,
    PasswordResetIpRateLimiterDep,
    RefreshLoginDep,
    RegisterRateLimiterDep,
    RegisterUserDep,
    RequestPasswordResetDep,
    RequireUserDep,
    ResetPasswordDep,
    SessionDep,
    SettingsDep,
    UserRepositoryDep,
    login_email_rate_limit_identifier,
    require_trusted_origin,
)
from tailorcraft.infrastructure.api.errors import (
    NOT_SIGNED_IN_DETAIL,
    domain_error_to_http_exception,
)
from tailorcraft.infrastructure.api.refresh_cookie import (
    clear_refresh_cookie,
    mint_refresh_token,
    read_refresh_token,
    remaining_lifetime_seconds,
    set_refresh_cookie,
)
from tailorcraft.infrastructure.api.schemas.auth import (
    AuthenticatedResponse,
    CredentialsRequest,
    DeleteAccountRequest,
    EmailRequest,
    ResetConfirmRequest,
    TokenRequest,
    UserResponse,
)
from tailorcraft.infrastructure.api.schemas.intake import ErrorResponse
from tailorcraft.infrastructure.identity.failed_login_log import EVENT_LOGIN_FAILED
from tailorcraft.infrastructure.rate_limit import (
    RateLimiterUnavailable,
    RateLimitScope,
    RedisFixedWindowRateLimiter,
    client_ip,
)
from tailorcraft.infrastructure.settings import Settings

router = APIRouter(prefix="/api/auth", tags=["identity"])
log = structlog.get_logger(__name__)

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

_NO_STORE: Final = "no-store"

EVENT_REGISTER_REFUSED: Final = "identity.register_refused"
EVENT_REFRESH_REFUSED: Final = "identity.refresh_refused"
EVENT_REFRESH_RACED: Final = "identity.refresh_raced"
EVENT_USER_MISSING: Final = "identity.user_missing"
EVENT_ACCOUNT_DELETION_REFUSED: Final = "identity.account_deletion_refused"
EVENT_ACCOUNT_ERASED: Final = "retention.account_erased"
EVENT_ACCOUNT_FILE_UNLINK_FAILED: Final = "retention.account_file_unlink_failed"

PASSWORD_INCORRECT_DETAIL: Final = {
    "code": "password_incorrect",
    "message": "That password is not right, so your account was not deleted.",
}
"""S-41: 403, **not** 401 — the requester *is* signed in, the fix is not "sign in", and the client's
interceptor refreshes on a 401 (AC-43). `InvalidCredentials` keeps its global 401
`invalid_credentials` for login; only this handler reads it this way (technical plan §3)."""


def _login_id(login_id: LoginId | None) -> str | None:
    """A login's id as a log field: the UUID's text, or `None` where the raise site had no login in
    hand. An **id** — the only thing about a login the refresh lines ever carry (I-21, I-23)."""
    return str(login_id.value) if login_id is not None else None


def _no_store(response: Response) -> None:
    """`Cache-Control: no-store` (AC-26) — on every response that carries a token or touches the
    cookie, so no shared or browser cache ever keeps a bearer credential or a `Set-Cookie`."""
    response.headers["Cache-Control"] = _NO_STORE


async def _commit(session: AsyncSession) -> None:
    """Commit the request's unit of work **before** answering (the module docstring's last point).

    Deliberately no `try`: a failed commit is a `SQLAlchemyError`, which `main.py` renders as 503
    `service_unavailable` and `get_session` rolls back. Nothing after this line runs on that path, so
    no cookie is set or cleared for a write that did not land (I-31)."""
    await session.commit()


def _error_response(exc: HTTPException) -> JSONResponse:
    """An `HTTPException` rendered as the handler's **return value**, in `main.py`'s envelope.

    For the refusals that must commit (I-21, I-24): returning keeps `get_session` on its committing
    branch, where raising would put it on its rolling-back one. `main.py`'s `HTTPException` handler
    wraps the same `detail` the same way, so a client cannot tell which path produced the body."""
    return JSONResponse(
        status_code=exc.status_code, content={"error": exc.detail}, headers=exc.headers
    )


def _cleared(response: JSONResponse, settings: Settings) -> JSONResponse:
    """Clear `tc_refresh` on a refusal response (AC-24: the one attribute builder) and mark it
    uncacheable — it sets a cookie."""
    clear_refresh_cookie(response, settings)
    _no_store(response)
    return response


def _authenticated(result: Authenticated) -> AuthenticatedResponse:
    return AuthenticatedResponse(
        access_token=result.access_token.token,
        expires_in=int(result.access_token.expires_in.total_seconds()),
        user=_user_response(result.user),
    )


def _user_response(user: User) -> UserResponse:
    return UserResponse(id=user.id.value, email=user.email.value, created_at=user.created_at)


def _cookie_max_age(login: Login) -> int:
    """The cookie's `Max-Age`: whole seconds from the use case's own instant to the login's
    **absolute** `expires_at` — so a rotation on day 29 carries one day, never thirty (AC-24, OQ-9).

    The instant is the one the use case stamped (`rotated_at` after a rotation, `created_at` for a new
    login), never a second `clock.now()` here: a system clock read a moment later can cross a second
    boundary and shave one off, and the use case already decided what "now" was (one `clock.now()`
    per use case, technical plan §2)."""
    now = login.rotated_at if login.rotated_at is not None else login.created_at
    return remaining_lifetime_seconds(login.expires_at, now)


def _sign_in(response: Response, result: Authenticated, token: str, settings: Settings) -> None:
    """Set the refresh cookie for `result`'s login and mark the response uncacheable. Called only
    after `_commit`, so the browser is never handed a cookie for a login that does not exist."""
    set_refresh_cookie(response, token, _cookie_max_age(result.login), settings)
    _no_store(response)


async def _enforce(
    limiter: RedisFixedWindowRateLimiter,
    scope: RateLimitScope,
    identifier: str,
    limit: int,
) -> None:
    """One limiter check, answered the identity way: **fail closed** (503 `rate_limit_unavailable`,
    the limiter already logged `rate_limit.unavailable`) and 429 `rate_limited` + `Retry-After` over
    the limit. Logs `scope` and `namespace` only — **never the identifier**, which is an IP address
    or the keyed hash of an email (I-14, I-15)."""
    try:
        decision = await limiter.check(scope, identifier, limit)
    except RateLimiterUnavailable:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "rate_limit_unavailable",
                "message": "Signing in is unavailable just now. Please try again shortly.",
            },
        ) from None
    if not decision.allowed:
        log.info("rate_limit.exceeded", scope=scope, namespace=limiter.namespace)
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "code": "rate_limited",
                "message": (
                    f"Too many attempts. Try again in {decision.retry_after_seconds} seconds."
                ),
            },
            headers={"Retry-After": str(decision.retry_after_seconds)},
        )


def _register_refusal_reason(exc: DomainError) -> str | None:
    """The `reason=` of `identity.register_refused` (I-1 … I-5), or `None` for an error that is not a
    refusal of the input (a hasher failure is I-45's line, written by the adapter)."""
    if isinstance(exc, InvalidEmailAddress):
        return "invalid_email"
    if isinstance(exc, WeakPassword):
        return exc.reason.value
    if isinstance(exc, EmailAlreadyRegistered):
        return "email_taken"
    return None


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
    settings: SettingsDep,
) -> AuthenticatedResponse:
    """Create an account and sign its owner in: 201 + `Set-Cookie: tc_refresh`."""
    # Before anything costly: a 429 or a 503 here computes no hash and touches no row (AC-27, I-16).
    await _enforce(
        rate_limiter,
        "ip",
        client_ip(request, settings.trusted_proxy_hops),
        settings.register_rate_limit_per_ip_per_hour,
    )

    minted = mint_refresh_token()
    try:
        result = await register_user(
            body.email, body.password.get_secret_value(), minted.token_hash
        )
    except DomainError as exc:
        reason = _register_refusal_reason(exc)
        if reason is not None:
            # Never the email, never the password, never its length (I-1 … I-5).
            log.info(EVENT_REGISTER_REFUSED, reason=reason)
        await _commit(session)
        raise domain_error_to_http_exception(exc) from None

    await _commit(session)
    _sign_in(response, result, minted.token, settings)
    return _authenticated(result)


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
    settings: SettingsDep,
) -> AuthenticatedResponse:
    """Check a password and start a new login: 200 + `Set-Cookie: tc_refresh`.

    **An existing `tc_refresh` is neither read nor revoked** (I-13): a second login is a second
    `Login`; the old one becomes unreachable in this browser and lives to its absolute expiry.
    Reading it here would make logging in depend on a second credential for housekeeping."""
    await _enforce(
        ip_rate_limiter,
        "ip",
        client_ip(request, settings.trusted_proxy_hops),
        settings.login_rate_limit_per_ip_per_hour,
    )
    # The per-email budget binds however many addresses the guesses come from (I-15). It is keyed
    # by the *normalized* address, so it needs one that parses; one that does not is refused 422 by
    # the use case below without a hash, so there is nothing for that budget to protect. The parse
    # here is the domain's own rule, not a second copy of it.
    try:
        email: EmailAddress | None = EmailAddress.parse(body.email)
    except InvalidEmailAddress:
        email = None
    if email is not None:
        await _enforce(
            email_rate_limiter,
            "email",
            login_email_rate_limit_identifier(email, settings),
            settings.login_rate_limit_per_email_per_hour,
        )

    minted = mint_refresh_token()
    try:
        result = await log_in(body.email, body.password.get_secret_value(), minted.token_hash)
    except DomainError as exc:
        if isinstance(exc, InvalidEmailAddress):
            # I-12. `unknown_email` and `wrong_password` are the observer's lines (I-9, I-10); this
            # router adds nothing about *which* — the error cannot tell it (AC-9).
            log.info(EVENT_LOGIN_FAILED, reason="invalid_email")
        await _commit(session)
        raise domain_error_to_http_exception(exc) from None

    await _commit(session)
    _sign_in(response, result, minted.token, settings)
    return _authenticated(result)


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
    settings: SettingsDep,
) -> AuthenticatedResponse | JSONResponse:
    """Rotate the refresh cookie and mint a new access token: 200 + rotated `Set-Cookie`. No body,
    and **no rate limiter and no Redis** — a Redis outage must never sign anybody out (I-18).

    The two `401`s that follow a *write* are **returned**, after `_commit` (the module docstring):
    `refresh_token_reused` (I-24, the login deleted) and `not_signed_in` for an expired login
    (I-21, deleted on sight). Returning them is what keeps the deletion."""
    presented = read_refresh_token(request)
    if presented is None:
        # I-19: every guest's first page load. No cookie to clear, and nothing worth a log line.
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail=NOT_SIGNED_IN_DETAIL)
    if presented.token_hash is None:
        # I-40: not shaped like a token we mint (a JWT pasted in, a truncated value). Never used as a
        # lookup key; the browser holds junk it should drop.
        log.info(EVENT_REFRESH_REFUSED, reason=LoginNotFoundReason.UNKNOWN.value)
        return _cleared(_error_response(domain_error_to_http_exception(LoginNotFound())), settings)

    replacement = mint_refresh_token()
    try:
        result = await refresh_login(presented.token_hash, replacement.token_hash)
    except RefreshInProgress as exc:
        # I-23, I-25: a second tab lost a race. Nothing changed, and the cookie is left alone — by
        # the time the client retries it holds the winner's.
        log.info(EVENT_REFRESH_RACED, login_id=_login_id(exc.login_id))
        await _commit(session)
        raise domain_error_to_http_exception(exc) from None
    except RefreshTokenReused as exc:
        # I-24. The use case has already removed the login and published `RefreshTokenReuseDetected`
        # (whose `warning` line `ReuseAlertingEventPublisher` writes). Commit, then return.
        await _commit(session)
        return _cleared(_error_response(domain_error_to_http_exception(exc)), settings)
    except LoginNotFound as exc:
        # I-20, I-21, I-27. Unknown, revoked, or expired and deleted on sight — the last one wrote,
        # so this is committed and returned exactly like the reuse above.
        # `reason=unknown` (I-20, I-27) carries no id — no login matched. `reason=expired` (I-21)
        # names the login that was deleted on sight. Never the token, never its hash.
        if exc.reason is LoginNotFoundReason.EXPIRED:
            log.info(
                EVENT_REFRESH_REFUSED, reason=exc.reason.value, login_id=_login_id(exc.login_id)
            )
        else:
            log.info(EVENT_REFRESH_REFUSED, reason=exc.reason.value)
        await _commit(session)
        return _cleared(_error_response(domain_error_to_http_exception(exc)), settings)

    await _commit(session)
    _sign_in(response, result, replacement.token, settings)
    return _authenticated(result)


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
    token required** — logging out must work precisely when the access token has expired.

    **The clear is set only after the commit** (I-31). A dead database raises out of `_commit` into
    `main.py`'s 503, which builds its own response — this `response`'s headers never reach the wire
    on that path, and the line that would clear the cookie never ran. A browser must not forget a
    token the server still honours."""
    presented = read_refresh_token(request)
    # A malformed cookie names no login (I-40): logged out already, so it is `None` to the use case,
    # and still cleared below.
    await log_out(presented.token_hash if presented is not None else None)
    await _commit(session)
    clear_refresh_cookie(response, settings)
    _no_store(response)


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
    session: SessionDep,
) -> UserResponse:
    """The account the bearer token speaks for. No `Origin` check and no limiter: it is authorized
    by a header a cross-site request cannot attach, and it is one stateless verify and one read.

    Writes nothing, and commits anyway — for I-41, not for the write: `get_session`'s own commit
    runs **after** the response is on the wire (the `posting` router measured it), so a database that
    fails there would have answered 200. The handler's commit is the last point a failure can still
    change the answer to 503."""
    try:
        user = await get_current_user(user_id)
    except UserNotFound as exc:
        # I-39: a valid token whose user row is gone.
        log.info(EVENT_USER_MISSING, user_id=str(user_id.value))
        raise domain_error_to_http_exception(exc) from None
    await _commit(session)
    _no_store(response)
    return _user_response(user)


@router.post(
    "/delete-account",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    dependencies=[Depends(require_trusted_origin)],
    responses={
        status.HTTP_403_FORBIDDEN: {
            "model": ErrorResponse,
            "description": (
                "origin_not_allowed — missing or foreign `Origin` header, answered before the "
                "limiter, the database and the hasher (AC-29) | password_incorrect — the bearer is "
                "valid but the password is not; 403 and not 401, so the client's interceptor does "
                "not refresh on it (AC-43). Nothing is deleted."
            ),
        },
        status.HTTP_401_UNAUTHORIZED: {
            "model": ErrorResponse,
            "description": (
                'invalid_access_token (+ WWW-Authenticate: Bearer error="invalid_token") | '
                "not_signed_in — the account is already gone (a concurrent deletion won, S-46)."
            ),
        },
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ErrorResponse,
            "description": "validation_error",
        },
        **_RATE_LIMITED,
        **_SERVICE_UNAVAILABLE_LIMITED,
    },
)
async def delete_account(
    body: DeleteAccountRequest,
    request: Request,
    response: Response,
    user_id: RequireUserDep,
    users: UserRepositoryDep,
    delete_own_account: DeleteOwnAccountDep,
    ip_rate_limiter: LoginIpRateLimiterDep,
    email_rate_limiter: LoginEmailRateLimiterDep,
    session: SessionDep,
    settings: SettingsDep,
) -> None:
    """Delete the bearer's account, re-asking its password: 204 + a cleared `tc_refresh` (AC-29).

    **A `POST` under `/api/auth`, not a `DELETE` under `/api/me`** (technical plan §4): it clears
    `tc_refresh`, whose `Path` is `/api/auth`, so no route elsewhere could; and a body on a `DELETE`
    is dropped by enough intermediaries to be a bug waiting.

    The order is the contract's (technical plan §3): trusted `Origin` (the decorator's
    `dependencies=`, first) → bearer → the login limiters, **fail closed**, the per-email key taken
    from the user row and never from the body → `DeleteOwnAccount` → log from its report → commit →
    clear the cookie. The account's rows go; the guest workspace in this browser is untouched — it is
    guest data, purged on its own clock.

    **Nothing is caught that would leave a write uncommitted.** Every refusal (wrong password, a
    limiter, a hasher failure, the account already gone) happens before `EraseAccount` deletes a row,
    so raising is safe. The erasure itself commits inside the bound `CommittingAccountData` before a
    single file is unlinked (rows first, committed, then files); a failure there is a
    `SQLAlchemyError`, `main.py`'s 503, and no line below it runs — so no cookie is cleared for an
    account that still exists (S-43).
    """
    # The IP budget first: it needs nothing but the request, so a 429 or a 503 reads no row and
    # computes no hash (2.1's order, AC-29).
    await _enforce(
        ip_rate_limiter,
        "ip",
        client_ip(request, settings.trusted_proxy_hops),
        settings.login_rate_limit_per_ip_per_hour,
    )

    # The per-email budget is keyed by the **account's** address, read from the user row — never from
    # the body, which carries only the password. A token outliving its account ends here (S-46's
    # loser, AC-30).
    try:
        user = await users.get(user_id)
    except UserNotFound as exc:
        log.info(EVENT_USER_MISSING, user_id=str(user_id.value))
        raise domain_error_to_http_exception(exc) from None
    await _enforce(
        email_rate_limiter,
        "email",
        login_email_rate_limit_identifier(user.email, settings),
        settings.login_rate_limit_per_email_per_hour,
    )

    try:
        password = Password.from_input(body.password.get_secret_value())
        report = await delete_own_account(user_id, password)
    except InvalidCredentials:
        # S-41. The id and the reason — never the password, never its length.
        log.info(EVENT_ACCOUNT_DELETION_REFUSED, user_id=str(user_id.value), reason="password")
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail=PASSWORD_INCORRECT_DETAIL) from None
    except (UserNotFound, AccountNotFound) as exc:
        # S-46: a concurrent deletion won between the read above and the erasure.
        log.info(EVENT_USER_MISSING, user_id=str(user_id.value))
        raise domain_error_to_http_exception(exc) from None
    except DomainError as exc:
        raise domain_error_to_http_exception(exc) from None

    _log_erasure(user_id, report)

    await _commit(session)
    clear_refresh_cookie(response, settings)
    _no_store(response)


def _log_erasure(user_id: UserId, report: AccountErasureReport) -> None:
    """S-45: one `retention.account_erased` line with the counts, and one
    `retention.account_file_unlink_failed` per failed unlink — ids, counts and class names only.

    `EraseAccount` returns its failures rather than logging them (the application layer does not
    log); this is the entry point writing them down, exactly as the purge's task does with its
    `PurgeReport`. Each failure is an orphan file for the operator's sweep (R-5)."""
    log.info(
        EVENT_ACCOUNT_ERASED,
        user_id=str(user_id.value),
        base_cvs=report.base_cvs,
        # Slice 2.3 (H-52): an account is also its history. `files` is every key the erasure tried
        # to unlink — saved CVs' and derived export keys — so the two numbers below read against it.
        tailoring_runs=report.tailoring_runs,
        job_postings=report.job_postings,
        export_jobs=report.export_jobs,
        files=report.files,
        files_unlinked=report.files_unlinked,
        files_failed=len(report.unlink_failures),
    )
    for error_type in report.unlink_failures:
        log.warning(
            EVENT_ACCOUNT_FILE_UNLINK_FAILED, user_id=str(user_id.value), error_type=error_type
        )


# ---------------------------------------------------------------------------------------------
# Slice 2.5 — confirming an address and resetting a password (technical plan §4, ADR-0027,
# ADR-0028). **SKELETON** (T26): the real paths, the real schemas, every documented status in
# `responses=`, the dependencies the GREEN handlers need, and bodies raising `NotImplementedError`.
# RED is T27 (`qa`), GREEN is T29.
#
# None of the three reads a bearer, `tc_refresh` or `tc_guest`, and none sets a cookie. Each sits
# behind `require_trusted_origin` (403 before the limiter, the database or the hasher). The two
# confirms have **no limiter** (plan §4: a 256-bit token is not guessable; garbage costs one indexed
# lookup, a malformed one none), so Redis down never blocks finishing a confirmation or a reset.
#
# **How T29 decides a malformed token**: `one_time_tokens.hash_presented(body.token)` — `None` (not
# the 43-character URL-safe grammar the minter produces) is 400 `link_invalid`, `reason=malformed`,
# **before any database read**; otherwise its `TokenHash` is what the use case receives. The
# plaintext never reaches `application/`.
# ---------------------------------------------------------------------------------------------

_LINK_INVALID: dict[int | str, dict[str, Any]] = {
    status.HTTP_400_BAD_REQUEST: {
        "model": ErrorResponse,
        "description": (
            "link_invalid — malformed (refused before any database read), unknown, already used or "
            "expired; one code for every reason, which goes to the log line only."
        ),
    },
}
_VALIDATION_ERROR: dict[int | str, dict[str, Any]] = {
    status.HTTP_422_UNPROCESSABLE_CONTENT: {
        "model": ErrorResponse,
        "description": "validation_error",
    },
}


@router.post(
    "/registration/confirm",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    dependencies=[Depends(require_trusted_origin)],
    responses={
        **_ORIGIN_NOT_ALLOWED,
        **_LINK_INVALID,
        status.HTTP_409_CONFLICT: {
            "model": ErrorResponse,
            "description": (
                "email_already_registered — the address became an account after this link was "
                "sent; the pending registration is deleted."
            ),
        },
        **_VALIDATION_ERROR,
        **_SERVICE_UNAVAILABLE,
    },
)
async def confirm_registration(
    body: TokenRequest,
    response: Response,
    confirm_registration: ConfirmRegistrationDep,
    session: SessionDep,
) -> None:
    """Turn a pending registration into an account: **204**, no body, **no cookie** — confirming
    does not sign in (OQ-3); the client sends the user to `/login`. `Cache-Control: no-store`."""
    raise NotImplementedError


@router.post(
    "/password-reset",
    status_code=status.HTTP_202_ACCEPTED,
    response_class=Response,
    dependencies=[Depends(require_trusted_origin)],
    responses={
        **_ORIGIN_NOT_ALLOWED,
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ErrorResponse,
            "description": "invalid_email | validation_error",
        },
        **_RATE_LIMITED,
        **_SERVICE_UNAVAILABLE_LIMITED,
    },
)
async def request_password_reset(
    body: EmailRequest,
    request: Request,
    response: Response,
    request_password_reset: RequestPasswordResetDep,
    ip_rate_limiter: PasswordResetIpRateLimiterDep,
    email_rate_limiter: PasswordResetEmailRateLimiterDep,
    settings: SettingsDep,
) -> None:
    """Ask for a reset link: **202**, empty body, **byte-identical whether or not the address has an
    account** (the use case is never handed anything that could tell). Order: `Origin` → per-IP
    limiter (`auth:password-reset`, scope `ip`, fails closed) → parse (422 `invalid_email`) →
    per-address limiter (scope `email`, keyed by `login_email_rate_limit_identifier`, fails closed)
    → `RequestPasswordReset` (its committing `add`, then the enqueue; a broker refusal is 503
    `service_unavailable`). `Cache-Control: no-store`."""
    raise NotImplementedError


@router.post(
    "/password-reset/confirm",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    dependencies=[Depends(require_trusted_origin)],
    responses={
        **_ORIGIN_NOT_ALLOWED,
        **_LINK_INVALID,
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ErrorResponse,
            "description": (
                "password_too_short (+min_length) | password_too_long (+max_length) | "
                "password_matches_email | validation_error — the token is kept, so the user can "
                "try another password with the same link."
            ),
        },
        **_SERVICE_UNAVAILABLE,
    },
)
async def confirm_password_reset(
    body: ResetConfirmRequest,
    response: Response,
    reset_password: ResetPasswordDep,
    session: SessionDep,
) -> None:
    """Set a new password from a reset link: **204**, no body, no cookie; **every login of the
    account is revoked** (ADR-0028). The handler commits on success (`deps.get_reset_password`).
    `Cache-Control: no-store`."""
    raise NotImplementedError
