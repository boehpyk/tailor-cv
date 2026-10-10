"""API tests for `identity-register-and-login`'s HTTP surface: `POST/GET /api/auth/*`.

**This is the RED half of a red-first cycle** (CLAUDE.md, sdlc.md §2, T29). Every test here is
written against `docs/specs/identity-register-and-login/feature-spec.md`'s failure contract
(I-1…I-54) and its acceptance criteria — not against `infrastructure/api/routers/auth.py`, whose
five handlers currently do nothing but `raise NotImplementedError` (T28's SKELETON).

**Why this file's `client` fixture is not the shared one.** Exactly `test_intake.py`'s reason:
`tests/conftest.py`'s `client` uses `ASGITransport(app=app)`, whose default
`raise_app_exceptions=True` re-raises an unhandled handler exception into the test as a bare Python
exception, turning every `NotImplementedError` skeleton into an ERROR rather than a `FAIL` and
burying the assertion this file is built around. This module's own `client` fixture sets
`raise_app_exceptions=False`, so `response.status_code` is a real `500` and
`assert response.status_code == 201` fails on the assertion, the way CLAUDE.md wants a red to fail.

**What is decided *before* a handler body runs, and therefore already passes.** Three of this
router's checks are FastAPI dependencies or Pydantic validation, not code inside the
`raise NotImplementedError` bodies, and were built and tested in earlier (non-RED) tasks — T26, T27:

- `require_trusted_origin` is a decorator-level `dependencies=[...]` entry on all four cookie
  endpoints, resolved before the route's own parameters. A missing or foreign `Origin` therefore
  already answers 403 `origin_not_allowed`.
- `CredentialsRequest`'s own field validation (a missing field, a wrong type, unparsable JSON bytes)
  is FastAPI's, resolved before the handler runs. It already answers 422 `validation_error`.
- `require_user` (`/me`'s bearer dependency) is a function parameter resolved before the handler
  body, exactly like `require_trusted_origin`. Every bearer-token refusal (I-32…I-40, AC-34) already
  answers 401 `invalid_access_token` with its `WWW-Authenticate` challenge.

Each test below that exercises one of these says so at the point where it turns out to already pass.
**Nothing that requires a rate-limiter's `.check()` call, a use case, a cookie, or a database write
already works** — those live inside the `NotImplementedError` bodies, and every such test reds on
`response.status_code`, typically `500 != <expected>`.

**Seeding bypasses the broken `register`/`login` endpoints on purpose.** A `refresh`/`logout`/`me`
test needs a real `User` and `Login` row to exercise meaningfully, and going through `POST
/api/auth/register` to get one is circular — that endpoint is exactly as unimplemented as the one
under test. `_seed_user`/`_seed_login` build them directly through the real repositories
(`SqlAlchemyUserRepository`, `SqlAlchemyLoginRepository`) and the real `Argon2PasswordHasher` /
`mint_refresh_token`, committed onto the test's own rolled-back transaction — the same pattern
`test_tailoring.py`'s `test_a_run_abandoned_past_the_stale_window_...` uses to seed a `running` run
directly through its repository.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from argon2 import PasswordHasher as Argon2Library
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from tailorcraft.domain.identity.guest_session import GuestSession
from tailorcraft.domain.identity.login import Login
from tailorcraft.domain.identity.ports import (
    GuestSessionRepository,
    LoginRepository,
    UserRepository,
)
from tailorcraft.domain.identity.user import User
from tailorcraft.domain.identity.value_objects import (
    EmailAddress,
    Password,
    PasswordHash,
    PasswordVerdict,
)
from tailorcraft.infrastructure.api.deps import (
    get_app_settings,
    get_clock,
    get_refresh_login,
    get_session,
)
from tailorcraft.infrastructure.api.guest_session import (
    COOKIE_NAME as GUEST_COOKIE_NAME,
)
from tailorcraft.infrastructure.api.guest_session import (
    mint_guest_token,
)
from tailorcraft.infrastructure.api.main import create_app
from tailorcraft.infrastructure.api.refresh_cookie import (
    COOKIE_NAME as REFRESH_COOKIE_NAME,
)
from tailorcraft.infrastructure.api.refresh_cookie import (
    hash_refresh_token,
    mint_refresh_token,
)
from tailorcraft.infrastructure.clock import FixedClock
from tailorcraft.infrastructure.identity.access_tokens import JwtAccessTokens
from tailorcraft.infrastructure.identity.password_hasher import Argon2PasswordHasher
from tailorcraft.infrastructure.persistence.database import create_session_factory
from tailorcraft.infrastructure.redis_client import create_redis
from tailorcraft.infrastructure.settings import JWT_SIGNING_KEY_MIN_BYTES, Settings
from tailorcraft.infrastructure.tasks.app import app as celery_app
from tests.api.account_mail_support import install_recording_queue
from tests.api.me_support import seed_user_and_sign_in

REGISTER_URL = "/api/auth/register"
LOGIN_URL = "/api/auth/login"
REFRESH_URL = "/api/auth/refresh"
LOGOUT_URL = "/api/auth/logout"
ME_URL = "/api/auth/me"

A_STRONG_PASSWORD = "correct horse battery staple 9"  # 31 chars, well clear of the 12-char floor.


# ---------------------------------------------------------------------------------------------
# Small HTTP helpers, matching test_intake.py's / test_posting.py's shapes
# ---------------------------------------------------------------------------------------------


def _error_code(response: Response) -> str:
    body = response.json()
    assert "error" in body, f"expected the {{'error': {{...}}}} envelope, got {body!r}"
    return str(body["error"]["code"])


def _error_message(response: Response) -> str:
    body = response.json()
    assert "error" in body, f"expected the {{'error': {{...}}}} envelope, got {body!r}"
    return str(body["error"]["message"])


def _error_extra(response: Response, key: str) -> object:
    body = response.json()
    assert "error" in body, f"expected the {{'error': {{...}}}} envelope, got {body!r}"
    assert key in body["error"], f"expected {key!r} in the error envelope, got {body['error']!r}"
    return body["error"][key]


def _origin_headers(settings: Settings) -> dict[str, str]:
    """Never hard-code the origin — read it off the settings object under test, as the box's own
    `PUBLIC_BASE_URL` decides it (AC-25)."""
    return {"Origin": settings.public_base_url}


def _credentials(email: str, password: str) -> dict[str, str]:
    return {"email": email, "password": password}


def _override_settings(app: FastAPI, base: Settings, **updates: object) -> Settings:
    """`test_intake.py`'s helper, unchanged: both settings-reading paths this codebase uses
    (`SettingsDep` and `request.app.state.settings`) are overridden together."""
    modified = base.model_copy(update=updates)
    app.dependency_overrides[get_app_settings] = lambda: modified
    app.state.settings = modified
    return modified


def _all_set_cookie_headers(response: Response) -> list[str]:
    return response.headers.get_list("set-cookie")


def _cookie_header_named(response: Response, name: str) -> str | None:
    for header in _all_set_cookie_headers(response):
        if header.startswith(f"{name}="):
            return header
    return None


def _parse_set_cookie(header: str) -> dict[str, str | bool]:
    """Every attribute of one `Set-Cookie` header, lower-cased keys, so a test can assert on
    `attrs["path"]`, `attrs["samesite"]`, `"secure" in attrs`, `"domain" not in attrs` without
    caring whether Starlette capitalized the attribute name or not (it does not, for `SameSite`:
    measured as `samesite=strict`, all lower case — parse case-insensitively, per the brief)."""
    parts = [p.strip() for p in header.split(";")]
    name, _, value = parts[0].partition("=")
    attrs: dict[str, str | bool] = {"__name__": name, "__value__": value}
    for part in parts[1:]:
        if "=" in part:
            k, _, v = part.partition("=")
            attrs[k.strip().lower()] = v.strip()
        else:
            attrs[part.strip().lower()] = True
    return attrs


def _refresh_cookie_attrs(response: Response) -> dict[str, str | bool]:
    header = _cookie_header_named(response, REFRESH_COOKIE_NAME)
    assert header is not None, f"expected a Set-Cookie: {REFRESH_COOKIE_NAME}=... header"
    return _parse_set_cookie(header)


def _user_repository(session: AsyncSession) -> UserRepository:
    """Deferred import — the mapper-configuration reason `deps.py::get_base_cv_repository`
    documents: the repository module reads `User._id` etc. as a plain attribute at *import* time,
    which only exists once `configure_mappings()` has run. Importing this at module scope would
    fail at collection time, before the session-scoped `_mappings` fixture ever executes."""
    from tailorcraft.infrastructure.persistence.repositories.identity.user import (
        SqlAlchemyUserRepository,
    )

    return SqlAlchemyUserRepository(session)


def _login_repository(session: AsyncSession) -> LoginRepository:
    from tailorcraft.infrastructure.persistence.repositories.identity.login import (
        SqlAlchemyLoginRepository,
    )

    return SqlAlchemyLoginRepository(session)


def _guest_session_repository(session: AsyncSession) -> GuestSessionRepository:
    from tailorcraft.infrastructure.persistence.repositories.identity.guest_session import (
        SqlAlchemyGuestSessionRepository,
    )

    return SqlAlchemyGuestSessionRepository(session)


# ---------------------------------------------------------------------------------------------
# Module-local fixtures — mirrors test_intake.py's exactly, for the module docstring's reason
# ---------------------------------------------------------------------------------------------


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    """Shadows `conftest.py`'s `client` fixture for every test in this module."""
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="https://testserver") as c:
        yield c


def _new_client(app: FastAPI) -> AsyncClient:
    """A second, independent cookie jar against the same app, for tests that need two distinct
    "requesters" while sharing `ASGITransport`'s fixed peer IP (the per-IP rate-limit tests)."""
    return AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="https://testserver"
    )


@pytest.fixture(autouse=True)
def _reset_redis_between_tests(clear_redis: None) -> None:
    """Applies `clear_redis` (conftest.py) to every test in this module automatically — nearly every
    test here touches a rate limiter, and CLAUDE.md is explicit that a database rollback does not
    reach Redis."""


@pytest.fixture(autouse=True)
def _recording_mail_queue(app: FastAPI) -> None:
    """Slice 2.5 (T27): register now enqueues a mail task. This module never publishes to the dev
    stack's real `mail` queue — the API is handed a recording fake (`account_mail_support`)."""
    install_recording_queue(app)


# ---------------------------------------------------------------------------------------------
# Seeding helpers — build a real User / Login directly through the repositories, bypassing the
# (currently unimplemented) register/login endpoints. See the module docstring.
# ---------------------------------------------------------------------------------------------


async def _seed_user(
    session: AsyncSession,
    password_hasher: Argon2PasswordHasher,
    clock: FixedClock,
    *,
    email: str = "seeded.user@example.com",
    password: str = A_STRONG_PASSWORD,
) -> User:
    users = _user_repository(session)
    hashed = await password_hasher.hash(Password.from_input(password))
    user = User.register_with_password(
        users.next_identity(), EmailAddress.parse(email), hashed, clock.now()
    )
    await users.add(user)
    await session.commit()
    return user


async def _seed_login(
    session: AsyncSession,
    user: User,
    clock: FixedClock,
    *,
    ttl_days: int = 30,
) -> tuple[Login, str]:
    """A fresh `Login` at generation 1, plus the raw token that hashes to its current hash — the
    value a test sets as the `tc_refresh` cookie."""
    logins = _login_repository(session)
    minted = mint_refresh_token()
    login = Login.start(
        logins.next_identity(), user.id, minted.token_hash, clock.now(), timedelta(days=ttl_days)
    )
    await logins.add(login)
    await session.commit()
    return login, minted.token


async def _seed_guest_session(
    session: AsyncSession,
    clock: FixedClock,
    *,
    ttl_hours: int = 24,
) -> tuple[GuestSession, str]:
    sessions = _guest_session_repository(session)
    minted = mint_guest_token()
    guest_session = GuestSession.start(
        sessions.next_identity(), minted.token_hash, clock.now(), ttl_hours
    )
    await sessions.add(guest_session)
    await session.commit()
    return guest_session, minted.token


class _RecordingHasher:
    """Wraps a real `PasswordHasherPort`, counting calls — AC-25's "a recording hasher sees zero
    calls" and I-1…I-4's "no hash computed"."""

    def __init__(self, real: Argon2PasswordHasher) -> None:
        self._real = real
        self.hash_calls = 0
        self.verify_calls = 0

    async def hash(self, password: Password) -> PasswordHash:
        self.hash_calls += 1
        return await self._real.hash(password)

    async def verify(self, password: Password, against: PasswordHash | None) -> PasswordVerdict:
        self.verify_calls += 1
        return await self._real.verify(password, against)


