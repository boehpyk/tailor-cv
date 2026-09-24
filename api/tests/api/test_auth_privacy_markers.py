"""AC-46's planted-marker privacy test for `identity-register-and-login` (T32).

Follows `test_intake.py`'s AC-12 privacy test and `test_purge_privacy_log_markers.py`'s (1.6, AC-38)
structure: markers planted in every PII/credential field this slice touches, a real flow driven
through the real app, `caplog` read for leaks.

**`caplog`, not `structlog.testing.capture_logs()`.** `test_intake.py`'s module docstring records the
measured reason and this file follows it rather than re-deriving it: `configure_logging()` (which
`create_app` calls, and every fresh `app` fixture therefore triggers) rebuilds `structlog.configure`'s
processor list from scratch on every call, and `cache_logger_on_first_use=True` means a module-level
logger first used by an *earlier* test in this session keeps a reference to *that* test's processors
— `capture_logs()`'s in-place mutation of "the current list" cannot reach it. `caplog` sidesteps the
question entirely: `structlog.stdlib.LoggerFactory()` routes every call through a real
`logging.Logger`, by which point `JSONRenderer` has already flattened the event to a string, and
`caplog`'s handler on the root logger sees it regardless of which processor chain rendered it. Adding
a second, unreliable capture path alongside a working one would not add coverage — it would add a
`capture_logs` variable in this file that can read empty while the assertions "pass" for the wrong
reason, exactly the vacuous-test defect this suite's whole tiered-TDD discipline exists to prevent.

**No synthetic marker for the refresh tokens, their hash, or the access token.** They are already the
most distinctive strings this test could plant: `secrets.token_urlsafe(32)` (refresh tokens) and a
signed JWT (the access token) are globally unique by construction, and the stored hash is derived
from one. Planting a *second*, human-chosen marker on top would prove nothing a leak of the real value
would not already prove, and would let a sloppy implementation launder the real secret through a
marker-shaped field it happens to null out. The values captured below are exactly what a real client
holds after this flow — nothing stronger is available to plant.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Final
from uuid import uuid4

import pytest
from httpx import AsyncClient, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tailorcraft.domain.identity.value_objects import EmailAddress
from tailorcraft.infrastructure.api.refresh_cookie import (
    COOKIE_NAME as REFRESH_COOKIE_NAME,
)
from tailorcraft.infrastructure.persistence.mapping.identity.user import user_table
from tailorcraft.infrastructure.settings import Settings

REGISTER_URL = "/api/auth/register"
LOGIN_URL = "/api/auth/login"
REFRESH_URL = "/api/auth/refresh"
LOGOUT_URL = "/api/auth/logout"
ME_URL = "/api/auth/me"

_MARKER_PREFIX: Final = "QA46MARKER"


def _marker(label: str) -> str:
    return f"{_MARKER_PREFIX}-{label}-{uuid4().hex}"


def _marker_email(label: str) -> str:
    """A well-formed, distinctive address (AC-2: ASCII, one `@`, a dotted domain)."""
    return f"{_marker(label).lower()}@example.com"


def _headers(settings: Settings, ip_marker: str) -> dict[str, str]:
    return {"Origin": settings.public_base_url, "X-Forwarded-For": ip_marker}


def _cookie_value(response: Response, name: str) -> str:
    for header in response.headers.get_list("set-cookie"):
        if header.startswith(f"{name}="):
            return header.split(";", 1)[0].partition("=")[2]
    raise AssertionError(
        f"no Set-Cookie: {name}=... in {response.headers.get_list('set-cookie')!r}"
    )


@pytest.fixture(autouse=True)
def _reset_redis(clear_redis: None) -> None:
    """Every register/login call here goes through a rate limiter keyed on `ip_marker` and the
    marker email; a clean Redis means this run's markers are the only thing in it (CLAUDE.md: the
    database rollback never reaches Redis)."""


async def test_a_full_auth_flow_never_logs_or_returns_any_of_its_seven_secrets(
    client: AsyncClient,
    session: AsyncSession,
    settings: Settings,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AC-46. Register, three logins (success / wrong password / unknown email), four refreshes
    (rotated, raced, rotated again, reused), an unknown-token refresh, an expired-login refresh,
    logout and `/me` — the whole failure contract's identity surface in one pass — with every
    response captured for its body and `caplog` capturing every log record. None of the seven
    markers (email, password, password hash, refresh token, refresh-token hash, access token, client
    IP) may appear anywhere in either.
    """
    marker_email = _marker_email("email")
    marker_password = _marker("password") + " and some more words for good measure"
    wrong_password = marker_password + "-WRONG"
    unknown_email = _marker_email("unknown-email")
    ip_marker = f"203.0.113.7-{_marker('ip')}"
    headers = _headers(settings, ip_marker)

    responses: list[Response] = []

    with caplog.at_level(logging.DEBUG):
        # --- register --------------------------------------------------------------------------
        register_response = await client.post(
            REGISTER_URL,
            json={"email": marker_email, "password": marker_password},
            headers=headers,
        )
        responses.append(register_response)
        assert register_response.status_code == 201, register_response.text
        access_token_1 = register_response.json()["access_token"]
        raw_token_1 = _cookie_value(register_response, REFRESH_COOKIE_NAME)

        # --- login: wrong password (I-10) -------------------------------------------------------
        wrong_password_response = await client.post(
            LOGIN_URL,
            json={"email": marker_email, "password": wrong_password},
            headers=headers,
        )
        responses.append(wrong_password_response)
        assert wrong_password_response.status_code == 401, wrong_password_response.text

        # --- login: unknown email (I-9) ----------------------------------------------------------
        unknown_email_response = await client.post(
            LOGIN_URL,
            json={"email": unknown_email, "password": marker_password},
            headers=headers,
        )
        responses.append(unknown_email_response)
        assert unknown_email_response.status_code == 401, unknown_email_response.text

        # --- login: success (I-13 — a second, independent Login) ---------------------------------
        login_response = await client.post(
            LOGIN_URL,
            json={"email": marker_email, "password": marker_password},
            headers=headers,
        )
        responses.append(login_response)
        assert login_response.status_code == 200, login_response.text
        access_token_2 = login_response.json()["access_token"]
        raw_token_2 = _cookie_value(login_response, REFRESH_COOKIE_NAME)

        # --- refresh: rotated (I-22) --------------------------------------------------------------
        client.cookies.clear()
        client.cookies.set(REFRESH_COOKIE_NAME, raw_token_2)
        rotate_1 = await client.post(REFRESH_URL, headers=headers)
        responses.append(rotate_1)
        assert rotate_1.status_code == 200, rotate_1.text
        raw_token_3 = _cookie_value(rotate_1, REFRESH_COOKIE_NAME)

        # --- refresh: raced (I-23 — the just-retired immediate predecessor, within the grace) -----
        client.cookies.clear()
        client.cookies.set(REFRESH_COOKIE_NAME, raw_token_2)
        raced = await client.post(REFRESH_URL, headers=headers)
        responses.append(raced)
        assert raced.status_code == 409, raced.text

        # --- refresh: rotated again, so raw_token_2 becomes two generations stale -----------------
        client.cookies.clear()
        client.cookies.set(REFRESH_COOKIE_NAME, raw_token_3)
        rotate_2 = await client.post(REFRESH_URL, headers=headers)
        responses.append(rotate_2)
        assert rotate_2.status_code == 200, rotate_2.text
        raw_token_4 = _cookie_value(rotate_2, REFRESH_COOKIE_NAME)

        # --- refresh: reused (I-24 — two generations behind, REUSED even at 0 s) ------------------
        client.cookies.clear()
        client.cookies.set(REFRESH_COOKIE_NAME, raw_token_2)
        reused = await client.post(REFRESH_URL, headers=headers)
        responses.append(reused)
        assert reused.status_code == 401, reused.text
        assert reused.json()["error"]["code"] == "refresh_token_reused"

        # --- refresh: unknown token (I-20) ---------------------------------------------------------
        client.cookies.clear()
        client.cookies.set(REFRESH_COOKIE_NAME, "x" * 43)
        unknown_token_response = await client.post(REFRESH_URL, headers=headers)
        responses.append(unknown_token_response)
        assert unknown_token_response.status_code == 401, unknown_token_response.text

        # --- refresh: expired login, deleted on sight (I-21) ---------------------------------------
        _expired_login, expired_raw_token = await _seed_an_already_expired_login(
            session, marker_email + ".expired"
        )
        client.cookies.clear()
        client.cookies.set(REFRESH_COOKIE_NAME, expired_raw_token)
        expired_response = await client.post(REFRESH_URL, headers=headers)
        responses.append(expired_response)
        assert expired_response.status_code == 401, expired_response.text

        # --- logout (login 1, still alive) ----------------------------------------------------------
        client.cookies.clear()
        client.cookies.set(REFRESH_COOKIE_NAME, raw_token_1)
        logout_response = await client.post(LOGOUT_URL, headers=headers)
        responses.append(logout_response)
        assert logout_response.status_code == 204, logout_response.text

        # --- me (the access token issued at registration; I-29: still valid after logout) ----------
        me_response = await client.get(
            ME_URL, headers={"Authorization": f"Bearer {access_token_1}"}
        )
        responses.append(me_response)
        assert me_response.status_code == 200, me_response.text

    # --- Read the resulting password hash back from the database, the sixth marker ---------------
    password_hash_marker = (
        (
            await session.execute(
                select(user_table.c.password_hash).where(
                    user_table.c.email == EmailAddress.parse(marker_email)
                )
            )
        )
        .scalar_one()
        .value
    )
    assert password_hash_marker.startswith("$"), password_hash_marker

    # --- Guard against vacuity: this run must have actually logged something -----------------------
    assert caplog.records, "expected the flow above to have produced at least one log record"

    # --- The lines the failure contract promises, with their fields. Each `_record_where` call is
    # itself a proof that this run produced the line it is about to check the fields of — a marker
    # search over a flow that never hit the branch would prove nothing. -----------------------------
    def _record_where(event: str, **fields: str) -> logging.LogRecord:
        for record in caplog.records:
            message = record.getMessage()
            if f'"event": "{event}"' not in message:
                continue
            if all(f'"{key}": "{value}"' in message for key, value in fields.items()):
                return record
        raise AssertionError(
            f"no captured record for event={event!r} fields={fields!r}; captured:\n{caplog.text}"
        )

    _record_where("identity.login_failed", reason="unknown_email")
    wrong_password_record = _record_where("identity.login_failed", reason="wrong_password")
    assert '"user_id"' in wrong_password_record.getMessage(), wrong_password_record.getMessage()

    _record_where("identity.refresh_refused", reason="unknown")
    expired_record = _record_where("identity.refresh_refused", reason="expired")
    assert '"login_id"' in expired_record.getMessage(), expired_record.getMessage()

    raced_record = _record_where("identity.refresh_raced")
    assert '"login_id"' in raced_record.getMessage(), raced_record.getMessage()

    reuse_record = next(
        (
            record
            for record in caplog.records
            if '"event": "identity.refresh_reuse_detected"' in record.getMessage()
        ),
        None,
    )
    assert reuse_record is not None, caplog.text
    assert reuse_record.levelname == "WARNING", (
        f"identity.refresh_reuse_detected must log at warning, was {reuse_record.levelname}"
    )
    reuse_message = reuse_record.getMessage()
    for field in ("login_id", "user_id", "generation_presented", "generation_current"):
        assert f'"{field}"' in reuse_message, reuse_message
    assert '"generation_presented": 1' in reuse_message, reuse_message
    assert '"generation_current": 3' in reuse_message, reuse_message

    # --- The actual claim -----------------------------------------------------------------------
    # Every marker is checked against every captured log record — the spec's "nor in any error
    # response body" is about *bodies*, not logs, and a log line has no legitimate reason to carry
    # any of the seven under any status code.
    all_markers: dict[str, str] = {
        "the marker email": marker_email,
        "the marker password": marker_password,
        "the wrong-password variant": wrong_password,
        "the unknown email": unknown_email,
        "the resulting password hash": password_hash_marker,
        "the raw refresh token (register)": raw_token_1,
        "the raw refresh token (login)": raw_token_2,
        "the raw refresh token (rotated once)": raw_token_3,
        "the raw refresh token (rotated twice)": raw_token_4,
        "the raw refresh token (the expired login's)": expired_raw_token,
        "the access token (register)": access_token_1,
        "the access token (login)": access_token_2,
        "the client IP marker": ip_marker,
    }
    log_text = caplog.text
    for description, marker in all_markers.items():
        assert marker not in log_text, (
            f"{description} ({marker!r}) appeared in a captured log record — AC-46. "
            f"Full captured text:\n{log_text}"
        )

    # A response BODY is different: the email and the two access tokens are the whole point of a
    # *successful* register/login/refresh/me response — that is the legitimate channel, not a leak.
    # What AC-46 actually forbids is these markers surfacing where the API never means to put them:
    # a password (which is never echoed by design, success or failure), a refresh token (delivered
    # only via `Set-Cookie`, never the JSON body), a password hash, the client IP, or any of the
    # seven appearing in an *error* body specifically (the spec's own wording).
    never_in_any_body: dict[str, str] = {
        "the marker password": marker_password,
        "the wrong-password variant": wrong_password,
        "the resulting password hash": password_hash_marker,
        "the raw refresh token (register)": raw_token_1,
        "the raw refresh token (login)": raw_token_2,
        "the raw refresh token (rotated once)": raw_token_3,
        "the raw refresh token (rotated twice)": raw_token_4,
        "the raw refresh token (the expired login's)": expired_raw_token,
        "the client IP marker": ip_marker,
    }
    for response in responses:
        for description, marker in never_in_any_body.items():
            assert marker not in response.text, (
                f"{description} ({marker!r}) appeared in a response body — AC-46: "
                f"{response.status_code} {response.request.url} -> {response.text}"
            )

    error_body_only: dict[str, str] = {
        "the marker email": marker_email,
        "the unknown email": unknown_email,
        "the access token (register)": access_token_1,
        "the access token (login)": access_token_2,
    }
    for response in responses:
        if response.status_code < 400:
            continue
        for description, marker in error_body_only.items():
            assert marker not in response.text, (
                f"{description} ({marker!r}) appeared in an ERROR response body — AC-46: "
                f"{response.status_code} {response.request.url} -> {response.text}"
            )


