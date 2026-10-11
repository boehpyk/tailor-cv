"""Slice 4.2, T14 RED: what a disabled account meets at the edge (AC-21's bearer 403s, AC-24, AC-25 (c)).

The disabled state is set by a raw `UPDATE` on the test's own session (the shared `app` fixture), not
through the admin route: this file is about the *consumers* of the bit (`require_user`, `/login`,
reset), and a raw statement shares no code with anything under test.

**Which of these are red today.** The bearer 403s (AC-21) are: `require_user` does not read the
disabled bit yet, so a disabled user's still-valid token gets 200 (`assert 200 == 403`). The login half
of AC-24 and the wrong-password byte-identity may already pass, because `LogIn` (T12) refuses and the
global `AccountDisabled -> 403` translation is live; those are regression guards with no recorded red
(2c01310's rule), proven by mutation at T15.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tailorcraft.domain.identity.value_objects import PasswordResetId
from tailorcraft.infrastructure.settings import Settings
from tests.api.account_mail_support import (
    install_recording_queue,
    reset_token,
)
from tests.api.me_support import A_PASSWORD, bearer, count_rows, error_code, seed_user_and_sign_in
from tests.integration.fakes import RecordingAccountMailer, RecordingAccountMailQueue

LOGIN = "/api/auth/login"
ME = "/api/auth/me"
BOARD = "/api/me/board"
RESET = "/api/auth/password-reset"
RESET_CONFIRM = "/api/auth/password-reset/confirm"
A_NEW_PASSWORD = "an entirely different passphrase 7"
DISABLED_MESSAGE = "This account has been disabled."


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="https://testserver") as c:
        yield c


@pytest.fixture(autouse=True)
def _reset_redis_between_tests(clear_redis: None) -> None:
    """Login and reset limiters live in Redis, which the database rollback does not reach."""


def _origin(settings: Settings) -> dict[str, str]:
    return {"Origin": settings.public_base_url}


async def _disable(session: AsyncSession, user_id: UUID) -> None:
    await session.execute(
        text("UPDATE identity_user SET disabled_at = now() WHERE id = :i"), {"i": user_id}
    )
    await session.execute(text("DELETE FROM identity_login WHERE user_id = :i"), {"i": user_id})


def _email() -> str:
    return f"t14-{uuid4().hex[:12]}@example.com"


# --------------------------------------------------------------------------- AC-21 bearer 403s


@pytest.mark.parametrize("url", [ME, BOARD], ids=["auth-me", "me-board"])
async def test_ac21_a_disabled_users_still_valid_bearer_is_403_account_disabled(
    client: AsyncClient, settings: Settings, session: AsyncSession, url: str
) -> None:
    token, user_id = await seed_user_and_sign_in(client, settings)
    before = await client.get(url, headers=bearer(token))
    assert before.status_code == 200, before.text
    await _disable(session, user_id)

    response = await client.get(url, headers=bearer(token))

    assert response.status_code == 403, response.text
    assert error_code(response) == "account_disabled"
    assert response.json()["error"]["message"] == DISABLED_MESSAGE


async def test_ac21_an_enabled_users_bearer_is_unaffected(
    client: AsyncClient, settings: Settings, session: AsyncSession
) -> None:
    """The control: the bit is read, and only a set bit refuses."""
    token, user_id = await seed_user_and_sign_in(client, settings)
    await _disable(session, user_id)
    await session.execute(
        text("UPDATE identity_user SET disabled_at = NULL WHERE id = :i"), {"i": user_id}
    )

    response = await client.get(ME, headers=bearer(token))

    assert response.status_code == 200, response.text


# --------------------------------------------------------------------------- AC-24 login


async def test_ac24_a_correct_password_for_a_disabled_account_is_403_with_no_cookie_and_no_login(
    client: AsyncClient, settings: Settings, session: AsyncSession
) -> None:
    email = _email()
    _, user_id = await seed_user_and_sign_in(client, settings, email=email)
    await _disable(session, user_id)
    client.cookies.clear()

    response = await client.post(
        LOGIN, json={"email": email, "password": A_PASSWORD}, headers=_origin(settings)
    )

    assert response.status_code == 403, response.text
    assert error_code(response) == "account_disabled"
    assert "set-cookie" not in response.headers
    assert await count_rows(session, "identity_login", user_id=user_id) == 0


async def test_ac24_a_wrong_password_is_the_ordinary_401_byte_identical_to_an_enabled_accounts(
    client: AsyncClient, settings: Settings, session: AsyncSession
) -> None:
    disabled_email, enabled_email = _email(), _email()
    _, disabled_id = await seed_user_and_sign_in(client, settings, email=disabled_email)
    await seed_user_and_sign_in(client, settings, email=enabled_email)
    await _disable(session, disabled_id)
    client.cookies.clear()

    disabled = await client.post(
        LOGIN,
        json={"email": disabled_email, "password": "not the password at all"},
        headers=_origin(settings),
    )
    enabled = await client.post(
        LOGIN,
        json={"email": enabled_email, "password": "not the password at all"},
        headers=_origin(settings),
    )

    assert enabled.status_code == 401, enabled.text
    assert disabled.status_code == 401, disabled.text
    assert error_code(disabled) == "invalid_credentials"
    assert disabled.content == enabled.content
    assert {k: v for k, v in disabled.headers.items() if k != "date"} == {
        k: v for k, v in enabled.headers.items() if k != "date"
    }


async def test_ac24_after_the_account_is_enabled_the_same_login_is_200(
    client: AsyncClient, settings: Settings, session: AsyncSession
) -> None:
    email = _email()
    _, user_id = await seed_user_and_sign_in(client, settings, email=email)
    await _disable(session, user_id)
    client.cookies.clear()
    refused = await client.post(
        LOGIN, json={"email": email, "password": A_PASSWORD}, headers=_origin(settings)
    )
    assert refused.status_code == 403, refused.text
    await session.execute(
        text("UPDATE identity_user SET disabled_at = NULL WHERE id = :i"), {"i": user_id}
    )

    response = await client.post(
        LOGIN, json={"email": email, "password": A_PASSWORD}, headers=_origin(settings)
    )

    assert response.status_code == 200, response.text


# --------------------------------------------------------------------------- AC-25 (c) reset


@pytest.fixture
def queue(app: FastAPI) -> RecordingAccountMailQueue:
    return install_recording_queue(app)


async def test_ac25c_a_disabled_user_can_reset_the_password_but_still_cannot_log_in(
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    queue: RecordingAccountMailQueue,
) -> None:
    from tailorcraft.infrastructure.tasks import container

    email = _email()
    _, user_id = await seed_user_and_sign_in(client, settings, email=email)
    await _disable(session, user_id)
    client.cookies.clear()

    requested = await client.post(RESET, json={"email": email}, headers=_origin(settings))
    assert requested.status_code == 202, requested.text
    assert len(queue.resets) == 1, "the reset mail is enqueued for a disabled account"
    reset_id: PasswordResetId = queue.resets[0]
    mailer = RecordingAccountMailer()
    await container._build_password_reset_delivery(settings, session, mailer)(reset_id)
    await session.commit()

    confirmed = await client.post(
        RESET_CONFIRM,
        json={"token": reset_token(mailer), "password": A_NEW_PASSWORD},
        headers=_origin(settings),
    )
    assert confirmed.status_code == 204, confirmed.text

    new_login = await client.post(
        LOGIN, json={"email": email, "password": A_NEW_PASSWORD}, headers=_origin(settings)
    )
    assert new_login.status_code == 403, new_login.text
    assert error_code(new_login) == "account_disabled"
    old_login = await client.post(
        LOGIN, json={"email": email, "password": A_PASSWORD}, headers=_origin(settings)
    )
    assert old_login.status_code == 401, "the new password is installed; the old one is gone"