# ---------------------------------------------------------------------------------------------
# AC-25 / I-28 — Origin check runs before the limiter, the database and the hasher
#
# ALREADY PASSES: `require_trusted_origin` is a decorator-level dependency, resolved before every
# one of these four handlers' NotImplementedError bodies.
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("url", [REGISTER_URL, LOGIN_URL, REFRESH_URL, LOGOUT_URL])
async def test_missing_origin_is_refused_before_the_handler_runs(
    client: AsyncClient, url: str
) -> None:
    response = await client.post(url, json=_credentials("someone@example.com", A_STRONG_PASSWORD))

    assert response.status_code == 403, response.text
    assert _error_code(response) == "origin_not_allowed"


@pytest.mark.parametrize("url", [REGISTER_URL, LOGIN_URL, REFRESH_URL, LOGOUT_URL])
async def test_a_foreign_origin_is_refused_403(client: AsyncClient, url: str) -> None:
    response = await client.post(
        url,
        json=_credentials("someone@example.com", A_STRONG_PASSWORD),
        headers={"Origin": "https://evil.example"},
    )

    assert response.status_code == 403, response.text
    assert _error_code(response) == "origin_not_allowed"


async def test_origin_check_runs_before_the_hasher_which_sees_zero_calls(
    client: AsyncClient, app: FastAPI
) -> None:
    real_hasher = app.state.password_hasher
    recording = _RecordingHasher(real_hasher)
    app.state.password_hasher = recording

    response = await client.post(
        REGISTER_URL, json=_credentials("someone@example.com", A_STRONG_PASSWORD)
    )

    assert response.status_code == 403, response.text
    assert _error_code(response) == "origin_not_allowed"
    assert recording.hash_calls == 0
    assert recording.verify_calls == 0


async def test_me_is_exempt_from_the_origin_check(client: AsyncClient) -> None:
    """`/me` has no `Origin` dependency at all — a missing header must not become 403; it falls
    through to the bearer check instead (401 `invalid_access_token`)."""
    response = await client.get(ME_URL)

    assert response.status_code == 401, response.text
    assert _error_code(response) == "invalid_access_token"


# ---------------------------------------------------------------------------------------------
# I-8 / validation_error — and its measured ordering against the Origin check
#
# ALREADY PASSES: every assertion below is decided by FastAPI's own body parsing / Pydantic
# validation, before any dependency or handler body runs.
# ---------------------------------------------------------------------------------------------


async def test_completely_invalid_json_bytes_get_422_before_the_origin_check(
    client: AsyncClient,
) -> None:
    """No `Origin` header at all — if the origin check ran first this would be 403. FastAPI decodes
    the body before resolving any dependency, so unparsable JSON is 422 first (router docstring)."""
    response = await client.post(
        REGISTER_URL, content=b"{not valid json", headers={"content-type": "application/json"}
    )

    assert response.status_code == 422, response.text
    assert _error_code(response) == "validation_error"


async def test_a_wrong_shape_body_gets_403_before_validation_when_origin_is_untrusted(
    client: AsyncClient,
) -> None:
    """A well-formed-but-wrong-shape body (valid JSON, missing fields) — no `Origin` header. Per the
    router's own docstring this gets 403, not 422: FastAPI's *own* parameter validation runs after
    dependency resolution, unlike raw JSON decoding."""
    response = await client.post(REGISTER_URL, json={})

    assert response.status_code == 403, response.text
    assert _error_code(response) == "origin_not_allowed"


async def test_missing_required_field_is_422_validation_error(
    client: AsyncClient, settings: Settings
) -> None:
    response = await client.post(
        REGISTER_URL, json={"email": "someone@example.com"}, headers=_origin_headers(settings)
    )

    assert response.status_code == 422, response.text
    assert _error_code(response) == "validation_error"


async def test_wrong_field_type_is_422_validation_error(
    client: AsyncClient, settings: Settings
) -> None:
    response = await client.post(
        REGISTER_URL,
        json={"email": "someone@example.com", "password": ["not", "a", "string"]},
        headers=_origin_headers(settings),
    )

    assert response.status_code == 422, response.text
    assert _error_code(response) == "validation_error"


# ---------------------------------------------------------------------------------------------
# I-1…I-4 — registration's failure contract over the email/password rules
# ---------------------------------------------------------------------------------------------


async def test_malformed_email_on_register_is_422_invalid_email(
    client: AsyncClient, settings: Settings
) -> None:
    response = await client.post(
        REGISTER_URL,
        json=_credentials("not-an-email", A_STRONG_PASSWORD),
        headers=_origin_headers(settings),
    )

    assert response.status_code == 422, response.text
    assert _error_code(response) == "invalid_email"


async def test_a_password_under_twelve_code_points_is_422_password_too_short(
    client: AsyncClient, settings: Settings
) -> None:
    response = await client.post(
        REGISTER_URL,
        json=_credentials("someone@example.com", "short1"),
        headers=_origin_headers(settings),
    )

    assert response.status_code == 422, response.text
    assert _error_code(response) == "password_too_short"
    assert _error_extra(response, "min_length") == 12


async def test_a_password_over_128_code_points_is_422_password_too_long(
    client: AsyncClient, settings: Settings
) -> None:
    response = await client.post(
        REGISTER_URL,
        json=_credentials("someone@example.com", "x" * 129),
        headers=_origin_headers(settings),
    )

    assert response.status_code == 422, response.text
    assert _error_code(response) == "password_too_long"
    assert _error_extra(response, "max_length") == 128


async def test_a_password_equal_to_the_email_is_422_password_matches_email(
    client: AsyncClient, settings: Settings
) -> None:
    response = await client.post(
        REGISTER_URL,
        json=_credentials("match@example.com", "match@example.com"),
        headers=_origin_headers(settings),
    )

    assert response.status_code == 422, response.text
    assert _error_code(response) == "password_matches_email"


# ---------------------------------------------------------------------------------------------
# I-5, I-7 / AC-29 — a duplicate email, and registering while carrying a live guest cookie
# ---------------------------------------------------------------------------------------------


async def test_registering_an_already_registered_email_is_202_and_never_409(
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    password_hasher: Argon2PasswordHasher,
    clock: FixedClock,
) -> None:
    """**Amended in slice 2.5 (T27, AC-28): was `..._is_409`.** Registration no longer enumerates —
    the 409 `email_already_registered` moved to *confirmation* (ADR-0027), where the only person who
    can see it holds a token mailed to that address. Register answers 202 for an address that has an
    account exactly as for a new one, so a stranger typing an address learns nothing."""
    await _seed_user(session, password_hasher, clock, email="taken@example.com")
    body = _credentials("taken@example.com", A_STRONG_PASSWORD)

    first = await client.post(REGISTER_URL, json=body, headers=_origin_headers(settings))
    second = await client.post(REGISTER_URL, json=body, headers=_origin_headers(settings))

    assert first.status_code == 202, first.text
    assert second.status_code == 202, second.text


async def test_registering_with_a_live_guest_cookie_leaves_the_guest_session_untouched(
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    clock: FixedClock,
) -> None:
    """AC-29: no `Set-Cookie: __Host-tc_guest` on any outcome, and the guest session row is
    byte-identical
    afterwards, including `expires_at` — registration must never read, set or extend it."""
    guest_session, raw_guest_token = await _seed_guest_session(session, clock)
    client.cookies.set(GUEST_COOKIE_NAME, raw_guest_token)

    response = await client.post(
        REGISTER_URL,
        json=_credentials("guest.carrier@example.com", A_STRONG_PASSWORD),
        headers=_origin_headers(settings),
    )

    assert response.status_code == 202, response.text
    assert _cookie_header_named(response, GUEST_COOKIE_NAME) is None

    sessions = _guest_session_repository(session)
    reloaded = await sessions.get(guest_session.id)
    assert reloaded.id == guest_session.id
    assert reloaded.created_at == guest_session.created_at
    assert reloaded.expires_at == guest_session.expires_at


# ---------------------------------------------------------------------------------------------
# AC-33 — the success envelope's key set
# ---------------------------------------------------------------------------------------------


async def test_a_successful_registration_returns_an_empty_202_not_the_authenticated_response(
    client: AsyncClient, settings: Settings
) -> None:
    """**Amended in slice 2.5 (T27, AC-27): was the 201 `AuthenticatedResponse` key-set test.**
    Registering no longer signs anyone in, so there is no token, no user object and no body."""
    response = await client.post(
        REGISTER_URL,
        json=_credentials("shape.check@example.com", A_STRONG_PASSWORD),
        headers=_origin_headers(settings),
    )

    assert response.status_code == 202, response.text
    assert response.content == b""


# ---------------------------------------------------------------------------------------------
# AC-28 — no enumeration at login: unknown email and wrong password are byte-identical
# ---------------------------------------------------------------------------------------------


async def test_unknown_email_and_wrong_password_are_byte_identical(
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    password_hasher: Argon2PasswordHasher,
    clock: FixedClock,
) -> None:
    await _seed_user(
        session, password_hasher, clock, email="real.account@example.com", password="the-real-one"
    )

    unknown = await client.post(
        LOGIN_URL,
        json=_credentials("nobody.here@example.com", "whatever-guess"),
        headers=_origin_headers(settings),
    )
    wrong = await client.post(
        LOGIN_URL,
        json=_credentials("real.account@example.com", "the-wrong-password"),
        headers=_origin_headers(settings),
    )

    assert unknown.status_code == 401, unknown.text
    assert wrong.status_code == 401, wrong.text
    assert unknown.status_code == wrong.status_code
    assert unknown.content == wrong.content
    assert _error_code(unknown) == _error_code(wrong) == "invalid_credentials"

    unknown_headers = {k.lower(): v for k, v in unknown.headers.items() if k.lower() != "date"}
    wrong_headers = {k.lower(): v for k, v in wrong.headers.items() if k.lower() != "date"}
    assert unknown_headers == wrong_headers


async def test_malformed_email_on_login_is_422_invalid_email(
    client: AsyncClient, settings: Settings
) -> None:
    response = await client.post(
        LOGIN_URL,
        json=_credentials("not-an-email", A_STRONG_PASSWORD),
        headers=_origin_headers(settings),
    )

    assert response.status_code == 422, response.text
    assert _error_code(response) == "invalid_email"


async def test_logging_in_twice_mints_two_independent_logins(
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    password_hasher: Argon2PasswordHasher,
    clock: FixedClock,
) -> None:
    """I-13: a second login is a **new** `Login`; the first is not revoked, only unreachable in this
    browser (its cookie was overwritten). Two logins must carry two different refresh tokens."""
    await _seed_user(
        session, password_hasher, clock, email="twice@example.com", password="the-real-password"
    )
    body = _credentials("twice@example.com", "the-real-password")

    first = await client.post(LOGIN_URL, json=body, headers=_origin_headers(settings))
    first_token = _refresh_cookie_attrs(first)["__value__"] if first.status_code == 200 else None
    assert first.status_code == 200, first.text

    second = await client.post(LOGIN_URL, json=body, headers=_origin_headers(settings))

    assert second.status_code == 200, second.text
    second_token = _refresh_cookie_attrs(second)["__value__"]
    assert second_token != first_token


# ---------------------------------------------------------------------------------------------
# I-14…I-17 / AC-27 — rate limiting and Redis
# ---------------------------------------------------------------------------------------------


async def test_more_than_the_login_ip_limit_returns_429_with_retry_after(
    app: FastAPI, settings: Settings
) -> None:
    _override_settings(app, settings, login_rate_limit_per_ip_per_hour=2)

    async def attempt(email: str) -> Response:
        async with _new_client(app) as c:
            return await c.post(
                LOGIN_URL,
                json=_credentials(email, "whatever-password-1"),
                headers=_origin_headers(settings),
            )

    first = await attempt("a1@example.com")
    second = await attempt("a2@example.com")
    third = await attempt("a3@example.com")

    assert first.status_code == 401, first.text
    assert second.status_code == 401, second.text
    assert third.status_code == 429, third.text
    assert _error_code(third) == "rate_limited"
    assert "retry-after" in {name.lower() for name in third.headers}