async def _seed_an_already_expired_login(session: AsyncSession, email: str) -> tuple[object, str]:
    """A real, committed-through-savepoint `User` and `Login`, expired by real wall-clock time —
    `created_at` an hour ago with a one-second lifetime, exactly T31's technique. Deferred imports:
    the mapper-configuration reason every other file in this suite documents at the same import."""
    from tailorcraft.domain.identity.login import Login
    from tailorcraft.domain.identity.user import User
    from tailorcraft.domain.identity.value_objects import EmailAddress, PasswordHash
    from tailorcraft.infrastructure.api.refresh_cookie import mint_refresh_token
    from tailorcraft.infrastructure.persistence.repositories.identity.login import (
        SqlAlchemyLoginRepository,
    )
    from tailorcraft.infrastructure.persistence.repositories.identity.user import (
        SqlAlchemyUserRepository,
    )

    now = datetime.now(UTC).replace(microsecond=0)
    started_at = now - timedelta(hours=1)

    users = SqlAlchemyUserRepository(session)
    user = User.register_with_password(
        users.next_identity(),
        EmailAddress.parse(email),
        PasswordHash("$argon2id$v=19$m=8,t=1,p=1$c2FsdA$dummy"),
        started_at,
    )
    await users.add(user)

    logins = SqlAlchemyLoginRepository(session)
    minted = mint_refresh_token()
    login = Login.start(
        logins.next_identity(), user.id, minted.token_hash, started_at, timedelta(seconds=1)
    )
    await logins.add(login)
    await session.commit()
    return login, minted.token
