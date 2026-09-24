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

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
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
from tailorcraft.infrastructure.api.deps import get_app_settings, get_clock, get_session
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
from tailorcraft.infrastructure.redis_client import create_redis
from tailorcraft.infrastructure.settings import Settings
from tailorcraft.infrastructure.tasks.app import app as celery_app

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
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


def _new_client(app: FastAPI) -> AsyncClient:
    """A second, independent cookie jar against the same app, for tests that need two distinct
    "requesters" while sharing `ASGITransport`'s fixed peer IP (the per-IP rate-limit tests)."""
    return AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://testserver"
    )


@pytest.fixture(autouse=True)
def _reset_redis_between_tests(clear_redis: None) -> None:
    """Applies `clear_redis` (conftest.py) to every test in this module automatically — nearly every
    test here touches a rate limiter, and CLAUDE.md is explicit that a database rollback does not
    reach Redis."""


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


async def test_registering_an_already_taken_email_is_409(
    client: AsyncClient, settings: Settings
) -> None:
    body = _credentials("taken@example.com", A_STRONG_PASSWORD)

    first = await client.post(REGISTER_URL, json=body, headers=_origin_headers(settings))
    assert first.status_code == 201, first.text

    second = await client.post(REGISTER_URL, json=body, headers=_origin_headers(settings))

    assert second.status_code == 409, second.text
    assert _error_code(second) == "email_already_registered"


async def test_registering_with_a_live_guest_cookie_leaves_the_guest_session_untouched(
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    clock: FixedClock,
) -> None:
    """AC-29: no `Set-Cookie: tc_guest` on any outcome, and the guest session row is byte-identical
    afterwards, including `expires_at` — registration must never read, set or extend it."""
    guest_session, raw_guest_token = await _seed_guest_session(session, clock)
    client.cookies.set(GUEST_COOKIE_NAME, raw_guest_token)

    response = await client.post(
        REGISTER_URL,
        json=_credentials("guest.carrier@example.com", A_STRONG_PASSWORD),
        headers=_origin_headers(settings),
    )

    assert response.status_code == 201, response.text
    assert _cookie_header_named(response, GUEST_COOKIE_NAME) is None

    sessions = _guest_session_repository(session)
    reloaded = await sessions.get(guest_session.id)
    assert reloaded.id == guest_session.id
    assert reloaded.created_at == guest_session.created_at
    assert reloaded.expires_at == guest_session.expires_at


# ---------------------------------------------------------------------------------------------
# AC-33 — the success envelope's key set
# ---------------------------------------------------------------------------------------------


async def test_a_successful_registration_returns_exactly_the_authenticated_response_shape(
    client: AsyncClient, settings: Settings
) -> None:
    response = await client.post(
        REGISTER_URL,
        json=_credentials("shape.check@example.com", A_STRONG_PASSWORD),
        headers=_origin_headers(settings),
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert set(body) == {"access_token", "token_type", "expires_in", "user"}
    assert body["token_type"] == "Bearer"
    assert set(body["user"]) == {"id", "email", "created_at"}
    assert body["user"]["email"] == "shape.check@example.com"


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

    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
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


async def test_the_registration_cookies_attributes(client: AsyncClient, settings: Settings) -> None:
    response = await client.post(
        REGISTER_URL,
        json=_credentials("cookie.check@example.com", A_STRONG_PASSWORD),
        headers=_origin_headers(settings),
    )

    assert response.status_code == 201, response.text
    attrs = _refresh_cookie_attrs(response)
    assert attrs["httponly"] is True
    assert str(attrs["samesite"]).lower() == "strict"
    assert attrs["path"] == "/api/auth"
    assert "domain" not in attrs
    assert "secure" not in attrs, "APP_ENV=test must never set Secure"
    assert len(str(attrs["__value__"])) == 43
    expected_max_age = settings.refresh_token_ttl_days * 86400
    assert int(str(attrs["max-age"])) == expected_max_age


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
        base_url="http://testserver",
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
    assert set(body) == {"id", "email", "created_at"}
    assert body["email"] == "me.shape@example.com"


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
) -> None:
    """I-45: the hasher's own floor (`PasswordHashingFailed`) maps to the same `service_unavailable`
    503 as a dead Postgres — to the client, both mean "try again"."""
    await _seed_user(
        session,
        password_hasher,
        clock,
        email="hasher.floor@example.com",
        password="a-real-password",
    )

    async def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("argon2-cffi exploded")

    monkeypatch.setattr(password_hasher, "verify", boom)

    response = await client.post(
        LOGIN_URL,
        json=_credentials("hasher.floor@example.com", "a-real-password"),
        headers=_origin_headers(settings),
    )

    assert response.status_code == 503, response.text
    assert _error_code(response) == "service_unavailable"


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

    assert response.status_code == 201, response.text
    assert response.headers.get("cache-control") == "no-store"


async def test_a_successful_refresh_carries_cache_control_no_store(
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    password_hasher: Argon2PasswordHasher,
    clock: FixedClock,
) -> None:
    user = await _seed_user(session, password_hasher, clock)
    _login, raw_token = await _seed_login(session, user, clock)
    client.cookies.set(REFRESH_COOKIE_NAME, raw_token)

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
# AC-29 — no `tc_guest` Set-Cookie on any of the five endpoints' outcomes
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
            f"{response.request.url} must never set tc_guest"
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
    """`/me` must never touch `tc_guest` — with a live guest cookie and no bearer, the answer is
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