async def test_more_than_the_login_email_limit_returns_429_across_different_ips(
    app: FastAPI, client: AsyncClient, settings: Settings
) -> None:
    """The per-email limit must bind however many IPs the attempts come from — vary
    `X-Forwarded-For` (one trusted hop, `client_ip`'s rule) while raising the per-IP limit clear of
    the way, so only the email-scoped counter can be what trips."""
    _override_settings(
        app,
        settings,
        login_rate_limit_per_email_per_hour=2,
        login_rate_limit_per_ip_per_hour=1000,
    )
    body = _credentials("targeted@example.com", "whatever-password-1")

    first = await client.post(
        LOGIN_URL, json=body, headers={**_origin_headers(settings), "x-forwarded-for": "10.0.0.1"}
    )
    second = await client.post(
        LOGIN_URL, json=body, headers={**_origin_headers(settings), "x-forwarded-for": "10.0.0.2"}
    )
    third = await client.post(
        LOGIN_URL, json=body, headers={**_origin_headers(settings), "x-forwarded-for": "10.0.0.3"}
    )

    assert first.status_code == 401, first.text
    assert second.status_code == 401, second.text
    assert third.status_code == 429, third.text
    assert _error_code(third) == "rate_limited"


async def test_more_than_the_register_ip_limit_returns_429(
    app: FastAPI, settings: Settings
) -> None:
    _override_settings(app, settings, register_rate_limit_per_ip_per_hour=2)

    async def attempt(email: str) -> Response:
        async with _new_client(app) as c:
            return await c.post(
                REGISTER_URL,
                json=_credentials(email, A_STRONG_PASSWORD),
                headers=_origin_headers(settings),
            )

    first = await attempt("b1@example.com")
    second = await attempt("b2@example.com")
    third = await attempt("b3@example.com")

    assert first.status_code == 202, first.text
    assert second.status_code == 202, second.text
    assert third.status_code == 429, third.text
    assert _error_code(third) == "rate_limited"


async def test_register_with_redis_unreachable_is_503_rate_limit_unavailable(
    client: AsyncClient, app: FastAPI, settings: Settings
) -> None:
    _override_settings(app, settings, redis_url="redis://127.0.0.1:1/0")

    response = await client.post(
        REGISTER_URL,
        json=_credentials("someone@example.com", A_STRONG_PASSWORD),
        headers=_origin_headers(settings),
    )

    assert response.status_code == 503, response.text
    assert _error_code(response) == "rate_limit_unavailable"


async def test_login_with_redis_unreachable_is_503_rate_limit_unavailable(
    client: AsyncClient, app: FastAPI, settings: Settings
) -> None:
    _override_settings(app, settings, redis_url="redis://127.0.0.1:1/0")

    response = await client.post(
        LOGIN_URL,
        json=_credentials("someone@example.com", "whatever-password-1"),
        headers=_origin_headers(settings),
    )

    assert response.status_code == 503, response.text
    assert _error_code(response) == "rate_limit_unavailable"


async def test_the_email_never_appears_in_any_rate_limiter_redis_key(
    client: AsyncClient, settings: Settings
) -> None:
    """AC-27: `SCAN` the test Redis after a login attempt and confirm no key contains the plaintext
    email, in either case."""
    email = "must-not-leak@example.com"
    await client.post(
        LOGIN_URL,
        json=_credentials(email, "whatever-password-1"),
        headers=_origin_headers(settings),
    )

    redis = create_redis(settings.redis_url)
    try:
        async for key in redis.scan_iter(match="rl:auth:login:*"):
            key_str = key.decode() if isinstance(key, bytes) else key
            assert email not in key_str
            assert email.lower() not in key_str
    finally:
        await redis.aclose()


async def test_refresh_with_redis_unreachable_still_succeeds_with_a_valid_cookie(
    app: FastAPI,
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    password_hasher: Argon2PasswordHasher,
    clock: FixedClock,
) -> None:
    """I-18 / AC-27: `refresh` has no limiter and no Redis dependency at all — a Redis outage must
    never sign anybody out."""
    user = await _seed_user(session, password_hasher, clock)
    _login, raw_token = await _seed_login(session, user, clock)
    client.cookies.set(REFRESH_COOKIE_NAME, raw_token)
    _override_settings(app, settings, redis_url="redis://127.0.0.1:1/0")
    # Judge expiry by the seeding clock, not the wall clock: the login is seeded at the fixed
    # 2026-09-04 with a 30-day TTL, so without this the test began failing on 2026-10-04.
    app.dependency_overrides[get_clock] = lambda: clock

    response = await client.post(REFRESH_URL, headers=_origin_headers(settings))

    assert response.status_code == 200, response.text


async def test_logout_with_redis_unreachable_still_succeeds(
    app: FastAPI,
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    password_hasher: Argon2PasswordHasher,
    clock: FixedClock,
) -> None:
    user = await _seed_user(session, password_hasher, clock)
    _login, raw_token = await _seed_login(session, user, clock)
    client.cookies.set(REFRESH_COOKIE_NAME, raw_token)
    _override_settings(app, settings, redis_url="redis://127.0.0.1:1/0")

    response = await client.post(LOGOUT_URL, headers=_origin_headers(settings))

    assert response.status_code == 204, response.text


async def test_me_with_redis_unreachable_still_succeeds(
    app: FastAPI,
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    password_hasher: Argon2PasswordHasher,
    clock: FixedClock,
) -> None:
    user = await _seed_user(session, password_hasher, clock)
    tokens = JwtAccessTokens(
        settings.jwt_signing_key.get_secret_value(),
        timedelta(minutes=settings.access_token_ttl_minutes),
    )
    issued = tokens.issue(user.id, clock.now())
    app.dependency_overrides[get_clock] = lambda: clock
    _override_settings(app, settings, redis_url="redis://127.0.0.1:1/0")

    response = await client.get(ME_URL, headers={"Authorization": f"Bearer {issued.token}"})

    assert response.status_code == 200, response.text


# ---------------------------------------------------------------------------------------------
# AC-24 — the refresh cookie's exact attributes
# ---------------------------------------------------------------------------------------------


async def test_a_login_cookie_is_secure_when_the_app_is_built_for_production(
    settings: Settings,
    session: AsyncSession,
    engine: AsyncEngine,
    password_hasher: Argon2PasswordHasher,
    clock: FixedClock,
) -> None:
    """The `test_intake.py` precedent: a second, real `FastAPI` app built with `app_env="production"`
    — `Secure` is present for at least one endpoint (AC-24)."""
    await _seed_user(
        session, password_hasher, clock, email="prod.cookie@example.com", password="a-real-password"
    )
    prod_settings = settings.model_copy(update={"app_env": "production"})
    prod_app = create_app(prod_settings)
    prod_app.dependency_overrides[get_session] = lambda: session
    prod_app.state.settings = prod_settings
    prod_app.state.engine = engine
    prod_app.state.session_factory = lambda: session
    prod_app.state.celery = celery_app
    prod_app.state.password_hasher = password_hasher

    async with AsyncClient(
        transport=ASGITransport(app=prod_app, raise_app_exceptions=False),
        base_url="https://testserver",
    ) as prod_client:
        response = await prod_client.post(
            LOGIN_URL,
            json=_credentials("prod.cookie@example.com", "a-real-password"),
            headers=_origin_headers(prod_settings),
        )

    assert response.status_code == 200, response.text
    attrs = _refresh_cookie_attrs(response)
    assert attrs["secure"] is True


async def test_refresh_rotates_the_cookie_with_max_age_equal_to_the_remaining_lifetime(
    app: FastAPI,
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    password_hasher: Argon2PasswordHasher,
) -> None:
    """A rotation on day N must carry `Max-Age = (ttl_days - N) * 86400`, never the full lifetime
    again (AC-24, OQ-9: rotation never extends a login)."""
    t0 = FixedClock(datetime(2026, 1, 1, tzinfo=UTC))
    app.dependency_overrides[get_clock] = lambda: t0
    user = await _seed_user(session, password_hasher, t0)
    _login, raw_token = await _seed_login(session, user, t0, ttl_days=30)
    client.cookies.set(REFRESH_COOKIE_NAME, raw_token)

    advanced_days = 5
    t1 = FixedClock(t0.now() + timedelta(days=advanced_days))
    app.dependency_overrides[get_clock] = lambda: t1

    response = await client.post(REFRESH_URL, headers=_origin_headers(settings))

    assert response.status_code == 200, response.text
    attrs = _refresh_cookie_attrs(response)
    expected_max_age = (30 - advanced_days) * 86400
    assert int(str(attrs["max-age"])) == expected_max_age
    assert attrs["path"] == "/api/auth"
    assert str(attrs["samesite"]).lower() == "strict"
    assert attrs["__value__"] != raw_token


async def test_logout_clears_the_cookie_with_the_same_attributes_and_max_age_zero(
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    password_hasher: Argon2PasswordHasher,
    clock: FixedClock,
) -> None:
    user = await _seed_user(session, password_hasher, clock)
    _login, raw_token = await _seed_login(session, user, clock)
    client.cookies.set(REFRESH_COOKIE_NAME, raw_token)

    response = await client.post(LOGOUT_URL, headers=_origin_headers(settings))

    assert response.status_code == 204, response.text
    attrs = _refresh_cookie_attrs(response)
    assert attrs["path"] == "/api/auth"
    assert str(attrs["samesite"]).lower() == "strict"
    assert "domain" not in attrs
    assert int(str(attrs["max-age"])) == 0


async def test_a_refresh_failure_also_clears_the_cookie(
    client: AsyncClient, settings: Settings
) -> None:
    client.cookies.set(REFRESH_COOKIE_NAME, "a-token-that-was-never-minted-by-this-server-xx")

    response = await client.post(REFRESH_URL, headers=_origin_headers(settings))

    assert response.status_code == 401, response.text
    attrs = _refresh_cookie_attrs(response)
    assert int(str(attrs["max-age"])) == 0
    assert attrs["path"] == "/api/auth"


# ---------------------------------------------------------------------------------------------
# I-19…I-23, I-27 — refresh's failure contract
# ---------------------------------------------------------------------------------------------


async def test_refresh_with_no_cookie_is_401_not_signed_in_and_sets_no_cookie_at_all(
    client: AsyncClient, settings: Settings
) -> None:
    response = await client.post(REFRESH_URL, headers=_origin_headers(settings))

    assert response.status_code == 401, response.text
    assert _error_code(response) == "not_signed_in"
    assert _all_set_cookie_headers(response) == []


async def test_refresh_with_an_unknown_token_is_401_and_clears_the_cookie(
    client: AsyncClient, settings: Settings
) -> None:
    client.cookies.set(REFRESH_COOKIE_NAME, "x" * 43)

    response = await client.post(REFRESH_URL, headers=_origin_headers(settings))

    assert response.status_code == 401, response.text
    assert _error_code(response) == "not_signed_in"
    assert int(str(_refresh_cookie_attrs(response)["max-age"])) == 0


async def test_refresh_of_an_expired_login_is_401_and_the_login_is_deleted_on_sight(
    app: FastAPI,
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    password_hasher: Argon2PasswordHasher,
) -> None:
    t0 = FixedClock(datetime(2026, 1, 1, tzinfo=UTC))
    app.dependency_overrides[get_clock] = lambda: t0
    user = await _seed_user(session, password_hasher, t0)
    login, raw_token = await _seed_login(session, user, t0, ttl_days=30)
    client.cookies.set(REFRESH_COOKIE_NAME, raw_token)

    past_expiry = FixedClock(t0.now() + timedelta(days=31))
    app.dependency_overrides[get_clock] = lambda: past_expiry

    response = await client.post(REFRESH_URL, headers=_origin_headers(settings))

    assert response.status_code == 401, response.text
    assert _error_code(response) == "not_signed_in"
    assert int(str(_refresh_cookie_attrs(response)["max-age"])) == 0

    logins = _login_repository(session)
    assert await logins.find_by_current_token_hash(login.current_token_hash) is None


async def test_refreshing_with_the_immediate_predecessor_within_the_grace_is_409(
    app: FastAPI,
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    password_hasher: Argon2PasswordHasher,
) -> None:
    """I-23: a login already rotated once (simulated directly through the repository, since the
    refresh endpoint that would normally do this is itself unimplemented); the OLD token, presented
    again within the 10 s grace, must be a 409 with the cookie left untouched."""
    t0 = FixedClock(datetime(2026, 1, 1, tzinfo=UTC))
    app.dependency_overrides[get_clock] = lambda: t0
    user = await _seed_user(session, password_hasher, t0)
    login, old_raw_token = await _seed_login(session, user, t0, ttl_days=30)

    logins = _login_repository(session)
    new_minted = mint_refresh_token()
    retired = login.rotate(new_minted.token_hash, t0.now())
    await logins.save_rotation(login, retired)
    await session.commit()

    client.cookies.set(REFRESH_COOKIE_NAME, old_raw_token)
    within_grace = FixedClock(t0.now() + timedelta(seconds=5))
    app.dependency_overrides[get_clock] = lambda: within_grace

    response = await client.post(REFRESH_URL, headers=_origin_headers(settings))

    assert response.status_code == 409, response.text
    assert _error_code(response) == "refresh_in_progress"
    assert _all_set_cookie_headers(response) == []


async def test_reusing_a_retired_token_beyond_the_grace_is_401_and_revokes_the_login(
    app: FastAPI,
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    password_hasher: Argon2PasswordHasher,
) -> None:
    """I-24: the same shape as the test above, but presented outside the 10 s grace — a reuse, not a
    race. The whole login is deleted."""
    t0 = FixedClock(datetime(2026, 1, 1, tzinfo=UTC))
    app.dependency_overrides[get_clock] = lambda: t0
    user = await _seed_user(session, password_hasher, t0)
    login, old_raw_token = await _seed_login(session, user, t0, ttl_days=30)

    logins = _login_repository(session)
    new_minted = mint_refresh_token()
    retired = login.rotate(new_minted.token_hash, t0.now())
    await logins.save_rotation(login, retired)
    await session.commit()

    client.cookies.set(REFRESH_COOKIE_NAME, old_raw_token)
    beyond_grace = FixedClock(t0.now() + timedelta(seconds=11))
    app.dependency_overrides[get_clock] = lambda: beyond_grace

    response = await client.post(REFRESH_URL, headers=_origin_headers(settings))

    assert response.status_code == 401, response.text
    assert _error_code(response) == "refresh_token_reused"
    assert int(str(_refresh_cookie_attrs(response)["max-age"])) == 0

    assert await logins.find_by_current_token_hash(new_minted.token_hash) is None
    assert await logins.find_by_retired_token_hash(hash_refresh_token(old_raw_token)) is None


async def test_refresh_after_logout_is_401_not_signed_in(
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    password_hasher: Argon2PasswordHasher,
    clock: FixedClock,
) -> None:
    user = await _seed_user(session, password_hasher, clock)
    login, raw_token = await _seed_login(session, user, clock)
    logins = _login_repository(session)
    await logins.remove(login.id)
    await session.commit()
    client.cookies.set(REFRESH_COOKIE_NAME, raw_token)

    response = await client.post(REFRESH_URL, headers=_origin_headers(settings))

    assert response.status_code == 401, response.text
    assert _error_code(response) == "not_signed_in"


# ---------------------------------------------------------------------------------------------
# I-29…I-31 — logout's failure contract
# ---------------------------------------------------------------------------------------------


async def test_logout_with_a_valid_cookie_is_204_and_clears_it(
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    password_hasher: Argon2PasswordHasher,
    clock: FixedClock,
) -> None:
    user = await _seed_user(session, password_hasher, clock)
    _login, raw_token = await _seed_login(session, user, clock)
    client.cookies.set(REFRESH_COOKIE_NAME, raw_token)

    response = await client.post(LOGOUT_URL, headers=_origin_headers(settings))

    assert response.status_code == 204, response.text
    assert int(str(_refresh_cookie_attrs(response)["max-age"])) == 0


async def test_logout_with_no_cookie_is_204_and_idempotent(
    client: AsyncClient, settings: Settings
) -> None:
    first = await client.post(LOGOUT_URL, headers=_origin_headers(settings))
    assert first.status_code == 204, first.text

    second = await client.post(LOGOUT_URL, headers=_origin_headers(settings))
    assert second.status_code == 204, second.text


async def test_logout_with_postgres_down_is_503_and_does_not_clear_the_cookie(
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    password_hasher: Argon2PasswordHasher,
    clock: FixedClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = await _seed_user(session, password_hasher, clock)
    _login, raw_token = await _seed_login(session, user, clock)
    client.cookies.set(REFRESH_COOKIE_NAME, raw_token)

    async def _raise_sqlalchemy_error() -> None:
        raise SQLAlchemyError("simulated commit failure (I-31)")

    monkeypatch.setattr(session, "commit", _raise_sqlalchemy_error)

    response = await client.post(LOGOUT_URL, headers=_origin_headers(settings))

    assert response.status_code == 503, response.text
    assert _error_code(response) == "service_unavailable"
    assert _cookie_header_named(response, REFRESH_COOKIE_NAME) is None


# ---------------------------------------------------------------------------------------------
# I-39, AC-34 — /me's failure contract and its uniform bearer refusal
# ---------------------------------------------------------------------------------------------


async def test_me_with_a_valid_token_but_a_deleted_user_is_401_not_signed_in(
    app: FastAPI,
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    password_hasher: Argon2PasswordHasher,
    clock: FixedClock,
) -> None:
    user = await _seed_user(session, password_hasher, clock)
    tokens = JwtAccessTokens(
        settings.jwt_signing_key.get_secret_value(),
        timedelta(minutes=settings.access_token_ttl_minutes),
    )
    issued = tokens.issue(user.id, clock.now())
    app.dependency_overrides[get_clock] = lambda: clock

    users = _user_repository(session)
    await session.delete(await users.get(user.id))
    await session.commit()

    response = await client.get(ME_URL, headers={"Authorization": f"Bearer {issued.token}"})

    assert response.status_code == 401, response.text
    assert _error_code(response) == "not_signed_in"


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "not-even-bearer-shaped"},
        {"Authorization": "Bearer not.a.jwt"},
        {"Authorization": f"Bearer {'x' * 43}"},
    ],
    ids=["no-header", "not-bearer-scheme", "malformed-jwt", "refresh-shaped-token"],
)
async def test_every_bearer_refusal_is_the_same_401_with_the_same_challenge(
    client: AsyncClient, headers: dict[str, str]
) -> None:
    """AC-34: identical body and the `WWW-Authenticate: Bearer error="invalid_token"` challenge for
    every refusal reason, never saying which check caught it. **Already passes**: `require_user` is
    resolved before the handler body, for every one of these cases."""
    response = await client.get(ME_URL, headers=headers)

    assert response.status_code == 401, response.text
    assert _error_code(response) == "invalid_access_token"
    assert response.headers.get("www-authenticate") == 'Bearer error="invalid_token"'


async def test_the_bearer_refusal_body_is_identical_across_two_different_reasons(
    client: AsyncClient, settings: Settings
) -> None:
    """A second half of AC-34's "identical body" claim: a missing header and a malformed token must
    produce byte-identical bodies, never a hint about which reason applied."""
    no_header = await client.get(ME_URL)
    malformed = await client.get(ME_URL, headers={"Authorization": "Bearer not.a.jwt"})

    assert no_header.status_code == malformed.status_code == 401
    assert no_header.content == malformed.content


async def test_a_successful_me_call_returns_exactly_the_user_shape(
    app: FastAPI,
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    password_hasher: Argon2PasswordHasher,
    clock: FixedClock,
) -> None:
    user = await _seed_user(session, password_hasher, clock, email="me.shape@example.com")
    tokens = JwtAccessTokens(
        settings.jwt_signing_key.get_secret_value(),
        timedelta(minutes=settings.access_token_ttl_minutes),
    )
    issued = tokens.issue(user.id, clock.now())
    app.dependency_overrides[get_clock] = lambda: clock

    response = await client.get(ME_URL, headers={"Authorization": f"Bearer {issued.token}"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"id", "email", "created_at", "role"}
    assert body["role"] == "user"
    assert body["email"] == "me.shape@example.com"


USER_KEYS = {"id", "email", "created_at", "role"}


async def test_the_user_in_a_login_response_has_exactly_the_four_keys(
    client: AsyncClient, settings: Settings, session: AsyncSession
) -> None:
    """AC-12: login's `user` object gains `role`, nothing else."""
    from tests.api.me_support import A_PASSWORD, LOGIN_URL

    email = "login.shape@example.com"
    await seed_user_and_sign_in(client, settings, email=email)
    response = await client.post(
        LOGIN_URL,
        json={"email": email, "password": A_PASSWORD},
        headers=_origin_headers(settings),
    )

    assert response.status_code == 200, response.text
    assert set(response.json()["user"]) == USER_KEYS
    assert response.json()["user"]["role"] == "user"


async def test_the_user_in_a_refresh_response_has_exactly_the_four_keys(
    app: FastAPI,
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    password_hasher: Argon2PasswordHasher,
    clock: FixedClock,
) -> None:
    """AC-12: refresh's `user` object has the same four keys."""
    app.dependency_overrides[get_clock] = lambda: clock
    user = await _seed_user(session, password_hasher, clock, email="refresh.shape@example.com")
    _login, raw_token = await _seed_login(session, user, clock)
    client.cookies.set(REFRESH_COOKIE_NAME, raw_token)

    response = await client.post(REFRESH_URL, headers=_origin_headers(settings))

    assert response.status_code == 200, response.text
    assert set(response.json()["user"]) == USER_KEYS
    assert response.json()["user"]["role"] == "user"


async def test_me_reports_a_grant_committed_elsewhere_with_the_same_token_and_the_token_is_unchanged(
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    password_hasher: Argon2PasswordHasher,
    clock: FixedClock,
    app: FastAPI,
) -> None:
    """AC-12 (read at request time) and AC-13 (no `role` claim: five claims before and after).

    The same token is presented twice around a grant. (The cross-connection proof is AC-18 in
    `test_admin_firewall.py`; this test is about `/me`'s shape and the token's claims.)"""
    import jwt  # PyJWT, already a dependency (access tokens)

    user = await _seed_user(session, password_hasher, clock, email="grant.shape@example.com")
    tokens = JwtAccessTokens(
        settings.jwt_signing_key.get_secret_value(),
        timedelta(minutes=settings.access_token_ttl_minutes),
    )
    issued = tokens.issue(user.id, clock.now())
    app.dependency_overrides[get_clock] = lambda: clock
    headers = {"Authorization": f"Bearer {issued.token}"}

    claims_before = set(jwt.decode(issued.token, options={"verify_signature": False}))
    first = await client.get(ME_URL, headers=headers)
    await session.execute(
        text("UPDATE identity_user SET role = 'admin' WHERE id = :id"), {"id": user.id.value}
    )
    await session.commit()
    session.expire_all()  # a Core UPDATE does not touch the identity map (2.4); production has a session per request
    second = await client.get(ME_URL, headers=headers)
    claims_after = set(jwt.decode(issued.token, options={"verify_signature": False}))

    assert first.status_code == second.status_code == 200
    assert first.json()["role"] == "user"
    assert second.json()["role"] == "admin"
    assert claims_before == claims_after == {"iss", "aud", "sub", "iat", "exp"}


# ---------------------------------------------------------------------------------------------
# I-41 — Postgres down on register / login / refresh / me
# ---------------------------------------------------------------------------------------------


async def test_register_with_postgres_down_is_503(
    client: AsyncClient, settings: Settings, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _raise_sqlalchemy_error() -> None:
        raise SQLAlchemyError("simulated commit failure (I-41)")

    monkeypatch.setattr(session, "commit", _raise_sqlalchemy_error)

    response = await client.post(
        REGISTER_URL,
        json=_credentials("db.down@example.com", A_STRONG_PASSWORD),
        headers=_origin_headers(settings),
    )

    assert response.status_code == 503, response.text
    assert _error_code(response) == "service_unavailable"


async def test_login_with_postgres_down_is_503(
    client: AsyncClient, settings: Settings, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _raise_sqlalchemy_error() -> None:
        raise SQLAlchemyError("simulated commit failure (I-41)")

    monkeypatch.setattr(session, "commit", _raise_sqlalchemy_error)

    response = await client.post(
        LOGIN_URL,
        json=_credentials("db.down@example.com", "whatever-password-1"),
        headers=_origin_headers(settings),
    )

    assert response.status_code == 503, response.text
    assert _error_code(response) == "service_unavailable"


async def test_refresh_with_postgres_down_is_503(
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    password_hasher: Argon2PasswordHasher,
    clock: FixedClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = await _seed_user(session, password_hasher, clock)
    _login, raw_token = await _seed_login(session, user, clock)
    client.cookies.set(REFRESH_COOKIE_NAME, raw_token)

    async def _raise_sqlalchemy_error() -> None:
        raise SQLAlchemyError("simulated commit failure (I-41)")

    monkeypatch.setattr(session, "commit", _raise_sqlalchemy_error)

    response = await client.post(REFRESH_URL, headers=_origin_headers(settings))

    assert response.status_code == 503, response.text
    assert _error_code(response) == "service_unavailable"


async def test_me_with_postgres_down_is_503(
    app: FastAPI,
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    password_hasher: Argon2PasswordHasher,
    clock: FixedClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = await _seed_user(session, password_hasher, clock)
    tokens = JwtAccessTokens(
        settings.jwt_signing_key.get_secret_value(),
        timedelta(minutes=settings.access_token_ttl_minutes),
    )
    issued = tokens.issue(user.id, clock.now())
    app.dependency_overrides[get_clock] = lambda: clock

    async def _raise_sqlalchemy_error() -> None:
        raise SQLAlchemyError("simulated commit failure (I-41)")

    monkeypatch.setattr(session, "commit", _raise_sqlalchemy_error)

    response = await client.get(ME_URL, headers={"Authorization": f"Bearer {issued.token}"})

    assert response.status_code == 503, response.text
    assert _error_code(response) == "service_unavailable"


async def test_argon2_hasher_failure_on_login_is_503(
    client: AsyncClient,
    app: FastAPI,
    settings: Settings,
    session: AsyncSession,
    password_hasher: Argon2PasswordHasher,
    clock: FixedClock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """I-45: the hasher's own floor (`PasswordHashingFailed`) maps to the same `service_unavailable`
    503 as a dead Postgres — to the client, both mean "try again".

    The explosion must be injected **below** the adapter's `except Exception` floor
    (`Argon2PasswordHasher.verify`, `password_hasher.py` ~143-158): patching the adapter's own
    `verify` replaces the floor itself, so the RuntimeError would reach the route untranslated as an
    honest 500 rather than exercising I-45's mapping. Patching the underlying
    `argon2.PasswordHasher.verify` — the call the adapter's `_verify_sync` makes on its executor
    thread — leaves the floor in place and lets it do the translating, exactly as T25's adapter-level
    floor test (`test_an_unexpected_exception_during_verify_becomes_exactly_password_hashing_failed`)
    does it. `argon2.PasswordHasher` is a slotted class, so the class itself is patched rather than
    the instance.
    """
    password = "a-real-password"
    await _seed_user(
        session,
        password_hasher,
        clock,
        email="hasher.floor@example.com",
        password=password,
    )

    def boom(self: Argon2Library, stored: str, secret: bytes) -> None:
        raise RuntimeError("argon2-cffi exploded")

    monkeypatch.setattr(Argon2Library, "verify", boom)

    with caplog.at_level(logging.INFO):
        response = await client.post(
            LOGIN_URL,
            json=_credentials("hasher.floor@example.com", password),
            headers=_origin_headers(settings),
        )

    assert response.status_code == 503, response.text
    assert _error_code(response) == "service_unavailable"

    # The floor's own promise (password_hasher.py's `_hashing_failed`): the exception's type is
    # loggable, its message and the password are not.
    assert "RuntimeError" in caplog.text, "the exception's type should reach the log"
    assert password not in caplog.text
    assert "argon2-cffi exploded" not in caplog.text


# ---------------------------------------------------------------------------------------------
# AC-26 — Cache-Control: no-store
# ---------------------------------------------------------------------------------------------


async def test_a_successful_register_carries_cache_control_no_store(
    client: AsyncClient, settings: Settings
) -> None:
    response = await client.post(
        REGISTER_URL,
        json=_credentials("no.store@example.com", A_STRONG_PASSWORD),
        headers=_origin_headers(settings),
    )

    assert response.status_code == 202, response.text
    assert response.headers.get("cache-control") == "no-store"


async def test_a_successful_refresh_carries_cache_control_no_store(
    app: FastAPI,
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    password_hasher: Argon2PasswordHasher,
    clock: FixedClock,
) -> None:
    user = await _seed_user(session, password_hasher, clock)
    _login, raw_token = await _seed_login(session, user, clock)
    client.cookies.set(REFRESH_COOKIE_NAME, raw_token)
    # Judge expiry by the seeding clock, not the wall clock: the login is seeded at the fixed
    # 2026-09-04 with a 30-day TTL, so without this the test began failing on 2026-10-04.
    app.dependency_overrides[get_clock] = lambda: clock

    response = await client.post(REFRESH_URL, headers=_origin_headers(settings))

    assert response.status_code == 200, response.text
    assert response.headers.get("cache-control") == "no-store"


async def test_a_successful_me_call_carries_cache_control_no_store(
    app: FastAPI,
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    password_hasher: Argon2PasswordHasher,
    clock: FixedClock,
) -> None:
    user = await _seed_user(session, password_hasher, clock)
    tokens = JwtAccessTokens(
        settings.jwt_signing_key.get_secret_value(),
        timedelta(minutes=settings.access_token_ttl_minutes),
    )
    issued = tokens.issue(user.id, clock.now())
    app.dependency_overrides[get_clock] = lambda: clock

    response = await client.get(ME_URL, headers={"Authorization": f"Bearer {issued.token}"})

    assert response.status_code == 200, response.text
    assert response.headers.get("cache-control") == "no-store"


# ---------------------------------------------------------------------------------------------
# AC-29 — no `__Host-tc_guest` Set-Cookie on any of the five endpoints' outcomes
# ---------------------------------------------------------------------------------------------


async def test_none_of_the_five_endpoints_ever_sets_a_guest_cookie(
    app: FastAPI,
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    password_hasher: Argon2PasswordHasher,
    clock: FixedClock,
) -> None:
    user = await _seed_user(session, password_hasher, clock, email="no.guest.leak@example.com")
    _login, raw_token = await _seed_login(session, user, clock)
    tokens = JwtAccessTokens(
        settings.jwt_signing_key.get_secret_value(),
        timedelta(minutes=settings.access_token_ttl_minutes),
    )
    issued = tokens.issue(user.id, clock.now())
    app.dependency_overrides[get_clock] = lambda: clock

    register_response = await client.post(
        REGISTER_URL,
        json=_credentials("another.new.user@example.com", A_STRONG_PASSWORD),
        headers=_origin_headers(settings),
    )
    login_response = await client.post(
        LOGIN_URL,
        json=_credentials("no.guest.leak@example.com", "wrong-password-on-purpose"),
        headers=_origin_headers(settings),
    )
    client.cookies.set(REFRESH_COOKIE_NAME, raw_token)
    refresh_response = await client.post(REFRESH_URL, headers=_origin_headers(settings))
    logout_response = await client.post(LOGOUT_URL, headers=_origin_headers(settings))
    me_response = await client.get(ME_URL, headers={"Authorization": f"Bearer {issued.token}"})

    for response in (
        register_response,
        login_response,
        refresh_response,
        logout_response,
        me_response,
    ):
        assert _cookie_header_named(response, GUEST_COOKIE_NAME) is None, (
            f"{response.request.url} must never set __Host-tc_guest"
        )


# ---------------------------------------------------------------------------------------------
# AC-33 — the last two codes: origin_not_allowed and invalid_access_token are covered above;
# refresh_in_progress, refresh_token_reused, email_already_registered, invalid_credentials,
# not_signed_in, rate_limited, rate_limit_unavailable, service_unavailable, invalid_email,
# password_too_short, password_too_long, password_matches_email and validation_error are each
# reached by a test above. This closes the set with the one row nothing above exercises on its own.
# ---------------------------------------------------------------------------------------------


async def test_double_at_sign_email_on_register_is_422_invalid_email(
    client: AsyncClient, settings: Settings
) -> None:
    """A second, independent AC-2 refusal case (distinct from the "no @ at all" shape already
    covered), so `invalid_email` is pinned against more than one input."""
    response = await client.post(
        REGISTER_URL,
        json=_credentials("a@@b.com", A_STRONG_PASSWORD),
        headers=_origin_headers(settings),
    )

    assert response.status_code == 422, response.text
    assert _error_code(response) == "invalid_email"


# ---------------------------------------------------------------------------------------------
# AC-30 — one credential per route: a guest route with a bearer, and /me with a guest cookie
# ---------------------------------------------------------------------------------------------


async def test_me_with_a_live_guest_cookie_and_no_bearer_behaves_as_without_it(
    client: AsyncClient, session: AsyncSession, clock: FixedClock
) -> None:
    """`/me` must never touch `__Host-tc_guest` — with a live guest cookie and no bearer, the
    answer is
    identical to having no cookie at all (401 `invalid_access_token`)."""
    _guest_session, raw_guest_token = await _seed_guest_session(session, clock)
    client.cookies.set(GUEST_COOKIE_NAME, raw_guest_token)

    response = await client.get(ME_URL)

    assert response.status_code == 401, response.text
    assert _error_code(response) == "invalid_access_token"


async def test_a_guest_route_called_with_a_valid_bearer_and_no_guest_cookie_behaves_as_without_it(
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    password_hasher: Argon2PasswordHasher,
    clock: FixedClock,
) -> None:
    user = await _seed_user(session, password_hasher, clock)
    tokens = JwtAccessTokens(
        settings.jwt_signing_key.get_secret_value(),
        timedelta(minutes=settings.access_token_ttl_minutes),
    )
    issued = tokens.issue(user.id, clock.now())

    response = await client.get(
        "/api/base-cvs", headers={"Authorization": f"Bearer {issued.token}"}
    )

    # `/api/base-cvs` (GET) answers to `require_guest_session` alone; a bearer token carries no
    # weight there at all, so the answer must be exactly what a request with neither credential gets:
    # 401 `guest_session_expired` (there is no guest cookie either).
    assert response.status_code == 401, response.text
    assert _error_code(response) == "guest_session_expired"


# ---------------------------------------------------------------------------------------------
# /verify round 1, findings 5-9 — test-after (the reviewer believes each of these is already
# correct); every test below was proven sensitive by a LOCAL mutation of the production line it
# guards, observed red, then the source restored byte-exact
# (`git diff --quiet <file>`) before this commit. The mutation and the exact failure are recorded in
# each test's own docstring or in the commit body, per CLAUDE.md's rule that an assertion which has
# never been observed failing is a docblock, not a test.
# ---------------------------------------------------------------------------------------------


async def test_two_clients_through_the_same_two_proxy_chain_get_different_rate_limit_keys(
    app: FastAPI, settings: Settings
) -> None:
    """Finding 5, HTTP level. Production is client -> Traefik -> nginx -> api
    (`trusted_proxy_hops=2`); two distinct clients whose requests both pass through the same
    Traefik hop must land in two separate `rl:auth:login:ip:*` buckets, keyed on the real client
    address — never a shared bucket, and never one keyed on a client-forged prefix.
    `test_client_ip.py` pins the pure function; this pins the same property through a real request
    and a real Redis SCAN, reading the actual identifier out of the key rather than only counting
    keys — a count alone cannot tell "keyed correctly" from "keyed on the wrong, but still
    per-client-distinct, entry".

    Each simulated client sends a 3-entry chain (`<client-forged>, <real-client>, <traefik>`) — a
    client can prepend anything to its own `X-Forwarded-For` before Traefik ever sees the request,
    so this is the adversarial shape, not the clean 2-entry case both proxies alone would produce.

    Mutation: `client_ip`'s `hops[-trusted_proxy_hops]` changed to `hops[0]` and back (the same
    "trust the client-forged leftmost entry" bug `test_client_ip.py` mutates). Under the mutation
    this test went RED: `assert identifiers == {"203.0.113.10", "203.0.113.20"}` failed with
    `identifiers == {"9.9.9.9", "8.8.8.8"}` — both clients still got *a* key each (a bare key-count
    assertion would have missed this), but keyed on the forged prefix instead of the real address.
    Restored byte-exact; `git diff --quiet api/src/tailorcraft/infrastructure/rate_limit.py`
    confirmed it.
    """
    modified = _override_settings(
        app, settings, trusted_proxy_hops=2, login_rate_limit_per_ip_per_hour=1000
    )

    async with _new_client(app) as c:
        await c.post(
            LOGIN_URL,
            json=_credentials("proxy.client.a@example.com", "whatever-password-1"),
            headers={
                **_origin_headers(modified),
                "x-forwarded-for": "9.9.9.9, 203.0.113.10, 10.0.0.1",
            },
        )
    async with _new_client(app) as c:
        await c.post(
            LOGIN_URL,
            json=_credentials("proxy.client.b@example.com", "whatever-password-1"),
            headers={
                **_origin_headers(modified),
                "x-forwarded-for": "8.8.8.8, 203.0.113.20, 10.0.0.1",
            },
        )

    redis = create_redis(modified.redis_url)
    try:
        ip_keys = [
            key.decode() if isinstance(key, bytes) else key
            async for key in redis.scan_iter(match="rl:auth:login:ip:*")
        ]
    finally:
        await redis.aclose()

    # rl:auth:login:ip:<identifier>:<epoch_hour>
    identifiers = {key.split(":")[4] for key in ip_keys}
    assert identifiers == {"203.0.113.10", "203.0.113.20"}, ip_keys


@pytest.mark.parametrize("url", [REGISTER_URL, LOGIN_URL])
async def test_a_403_origin_refusal_creates_no_rate_limit_key(
    client: AsyncClient, settings: Settings, url: str
) -> None:
    """Finding 6 / AC-25 / I-28, first half. `require_trusted_origin` is resolved before the
    handler body that calls `_enforce(...)` — proven here by scanning Redis, not by inference from
    the hasher-call-count test above.

    Mutation: `require_trusted_origin` (`deps.py`) changed to an unconditional `return` (never
    raising, as if the check had been moved past the limiter, per the finding's instruction). Under
    the mutation this test went RED on its first assertion — `assert response.status_code == 403`
    failed (`201`/`200` instead, the request actually went through) — before the Redis assertion was
    even reached. Restored byte-exact; `git diff --quiet api/src/tailorcraft/infrastructure/api/deps.py`
    confirmed it.
    """
    response = await client.post(
        url,
        json=_credentials("origin.refused@example.com", A_STRONG_PASSWORD),
        headers={"Origin": "https://evil.example"},
    )
    assert response.status_code == 403, response.text

    redis = create_redis(settings.redis_url)
    try:
        auth_keys = [key async for key in redis.scan_iter(match="rl:auth:*")]
    finally:
        await redis.aclose()

    assert auth_keys == [], auth_keys


async def test_a_403_origin_refusal_on_refresh_rotates_nothing(
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    password_hasher: Argon2PasswordHasher,
    clock: FixedClock,
) -> None:
    """Finding 6 / AC-25 / I-28, second half (I-28: "nothing rotated"). A seeded login, presented
    with a valid cookie but a foreign `Origin`, must come back 403 with the login's `generation` and
    `version` exactly as seeded — the origin check must refuse before `refresh_login` ever touches
    the row.

    Mutation: same as above (`require_trusted_origin` forced to always return). Under the mutation
    this test went RED on `assert response.status_code == 403` (the refresh actually ran and
    rotated the cookie, answering `200`). Restored byte-exact.
    """
    user = await _seed_user(
        session, password_hasher, clock, email="origin.refused.refresh@example.com"
    )
    _login, raw_token = await _seed_login(session, user, clock)
    client.cookies.set(REFRESH_COOKIE_NAME, raw_token)

    response = await client.post(REFRESH_URL, headers={"Origin": "https://evil.example"})

    assert response.status_code == 403, response.text
    assert _error_code(response) == "origin_not_allowed"

    # Looked up by the ORIGINAL token's hash: if a rotation had gone through, this hash would no
    # longer be the login's *current* one and the lookup would come back `None`.
    reloaded = await _login_repository(session).find_by_current_token_hash(
        hash_refresh_token(raw_token)
    )
    assert reloaded is not None, "the seeded token is no longer current — a rotation happened"
    assert reloaded.generation == 1
    assert reloaded.version == 1


async def test_a_successful_login_carries_cache_control_no_store(
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    password_hasher: Argon2PasswordHasher,
    clock: FixedClock,
) -> None:
    """Finding 7 / AC-26. `register`, `refresh` and `me` already have this test; `login` and
    `logout` did not.

    Mutation: the `_no_store(response)` call inside `login`'s handler (`routers/auth.py`) commented
    out. Under the mutation this test went RED:
    `assert response.headers.get("cache-control") == "no-store"` failed with `None`. Restored
    byte-exact; `git diff --quiet api/src/tailorcraft/infrastructure/api/routers/auth.py` confirmed
    it (checked together with the logout mutation below, one at a time).
    """
    await _seed_user(session, password_hasher, clock, email="cache.control.login@example.com")

    response = await client.post(
        LOGIN_URL,
        json=_credentials("cache.control.login@example.com", A_STRONG_PASSWORD),
        headers=_origin_headers(settings),
    )

    assert response.status_code == 200, response.text
    assert response.headers.get("cache-control") == "no-store"


async def test_a_successful_logout_carries_cache_control_no_store(
    client: AsyncClient, settings: Settings
) -> None:
    """Finding 7 / AC-26, the other endpoint the existing tests skip.

    Mutation: the `_no_store(response)` call inside `logout`'s handler commented out. Under the
    mutation this test went RED the same way: `cache-control` came back `None`. Restored
    byte-exact.
    """
    response = await client.post(LOGOUT_URL, headers=_origin_headers(settings))

    assert response.status_code == 204, response.text
    assert response.headers.get("cache-control") == "no-store"


class _FailIfCalledRefreshLogin:
    """A `RefreshLogin`-shaped double that fails the test the instant it is invoked — I-40's "never
    used as a lookup key" proven as "never even reached the use case", not inferred from the
    response alone."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, presented: object, replacement: object) -> object:
        self.calls += 1
        raise AssertionError(
            "RefreshLogin must not be called for a malformed (I-40) refresh cookie"
        )


async def test_a_jwt_shaped_refresh_cookie_is_401_cleared_with_no_db_lookup(
    app: FastAPI, client: AsyncClient, settings: Settings
) -> None:
    """Finding 8 / I-40. A JWT pasted into `tc_refresh` is never used as a lookup key:
    `read_refresh_token`'s shape check (`refresh_cookie.py`) already turns it into
    `PresentedRefreshToken(token_hash=None)` before the router's `if presented.token_hash is None:`
    branch returns 401 without ever calling `refresh_login` — proven here by replacing
    `RefreshLoginDep` with a spy that raises if it is ever invoked at all, not merely by asserting
    the response shape (which a lookup that happened to also return "not found" could produce too).

    Mutation: `refresh_cookie.py`'s `_TOKEN_SHAPE` regex widened from `[A-Za-z0-9_-]{43}` to `.*`
    (so a JWT is wrongly accepted as "shaped like a token we mint"). Under the mutation this test
    went RED: the spy's `AssertionError` propagated out of the handler, and with this module's
    `raise_app_exceptions=False` client that surfaced as `response.status_code == 500` instead of
    the expected `401` — `assert response.status_code == 401` failed. Restored byte-exact;
    `git diff --quiet api/src/tailorcraft/infrastructure/api/refresh_cookie.py` confirmed it.
    """
    spy = _FailIfCalledRefreshLogin()
    app.dependency_overrides[get_refresh_login] = lambda: spy
    # Header.payload.signature shape: three dot-separated base64url segments, far longer than the
    # 43 characters `mint_refresh_token` always produces, and containing `.` at all — which alone
    # is outside `_TOKEN_SHAPE`'s character class.
    fake_jwt = (
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJhdHRhY2tlciJ9.c2lnbmF0dXJlLWdvZXMtaGVyZQ"
    )
    client.cookies.set(REFRESH_COOKIE_NAME, fake_jwt)

    response = await client.post(REFRESH_URL, headers=_origin_headers(settings))

    assert response.status_code == 401, response.text
    assert _error_code(response) == "not_signed_in"
    cleared = _refresh_cookie_attrs(response)
    assert cleared["max-age"] == "0"
    assert spy.calls == 0


async def test_refresh_succeeds_after_the_jwt_signing_key_changes_since_login(
    app: FastAPI,
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    password_hasher: Argon2PasswordHasher,
    clock: FixedClock,
) -> None:
    """Finding 9 / I-44. Refresh tokens are opaque, SHA-256-hashed rows in `identity_login` — never
    JWTs (ADR-0010 §3, ADR-0020 §7) — so rotating `JWT_SIGNING_KEY` between a login and a later
    refresh must not affect the refresh at all: `refresh` looks the cookie up by hash and mints a
    brand-new access token signed with whatever key is live *now*, never one signed at login time.

    Mutation: `get_access_tokens` (`deps.py`) changed to read a hard-coded key
    (`"mutated-signing-key-that-ignores-settings-32b"`) instead of `settings.jwt_signing_key`. Under
    the mutation this test went RED: `new_tokens.verify(...)`, built from the key this test actually
    configured, raised `AccessTokenInvalid: access token refused: bad_signature` instead of
    returning the user's id — the refreshed token was signed with a key the test never set.
    Restored byte-exact; `git diff --quiet api/src/tailorcraft/infrastructure/api/deps.py`
    confirmed it.
    """
    user = await _seed_user(session, password_hasher, clock, email="key.rotation@example.com")
    _login, raw_token = await _seed_login(session, user, clock)
    client.cookies.set(REFRESH_COOKIE_NAME, raw_token)
    # The access token this refresh mints is judged by the `Clock` port (AC-21) — pin it to the
    # same fixed instant `verify` below checks against, or the real wall clock's `iat` reads as
    # "issued in the future" against a stopped test clock and this test would fail for a reason
    # that has nothing to do with I-44.
    app.dependency_overrides[get_clock] = lambda: clock

    # A rotation that happened *after* login, simulated by overriding settings only now.
    new_key = "b" * JWT_SIGNING_KEY_MIN_BYTES
    modified = _override_settings(app, settings, jwt_signing_key=SecretStr(new_key))

    response = await client.post(REFRESH_URL, headers=_origin_headers(settings))

    assert response.status_code == 200, response.text
    new_access_token = response.json()["access_token"]
    new_tokens = JwtAccessTokens(new_key, timedelta(minutes=modified.access_token_ttl_minutes))
    verified_user_id = new_tokens.verify(new_access_token, clock.now())
    assert verified_user_id == user.id


# ---------------------------------------------------------------------------------------------
# Slice 2.2 (intake-saved-base-cvs, T19) — AC-29 / AC-30: `POST /api/auth/delete-account`, and the
# S-rows it can produce (S-39...S-46). `routers/auth.py::delete_account` currently does nothing but
# `raise NotImplementedError` (T18's SKELETON) — every assertion below is written against
# `docs/specs/intake-saved-base-cvs/feature-spec.md`, never against that body.
#
# Dedicated tests rather than widening the existing `@pytest.mark.parametrize("url", [REGISTER_URL,
# LOGIN_URL, REFRESH_URL, LOGOUT_URL])` lists above: the task list does not name those parametrize
# blocks as needing amendment for 2.2 (unlike the walker and the intake key sets, which it names
# explicitly), and `require_trusted_origin` already covers `delete-account` identically — widening
# an existing green parametrization is not what red-first asks for here.
# ---------------------------------------------------------------------------------------------

DELETE_ACCOUNT_URL = "/api/auth/delete-account"


async def _register_2_2(
    client: AsyncClient, settings: Settings, *, email: str | None = None
) -> tuple[str, str]:
    """A signed-in account for the delete-account tests. **Re-seeded in slice 2.5 (T27):** `POST
    /api/auth/register` no longer returns a token or sets `tc_refresh`, so the user is written through
    the repository and signed in through the real login route (`me_support.seed_user_and_sign_in`),
    which sets the same `tc_refresh` cookie on `client` the refresh-rotation tests below rely on. The
    name is kept so no test body changes meaning."""
    token, user_id = await seed_user_and_sign_in(
        client, settings, email=email or f"t19-delacct-{uuid4().hex}@example.com"
    )
    return token, str(user_id)


def _delete_account_headers(settings: Settings, token: str) -> dict[str, str]:
    return {**_origin_headers(settings), "Authorization": f"Bearer {token}"}


def _assert_test_database(settings: Settings) -> None:
    """CLAUDE.md's 1.4 guard, reproduced locally exactly as every other deleting test file in this
    suite does (`test_identity_database_truths.py`'s own copy, T31): `get_settings()` under
    `APP_ENV=test` still returns the **dev** `database_url` — only the `settings` fixture swaps in
    `test_database_url`. Asserted before the first statement of any test below that writes for real."""
    assert "_test" in settings.database_url, (
        "refusing to run a deleting test against a URL that is not the test database: "
        f"{settings.database_url!r}"
    )


@pytest.fixture
def concurrent_app(
    settings: Settings, engine: AsyncEngine, password_hasher: Argon2PasswordHasher
) -> FastAPI:
    """A second application wired for genuine per-request sessions — `test_identity_database_truths.py`'s
    `concurrent_app` fixture (T31), reproduced here for the identical reason. `tests/conftest.py`'s
    shared `app` fixture binds every request in a test to the **same** already-open `AsyncSession`
    (`_committing_session_override`) — exactly right for isolation, and exactly wrong for two
    coroutines racing a real DELETE against one row: they would corrupt that one session object
    (`IllegalStateChangeError` -> an honest-looking 503 that actually proves nothing about S-46),
    never reproduce two independent HTTP clients contending for one database row. This app instead
    opens and commits its own session per request (`app.state.session_factory =
    create_session_factory(engine)`), exactly as `main.py`'s lifespan wires production. Every write a
    test drives through it is a real, committed row against `tailorcraft_test` — cleaned up by hand."""
    app = create_app(settings)
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = create_session_factory(engine)
    app.state.celery = celery_app
    app.state.password_hasher = password_hasher
    return app


# ---------------------------------------------------------------------------------------------
# AC-29 — the happy path
# ---------------------------------------------------------------------------------------------


async def test_delete_account_with_the_correct_password_is_204_and_clears_the_refresh_cookie(
    client: AsyncClient, settings: Settings
) -> None:
    token, _ = await _register_2_2(client, settings)

    response = await client.post(
        DELETE_ACCOUNT_URL,
        json={"password": A_STRONG_PASSWORD},
        headers=_delete_account_headers(settings, token),
    )

    assert response.status_code == 204, response.text
    cleared = _refresh_cookie_attrs(response)
    assert cleared["max-age"] == "0"


async def test_delete_account_removes_the_user_so_the_old_credentials_no_longer_sign_in(
    client: AsyncClient, settings: Settings, session: AsyncSession
) -> None:
    """**Amended in slice 2.5 (T27): was `..._so_a_second_register_of_the_same_email_succeeds`.** That
    proof read "a second register answering 201 rather than 409 means the account is gone" — and
    registration answers 202 for every address now, so it can no longer tell a deleted account from a
    live one. The proof of the row's absence is now the row itself, and that the old password gets
    the unknown-email answer (401 `invalid_credentials`)."""
    email = f"t19-delacct-reuse-{uuid4().hex}@example.com"
    token, user_id = await _register_2_2(client, settings, email=email)
    signed_in = await client.post(
        LOGIN_URL, json=_credentials(email, A_STRONG_PASSWORD), headers=_origin_headers(settings)
    )
    assert signed_in.status_code == 200, signed_in.text  # the positive control: it did sign in

    deleted = await client.post(
        DELETE_ACCOUNT_URL,
        json={"password": A_STRONG_PASSWORD},
        headers=_delete_account_headers(settings, token),
    )
    assert deleted.status_code == 204, deleted.text

    after = await client.post(
        LOGIN_URL, json=_credentials(email, A_STRONG_PASSWORD), headers=_origin_headers(settings)
    )
    assert after.status_code == 401, after.text
    assert _error_code(after) == "invalid_credentials"
    remaining = (
        await session.execute(
            text("SELECT count(*) FROM identity_user WHERE id = :i"), {"i": UUID(user_id)}
        )
    ).scalar_one()
    assert remaining == 0


async def test_delete_account_takes_every_row_and_file_it_owns_and_nothing_a_guest_owns(
    client: AsyncClient, settings: Settings, session: AsyncSession
) -> None:
    """AC-29's `Data` column in full: the user, every login, every retired hash, every saved CV row
    **and file** gone; the guest workspace (`__Host-tc_guest`, its session row, its rows, the
    working copy's
    file) untouched — it is guest data, purged on its own clock, never by account deletion.

    Two saved CVs (real files) plus one refresh rotation (so a retired hash genuinely exists to
    prove the cascade reaches it, not only the login row itself) plus one guest working copy (a
    guest upload whose `copied_from_base_cv_id` names the first saved CV: the shape the retired copy
    route made, still present on the box until the purge takes it)."""
    _assert_test_database(settings)
    token, user_id = await _register_2_2(client, settings)

    # A retired hash: one refresh rotation before the account is erased.
    refreshed = await client.post(REFRESH_URL, headers=_origin_headers(settings))
    assert refreshed.status_code == 200, refreshed.text
    token = refreshed.json()["access_token"]

    first_cv_id = await _upload_saved_cv_2_2(client, token, filename="a.txt")
    second_cv_id = await _upload_saved_cv_2_2(client, token, filename="b.txt")

    # A guest working copy: an ordinary guest upload whose provenance column names the saved CV
    # (the shape 2.2's retired copy route made; `copied_from_base_cv_id` carries no FK, ADR-0022).
    uploaded = await client.post(
        "/api/base-cvs", files={"file": ("copy.txt", b"working copy bytes " * 20, "text/plain")}
    )
    assert uploaded.status_code == 201, uploaded.text
    working_copy_id = uploaded.json()["id"]
    await session.execute(
        text("UPDATE intake_base_cv SET copied_from_base_cv_id = :src WHERE id = :id"),
        {"src": UUID(first_cv_id), "id": UUID(working_copy_id)},
    )
    await session.commit()

    row = (
        await session.execute(
            text("SELECT guest_session_id, file_key FROM intake_base_cv WHERE id = :id"),
            {"id": UUID(working_copy_id)},
        )
    ).one()
    guest_session_id, working_copy_key = row.guest_session_id, row.file_key
    working_copy_path = settings.upload_dir / working_copy_key
    assert working_copy_path.exists(), "setup sanity: the working copy's file must exist"
    working_copy_bytes = working_copy_path.read_bytes()

    async def _login_count() -> int:
        result = await session.execute(
            text("SELECT count(*) FROM identity_login WHERE user_id = :id"), {"id": UUID(user_id)}
        )
        return int(result.scalar_one())

    async def _retired_hash_count() -> int:
        result = await session.execute(
            text(
                "SELECT count(*) FROM identity_retired_refresh_token rt "
                "JOIN identity_login l ON l.id = rt.login_id WHERE l.user_id = :id"
            ),
            {"id": UUID(user_id)},
        )
        return int(result.scalar_one())

    assert await _login_count() == 1, "setup sanity: the register+refresh must leave one login"
    assert await _retired_hash_count() == 1, "setup sanity: the rotation must retire one hash"

    response = await client.post(
        DELETE_ACCOUNT_URL,
        json={"password": A_STRONG_PASSWORD},
        headers=_delete_account_headers(settings, token),
    )

    assert response.status_code == 204, response.text
    assert _refresh_cookie_attrs(response)["max-age"] == "0"

    # The user, every login, every retired hash: gone.
    user_row = await session.execute(
        text("SELECT count(*) FROM identity_user WHERE id = :id"), {"id": UUID(user_id)}
    )
    assert user_row.scalar_one() == 0
    assert await _login_count() == 0
    assert await _retired_hash_count() == 0

    # Every saved CV row and file: gone.
    saved_rows = await session.execute(
        text("SELECT count(*) FROM intake_base_cv WHERE user_id = :id"), {"id": UUID(user_id)}
    )
    assert saved_rows.scalar_one() == 0
    assert not (settings.upload_dir / _saved_cv_key_2_2(first_cv_id)).exists()
    assert not (settings.upload_dir / _saved_cv_key_2_2(second_cv_id)).exists()

    # The guest workspace: session row, working-copy row, and working-copy file, all untouched.
    guest_row = await session.execute(
        text("SELECT count(*) FROM identity_guest_session WHERE id = :id"),
        {"id": guest_session_id},
    )
    assert guest_row.scalar_one() == 1, (
        "the guest session must survive an unrelated account erasure"
    )
    copy_row = await session.execute(
        text("SELECT count(*) FROM intake_base_cv WHERE id = :id"), {"id": UUID(working_copy_id)}
    )
    assert copy_row.scalar_one() == 1, "the working copy's row must survive"
    assert working_copy_path.exists(), "the working copy's file must survive"
    assert working_copy_path.read_bytes() == working_copy_bytes


# ---------------------------------------------------------------------------------------------
# S-39 — missing / foreign Origin: 403, before anything
# ---------------------------------------------------------------------------------------------


async def test_delete_account_with_no_origin_is_403_and_nothing_is_touched(
    client: AsyncClient, settings: Settings
) -> None:
    token, _ = await _register_2_2(client, settings)

    response = await client.post(
        DELETE_ACCOUNT_URL, json={"password": A_STRONG_PASSWORD}, headers=_bearer_delacct(token)
    )

    assert response.status_code == 403, response.text
    assert _error_code(response) == "origin_not_allowed"

    still_signed_in = await client.get(ME_URL, headers=_bearer_delacct(token))
    assert still_signed_in.status_code == 200, still_signed_in.text


async def test_delete_account_with_a_foreign_origin_is_403(
    client: AsyncClient, settings: Settings
) -> None:
    token, _ = await _register_2_2(client, settings)

    response = await client.post(
        DELETE_ACCOUNT_URL,
        json={"password": A_STRONG_PASSWORD},
        headers={**_bearer_delacct(token), "Origin": "https://evil.example"},
    )

    assert response.status_code == 403, response.text
    assert _error_code(response) == "origin_not_allowed"


def _bearer_delacct(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------------------------
# S-40 — the login limiters, fail closed
# ---------------------------------------------------------------------------------------------


async def test_delete_account_rate_limited_by_ip_is_429(
    client: AsyncClient, app: FastAPI, settings: Settings
) -> None:
    # `_register_2_2` signs in through the login route (T27's re-seed), and the delete route shares
    # that limiter's `auth:login` / `ip` counter: the sign-in spends one of the hour's attempts, so
    # the limit is 2 — one spent by the seed, the first delete the last one allowed, the second 429.
    token, _ = await _register_2_2(client, settings)
    _override_settings(app, settings, login_rate_limit_per_ip_per_hour=2)

    first = await client.post(
        DELETE_ACCOUNT_URL,
        json={"password": "definitely-the-wrong-password"},
        headers=_delete_account_headers(settings, token),
    )
    second = await client.post(
        DELETE_ACCOUNT_URL,
        json={"password": "definitely-the-wrong-password"},
        headers=_delete_account_headers(settings, token),
    )

    assert first.status_code == 403, first.text  # the wrong password, not yet limited
    assert second.status_code == 429, second.text
    assert _error_code(second) == "rate_limited"


async def test_delete_account_with_redis_unreachable_is_503_rate_limit_unavailable(
    client: AsyncClient, app: FastAPI, settings: Settings
) -> None:
    token, _ = await _register_2_2(client, settings)
    _override_settings(app, settings, redis_url="redis://127.0.0.1:1/0")

    response = await client.post(
        DELETE_ACCOUNT_URL,
        json={"password": A_STRONG_PASSWORD},
        headers=_delete_account_headers(settings, token),
    )

    assert response.status_code == 503, response.text
    assert _error_code(response) == "rate_limit_unavailable"


# ---------------------------------------------------------------------------------------------
# S-41 — wrong password: 403 password_incorrect, nothing deleted
# ---------------------------------------------------------------------------------------------


async def test_delete_account_with_the_wrong_password_is_403_password_incorrect(
    client: AsyncClient, settings: Settings
) -> None:
    token, _ = await _register_2_2(client, settings)

    response = await client.post(
        DELETE_ACCOUNT_URL,
        json={"password": "definitely-the-wrong-password"},
        headers=_delete_account_headers(settings, token),
    )

    assert response.status_code == 403, response.text
    assert _error_code(response) == "password_incorrect"

    still_signed_in = await client.get(ME_URL, headers=_bearer_delacct(token))
    assert still_signed_in.status_code == 200, still_signed_in.text


async def test_delete_account_with_the_wrong_password_logs_the_refusal_with_reason_password(
    client: AsyncClient, settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    """S-41's `Logged` cell: `identity.account_deletion_refused`, `user_id`, `reason=password` —
    never the password or its length.

    **Mutation, observed red 2026-09-26 and reverted byte-exact.** Replaced
    `routers/auth.py::delete_account`'s `log.info(EVENT_ACCOUNT_DELETION_REFUSED, ...)` call (the
    `except InvalidCredentials:` branch) with `pass`. Re-run:
    ```
    >       assert refusal_lines, f"expected an 'identity.account_deletion_refused' line, captured:
    \\n{caplog.text}"
    E       AssertionError: expected an 'identity.account_deletion_refused' line, captured:
    E
    E       assert []
    FAILED tests/api/test_auth.py::test_delete_account_with_the_wrong_password_logs_the_refusal_with_reason_password
    1 failed in 0.5s
    ```
    Source restored byte-exact (`git diff --stat api/src` empty); re-run green alone and the full
    module green twice in a row afterward.
    """
    token, user_id = await _register_2_2(client, settings)

    with caplog.at_level(logging.INFO):
        response = await client.post(
            DELETE_ACCOUNT_URL,
            json={"password": "definitely-the-wrong-password"},
            headers=_delete_account_headers(settings, token),
        )
    assert response.status_code == 403, response.text

    refusal_lines = [
        r for r in caplog.records if "identity.account_deletion_refused" in r.getMessage()
    ]
    assert refusal_lines, (
        f"expected an 'identity.account_deletion_refused' line, captured:\n{caplog.text}"
    )
    message = refusal_lines[0].getMessage()
    assert user_id in message
    assert "password" in message
    assert "definitely-the-wrong-password" not in caplog.text


# ---------------------------------------------------------------------------------------------
# S-42 — argon2 fails: 503 service_unavailable, nothing deleted
# ---------------------------------------------------------------------------------------------


async def test_delete_account_argon2_failure_is_503(
    client: AsyncClient, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Injected below the adapter's own floor, exactly `test_argon2_hasher_failure_on_login_is_503`
    above: the underlying `argon2.PasswordHasher.verify` explodes, not the adapter's own method."""
    token, _ = await _register_2_2(client, settings)

    def boom(self: Argon2Library, stored: str, secret: bytes) -> None:
        raise RuntimeError("argon2-cffi exploded (S-42)")

    monkeypatch.setattr(Argon2Library, "verify", boom)

    response = await client.post(
        DELETE_ACCOUNT_URL,
        json={"password": A_STRONG_PASSWORD},
        headers=_delete_account_headers(settings, token),
    )

    assert response.status_code == 503, response.text
    assert _error_code(response) == "service_unavailable"


# ---------------------------------------------------------------------------------------------
# S-43 — the commit fails: 503, cookie not cleared, nothing deleted
# ---------------------------------------------------------------------------------------------


async def test_delete_account_commit_failure_is_503_and_does_not_clear_the_cookie(
    client: AsyncClient, settings: Settings, session: AsyncSession
) -> None:
    """S-43. Two saved CVs seeded first, so "nothing deleted" is a claim about real rows and real
    files, not merely about the user row `CommittingAccountData.delete_account`'s failing commit
    touches directly: the erasure is one transaction (`CommittingAccountData`'s own `session.commit()`
    is the commit this test's monkeypatch intercepts — it fires **before** `EraseAccount` unlinks a
    single file), so a failure there must leave the account, its saved CVs and their files exactly as
    they were."""
    token, _ = await _register_2_2(client, settings)
    first_cv_id = await _upload_saved_cv_2_2(client, token, filename="a.txt")
    second_cv_id = await _upload_saved_cv_2_2(client, token, filename="b.txt")
    first_path = settings.upload_dir / _saved_cv_key_2_2(first_cv_id)
    second_path = settings.upload_dir / _saved_cv_key_2_2(second_cv_id)
    assert first_path.exists(), "setup sanity: both files must exist"
    assert second_path.exists(), "setup sanity: both files must exist"

    async def _raise_sqlalchemy_error() -> None:
        raise SQLAlchemyError("simulated commit failure (S-43)")

    # Scoped to only the one request: `session` is the fixture's own connection, shared by every
    # later call this test makes too, and `main.py`'s error handler leaves it able to serve more
    # requests — but only once this failing `commit` is no longer patched onto it.
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(session, "commit", _raise_sqlalchemy_error)
        response = await client.post(
            DELETE_ACCOUNT_URL,
            json={"password": A_STRONG_PASSWORD},
            headers=_delete_account_headers(settings, token),
        )

    assert response.status_code == 503, response.text
    assert _cookie_header_named(response, REFRESH_COOKIE_NAME) is None, (
        "a failed commit must never clear tc_refresh — the server still honours it (I-31's rule)"
    )

    # Nothing was deleted: the user row, both saved CV rows and both files all survive.
    still_signed_in = await client.get(ME_URL, headers=_bearer_delacct(token))
    assert still_signed_in.status_code == 200, still_signed_in.text
    listing = await client.get("/api/me/base-cvs", headers=_bearer_delacct(token))
    assert {item["id"] for item in listing.json()["items"]} == {first_cv_id, second_cv_id}
    assert first_path.exists(), "the commit failure must never have reached a file unlink"
    assert second_path.exists(), "the commit failure must never have reached a file unlink"


async def _upload_saved_cv_2_2(client: AsyncClient, token: str, *, filename: str = "a.txt") -> str:
    """A minimal saved CV, uploaded through the real route — `test_saved_base_cvs.py`'s own
    `_upload_extracted_saved_cv`, reproduced locally rather than imported across test modules."""
    text = ("word " * 200).encode()
    response = await client.post(
        "/api/me/base-cvs",
        files={"file": (filename, text, "text/plain")},
        headers=_bearer_delacct(token),
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "extracted", body
    return str(body["id"])


def _saved_cv_key_2_2(cv_id: str) -> str:
    """The storage key `FileRef.for_base_cv` derives from an id — via the real function, so this
    cannot drift from ADR-0011's own sharding rule."""
    from tailorcraft.domain.intake.value_objects import BaseCvId, CvContentType
    from tailorcraft.domain.shared.files import FileRef

    return FileRef.for_base_cv(BaseCvId(UUID(cv_id)), CvContentType.TXT).key


# ---------------------------------------------------------------------------------------------
# S-45 — some unlinks fail: 204, one retention.account_erased line, one
# retention.account_file_unlink_failed line per failure
# ---------------------------------------------------------------------------------------------


async def test_delete_account_with_a_failing_unlink_is_still_204_and_logs_both_lines(
    client: AsyncClient,
    settings: Settings,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S-45. Two saved CVs; one file's unlink fails — injected **below** `LocalFileStore.delete`'s
    own floor (`Path.unlink`, the library call it wraps, matched by the exact path so no unrelated
    `Path.unlink` call anywhere else in the request can be caught by accident), never the adapter's
    own public method (CLAUDE.md's I-45 correction: patching `LocalFileStore.delete` itself would
    bypass its own `except OSError` floor, which is the very thing this row exists to prove still
    stands). The account is still erased (204 — nothing left to retry against): one
    `retention.account_erased` line with the right counts, and one
    `retention.account_file_unlink_failed` line naming the failure's exception type — both from
    `routers/auth.py::_log_erasure`.

    **Mutation, observed red 2026-09-26 and reverted byte-exact.** Replaced `_log_erasure`'s body
    with `pass; return` before its two `log.info`/`log.warning` calls. Re-run:
    ```
    erased_lines = [r for r in caplog.records if "retention.account_erased" in r.getMessage()]
    >       assert erased_lines, f"expected a 'retention.account_erased' line, captured:\\n{caplog.text}"
    E       AssertionError: expected a 'retention.account_erased' line, captured:
    E         ERROR    tailorcraft.infrastructure.files.local_file_store:local_file_store.py:121
    {"errno": 5, ..., "event": "file_store.delete_failed", "level": "error", ...}
    E
    E       assert []
    FAILED tests/api/test_auth.py::test_delete_account_with_a_failing_unlink_is_still_204_and_logs_both_lines
    1 failed in 0.6s
    ```
    (The adapter's own `file_store.delete_failed` line still fires — it lives inside
    `LocalFileStore`, not `_log_erasure` — which is exactly why this test needs the *positive*
    assertions on `retention.account_erased`/`retention.account_file_unlink_failed` rather than a
    bare "something was logged".) Source restored byte-exact (`git diff --stat api/src` empty);
    re-run green alone and the full module green twice in a row afterward.
    """
    token, user_id = await _register_2_2(client, settings)
    ok_cv_id = await _upload_saved_cv_2_2(client, token, filename="a.txt")
    failing_cv_id = await _upload_saved_cv_2_2(client, token, filename="b.txt")

    failing_path = settings.upload_dir / _saved_cv_key_2_2(failing_cv_id)
    ok_path = settings.upload_dir / _saved_cv_key_2_2(ok_cv_id)
    original_unlink = Path.unlink

    def _selective_unlink(self: Path, missing_ok: bool = False) -> None:
        if self == failing_path:
            raise OSError(5, "Input/output error")
        return original_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", _selective_unlink)

    with caplog.at_level(logging.INFO):
        response = await client.post(
            DELETE_ACCOUNT_URL,
            json={"password": A_STRONG_PASSWORD},
            headers=_delete_account_headers(settings, token),
        )

    assert response.status_code == 204, response.text
    assert not ok_path.exists(), "the successfully-unlinked file must be gone"
    assert failing_path.exists(), "the failing unlink must have left its bytes behind"

    erased_lines = [r for r in caplog.records if "retention.account_erased" in r.getMessage()]
    assert erased_lines, f"expected a 'retention.account_erased' line, captured:\n{caplog.text}"
    erased_message = erased_lines[0].getMessage()
    assert user_id in erased_message
    assert '"base_cvs": 2' in erased_message, erased_message
    assert '"files_unlinked": 1' in erased_message, erased_message
    assert '"files_failed": 1' in erased_message, erased_message

    failure_lines = [
        r for r in caplog.records if "retention.account_file_unlink_failed" in r.getMessage()
    ]
    assert len(failure_lines) == 1, f"expected exactly one failure line, captured:\n{caplog.text}"
    failure_message = failure_lines[0].getMessage()
    assert user_id in failure_message
    assert "FileStoreUnavailable" in failure_message, (
        "the OSError must have been translated by LocalFileStore's own floor before EraseAccount "
        f"ever sees it — got: {failure_message}"
    )


# ---------------------------------------------------------------------------------------------
# S-46 — two concurrent deletions of one account: one 204, one 401 not_signed_in
# ---------------------------------------------------------------------------------------------


async def test_two_concurrent_correct_deletions_exactly_one_204_one_401(
    concurrent_app: FastAPI, settings: Settings, engine: AsyncEngine
) -> None:
    """S-46, on `concurrent_app` (see its docstring above) rather than the shared `app`/`client`
    fixtures: the shared fixture binds both in-flight requests to one `AsyncSession`, which is not
    safe for concurrent use and answers `IllegalStateChangeError` -> 503 for a reason that has
    nothing to do with S-46's race. Real, committed rows; cleaned up by hand — a `finally` that
    tolerates the row already being gone (whichever request won the race deleted it for real)."""
    _assert_test_database(settings)
    async with _new_client(concurrent_app) as setup_client:
        token, user_id = await _register_2_2(setup_client, settings)

    try:
        async with (
            _new_client(concurrent_app) as client_a,
            _new_client(concurrent_app) as client_b,
        ):
            results = await asyncio.gather(
                client_a.post(
                    DELETE_ACCOUNT_URL,
                    json={"password": A_STRONG_PASSWORD},
                    headers=_delete_account_headers(settings, token),
                ),
                client_b.post(
                    DELETE_ACCOUNT_URL,
                    json={"password": A_STRONG_PASSWORD},
                    headers=_delete_account_headers(settings, token),
                ),
            )

        statuses = sorted(r.status_code for r in results)
        assert statuses == [204, 401], [r.text for r in results]
        the_401 = next(r for r in results if r.status_code == 401)
        assert _error_code(the_401) == "not_signed_in"
    finally:
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM identity_user WHERE id = :id"), {"id": UUID(user_id)}
            )


# ---------------------------------------------------------------------------------------------
# AC-30 — after the 204, the still-unexpired access token is dead everywhere
# ---------------------------------------------------------------------------------------------


async def test_after_deletion_the_still_valid_access_token_is_401_not_signed_in_on_me(
    client: AsyncClient, settings: Settings
) -> None:
    token, _ = await _register_2_2(client, settings)
    deleted = await client.post(
        DELETE_ACCOUNT_URL,
        json={"password": A_STRONG_PASSWORD},
        headers=_delete_account_headers(settings, token),
    )
    assert deleted.status_code == 204, deleted.text

    response = await client.get(ME_URL, headers=_bearer_delacct(token))

    assert response.status_code == 401, response.text
    assert _error_code(response) == "not_signed_in"


async def test_after_deletion_every_me_base_cvs_route_is_401_not_signed_in(
    client: AsyncClient, settings: Settings
) -> None:
    """AC-30's own wording: "every `/api/me/base-cvs*` route", not only `GET`. `UploadBaseCv`/
    `ListSavedBaseCvs`/`RenameSavedBaseCv`/`DeleteSavedBaseCv` all resolve the user first (AC-8), so
    a fresh, never-issued CV id is enough for `PATCH`/`DELETE` — the 401 fires before either route
    would even look for a row."""
    token, _ = await _register_2_2(client, settings)
    deleted = await client.post(
        DELETE_ACCOUNT_URL,
        json={"password": A_STRONG_PASSWORD},
        headers=_delete_account_headers(settings, token),
    )
    assert deleted.status_code == 204, deleted.text

    some_cv_id = str(uuid4())
    checks: list[tuple[str, Response]] = [
        (
            "GET /api/me/base-cvs",
            await client.get("/api/me/base-cvs", headers=_bearer_delacct(token)),
        ),
        (
            "POST /api/me/base-cvs",
            await client.post(
                "/api/me/base-cvs",
                files={"file": ("a.txt", ("word " * 200).encode(), "text/plain")},
                headers=_bearer_delacct(token),
            ),
        ),
        (
            "PATCH /api/me/base-cvs/{id}",
            await client.patch(
                f"/api/me/base-cvs/{some_cv_id}",
                json={"label": "x"},
                headers=_bearer_delacct(token),
            ),
        ),
        (
            "DELETE /api/me/base-cvs/{id}",
            await client.delete(f"/api/me/base-cvs/{some_cv_id}", headers=_bearer_delacct(token)),
        ),
    ]

    for name, response in checks:
        assert response.status_code == 401, f"{name}: {response.text}"
        assert _error_code(response) == "not_signed_in", f"{name}: {response.text}"
